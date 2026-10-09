"""Discover and retain only monthly categories covered by the configured formats."""
import base64
import hashlib
import json
import time
import uuid
import zlib
from datetime import datetime
from pathlib import Path

import pymysql

from ...schedules import MOSCOW
from ...wiki import WikiError
from .config import get_config
from .formats import date_of, expected_category, refresh_formats, saved_formats


def selection(config):
    return {key: config[key] for key in ('source_page', 'category_prefix', 'check_simple', 'check_complex',
                                        'start_year', 'start_month')}


def decode(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def replica_snapshot(prefix):
    cnf = Path.home() / 'replica.my.cnf'
    if not cnf.is_file():
        return None
    try:
        with pymysql.connect(host='ruwiki.analytics.db.svc.wikimedia.cloud', database='ruwiki_p',
                             read_default_file=str(cnf), charset='utf8mb4',
                             connect_timeout=5, read_timeout=25) as conn:
            with conn.cursor() as cur:
                cur.execute('SET max_statement_time=20')
                like = prefix.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%').replace(' ', '\\_') + '%'
                cur.execute('SELECT c.cat_title,c.cat_pages FROM category c LEFT JOIN page p '
                            'ON p.page_namespace=14 AND p.page_title=c.cat_title '
                            'WHERE c.cat_pages>0 AND p.page_id IS NULL AND c.cat_title LIKE %s '
                            'ORDER BY c.cat_title', (like,))
                missing = [('Категория:' + decode(title).replace('_', ' '), count) for title, count in cur.fetchall()]
                cur.execute('SELECT page_title,page_latest FROM page WHERE page_namespace=14 '
                            'AND page_is_redirect=0 AND page_title LIKE %s ORDER BY page_title', (like,))
                existing = [('Категория:' + decode(title).replace('_', ' '), revision) for title, revision in cur.fetchall()]
        return dict(missing=missing, existing=existing, backend='replica')
    except (pymysql.MySQLError, OSError, ValueError):
        # Never expose credential files, connection strings or database errors in reports.
        raise WikiError('category-replica-unavailable') from None


def api_snapshot(wiki, prefix):
    missing, existing = [], []
    params = dict(action='query', generator='allcategories', gacprefix=prefix, gacmin=1,
                  gaclimit=500, prop='info|categoryinfo')
    while True:
        data = wiki.request(params)
        for page in data.get('query', {}).get('pages', []):
            if page.get('missing') and page.get('categoryinfo', {}).get('size', 0):
                missing.append((page['title'], page['categoryinfo']['size']))
        if 'continue' not in data:
            break
        params.update(data['continue'])
    params = dict(action='query', generator='allpages', gapnamespace=14, gapprefix=prefix,
                  gapfilterredir='nonredirects', gaplimit=500, prop='info')
    while True:
        data = wiki.request(params)
        existing.extend((page['title'], page['lastrevid']) for page in data.get('query', {}).get('pages', [])
                        if not page.get('missing'))
        if 'continue' not in data:
            break
        params.update(data['continue'])
    return dict(missing=missing, existing=existing, backend='api')


def snapshot_key(config):
    scope = json.dumps(selection(config), sort_keys=True, ensure_ascii=False)
    return 'categories:scope:' + hashlib.sha256(scope.encode()).hexdigest()[:40]


def snapshot(store, config):
    key = snapshot_key(config)
    header = store.get_state(key + ':header')
    if not header:
        return None
    with store.report_cache_lock:
        cached = store.report_cache.get(key)
        if cached and cached['version'] == header['version']:
            return cached
        raw = store.get_state(key)
        result = dict(json.loads(zlib.decompress(base64.b64decode(raw))), **header)
        store.report_cache[key] = result
        return result


def refresh_snapshot(wiki, store, config, formats, *, force=True, now=None):
    now = time.time() if now is None else now
    cached = snapshot(store, config)
    if cached and not force and now - cached['at'] < 60 and cached['format_signature'] == formats['signature']:
        return cached
    wiki.request_guard()
    scan = replica_snapshot(config['category_prefix']) or api_snapshot(wiki, config['category_prefix'])
    wiki.request_guard()
    current = datetime.fromtimestamp(now, MOSCOW)
    first, last = (config['start_year'], config['start_month']), (current.year, current.month)
    result = dict(backend=scan['backend'], missing=[], existing=[])
    for kind in ('missing', 'existing'):
        for title, value in scan[kind]:
            date = date_of(title)
            if not date or kind == 'existing' and not first <= date <= last:
                continue
            if expected_category(title, formats, config):
                result[kind].append((title, value))
    # Persist just the working set; unrelated missing categories are never retained.
    version = uuid.uuid4().hex
    packed = base64.b64encode(zlib.compress(json.dumps(result, ensure_ascii=False).encode(), 6)).decode('ascii')
    with store.engine.begin() as conn:
        store._set_state(conn, snapshot_key(config), packed)
        store._set_state(conn, snapshot_key(config) + ':header',
                        dict(at=now, version=version, format_signature=formats['signature']))
    return snapshot(store, config)


def refresh_inventory(wiki, store, slug, config=None, *, scan=None, formats=None, force=True, now=None):
    config = config or get_config(store, slug)
    now = time.time() if now is None else now
    formats = formats or refresh_formats(wiki, store, config)
    scan = scan or refresh_snapshot(wiki, store, config, formats, force=force, now=now)
    creates = slug == 'categories-create'
    current = (datetime.fromtimestamp(now, MOSCOW).year, datetime.fromtimestamp(now, MOSCOW).month)
    rows = {}
    for title, value in scan['missing' if creates else 'existing']:
        if not title.startswith('Категория:' + config['category_prefix']):
            continue
        date = date_of(title)
        if not date or not creates and not (tuple((config['start_year'], config['start_month'])) <= date <= current):
            continue
        expected = expected_category(title, formats, config)
        if expected:
            rows[title] = (dict(count=value, kind=expected['kind']) if creates else
                           dict(revision=value, kind=expected['kind'], format_signature=expected['signature']))
    if creates and rows:
        # Replicas can lag behind a successful save or a human-created page.
        # Validate this small monthly subset against the live API before counting it.
        live = wiki.category_pages(list(rows))
        def still_missing(title):
            page = live.get(title, {})
            return page.get('base') and page['base'].missing and page.get('population', 0) > 0
        rows = {title: dict(row, count=live[title]['population']) for title, row in rows.items() if still_missing(title)}
    result = dict(articles=rows, total=len(rows), checked_at=now, selection=selection(config),
                  format_signature=formats['signature'], format_revision=formats['revision'],
                  backend=scan['backend'],
                  version=uuid.uuid4().hex)
    store.set_state(slug + ':inventory', result)
    store.set_state(slug + ':monitor_error', None)
    return result


def is_checked(entry):
    # Weekly passes check new members of our set. Revisions and format changes
    # are reconsidered in the explicit full pass, not in the weekly queue.
    return bool(entry and entry['outcome'] in {'ok', 'edited', 'bot-excluded'})


def report(store, slug, config=None):
    config = config or get_config(store, slug)
    inv = store.get_state(slug + ':inventory', {})
    if inv.get('selection') != selection(config):
        inv = {}
    current = inv.get('articles', {})
    formats = saved_formats(store, config)
    checked = store.checked_articles(slug) if slug == 'categories-format' else {}
    retries = {job['title'] for job in store.queue(slug) if job['kind'] == 'article'}
    done = {title for title in current if is_checked(checked.get(title))}
    pending = {title: row for title, row in current.items() if title not in done or title in retries}
    result = store.get_state(slug + ':result', {})
    manual = []
    if result.get('selection') == selection(config) and result.get('format_signature') == inv.get('format_signature'):
        manual.extend(item for item in result.get('manual', []) if item['title'] in current)
    excluded = [dict(title=title, reason='На странице запрещена работа бота', categories=[])
                for title in done if checked[title]['outcome'] == 'bot-excluded']
    return dict(problems=len(manual), manual=manual, skipped=0, skipped_articles=[], nuances=0,
                pages=len(current), to_process=len(pending) if inv else None, category_total=len(current),
                checked=len(done), remembered=len(checked), latest_check=inv.get('checked_at'),
                source=config['source_page'], sources=[config['source_page']], categories=[], excluded=excluded,
                pending_articles=[dict(title=title, categories=[], **row) for title, row in sorted(pending.items())],
                after_run=result.get('at'), monitor_error=store.get_state(slug + ':monitor_error'),
                formats=formats,
                backend=inv.get('backend'), limited=False)
