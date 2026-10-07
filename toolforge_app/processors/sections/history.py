"""Read-only, idempotent import of processing records and inventory caches."""
import csv
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import TASK_SLUGS
from .config import get_config
from .inventory import inventory_signature

HEADERS = ['article_title', 'mode', 'date', 'status', 'changes_made', 'processing_time', 'message']
MODES = dict(zip(TASK_SLUGS, ('empty_to_fill', 'fill_to_empty')))


def valid_title(title):
    return isinstance(title, str) and bool(title) and len(title) <= 255 and not any(c in title for c in '\r\n|{}[]<>\x00')


def read_log(path, mode):
    entries = {}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle, delimiter='\t')
        if reader.fieldnames != HEADERS:
            raise ValueError(path.name + ': неизвестный формат заголовка')
        for row in reader:
            title = (row.get('article_title') or '').strip().replace('_', ' ')
            if not valid_title(title) or row.get('mode') != mode or None in row or None in row.values():
                raise ValueError(path.name + ': некорректная строка')
            entries.pop(title, None)
            if row['status'] not in {'saved', 'saved_manually', 'no_changes_needed'}:
                continue
            at = datetime.fromisoformat(row['date']).replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp()
            edited = row['status'] in {'saved', 'saved_manually'}
            entries[title] = dict(title=title, checked_at=at, outcome='edited' if edited else 'unchanged',
                                  reason='' if edited else 'Замена не требовалась при предыдущей проверке', run_id='')
    return entries


def import_legacy(source, store):
    source = Path(source)
    # Validate all files before any mutations; credentials and cookies are never read.
    logs = {slug: read_log(source / f'log_{mode}.tsv', mode) for slug, mode in MODES.items()}
    inventories = {}
    for slug, mode in MODES.items():
        path = source / f'articles_{mode}.json'
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, list) or any(not isinstance(row, dict) or row.get('namespace_id') != 0 or not valid_title(row.get('title', '')) for row in data):
            raise ValueError(path.name + ': некорректный список статей')
        dates = [datetime.fromisoformat(row['timestamp']).replace(tzinfo=ZoneInfo('Europe/Moscow')).timestamp() for row in data if row.get('timestamp')]
        inventories[slug] = (sorted({row['title'] for row in data}), max(dates) if dates else path.stat().st_mtime)
    alias_path = source / 'templates_redirects.json'
    aliases = json.loads(alias_path.read_text(encoding='utf-8-sig'))
    if not isinstance(aliases, dict) or any(not valid_title(name) or not isinstance(values, dict) or
        any(not valid_title(alias) or not valid_title(target) for alias, target in values.items()) for name, values in aliases.items()):
        raise ValueError('Некорректный кеш перенаправлений')
    counts, now = {}, time.time()
    for slug, entries in logs.items():
        old = store.checked_articles(slug)
        store.record_article_checks(slug, list(entries.values()), overwrite=False)
        counts[slug] = dict(total=len(entries), added=len(entries.keys() - old.keys()))
        store.set_state(slug + ':legacy_import', dict(at=now, **counts[slug],
            filename=f'log_{MODES[slug]}.tsv', sha256=hashlib.sha256((source / f'log_{MODES[slug]}.tsv').read_bytes()).hexdigest()))
        if not store.get_state(slug + ':inventory'):
            config = get_config(store, slug)
            titles, at = inventories[slug]
            root = 'Шаблон:' + config['source_template']
            store.set_state(slug + ':inventory', dict(source=root, sources=[root], origin='legacy', checked_at=at,
                selection=inventory_signature(config), total=len(titles), articles={title: [root] for title in titles},
                categories=[dict(title=root, count=len(titles), articles=titles)], limited=False))
    for name, values in aliases.items():
        key = 'sections:aliases:' + hashlib.sha256(name.encode()).hexdigest()[:40]
        if not store.get_state(key):
            target = next(iter(values.values()), name)
            store.set_state(key, dict(at=alias_path.stat().st_mtime, definition=dict(name=target, aliases=sorted({name, target, *values}))))
    return counts
