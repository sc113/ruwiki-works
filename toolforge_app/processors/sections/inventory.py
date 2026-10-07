"""Transclusion inventories and persistent visits, independent for each direction."""
import hashlib
import json
import time

from ...wiki import WikiError
from .config import get_config
from .transform import normalize


def signature(config):
    return json.dumps({key: config[key] for key in ('source_template', 'replacement_template', 'ignored_templates')},
                      ensure_ascii=False, sort_keys=True)


def inventory_signature(config):
    return [config['source_template'], config['embeddedin_limit']]


class Catalogue:
    def __init__(self, wiki, store, days=14):
        self.wiki, self.store, self.days = wiki, store, days
        self.loaded = {}

    def aliases(self, name, force=False):
        if name in self.loaded and not force:
            return self.loaded[name]
        key = 'sections:aliases:' + hashlib.sha256(name.encode()).hexdigest()[:40]
        cached = self.store.get_state(key)
        if not force and cached and time.time() - cached['at'] < self.days * 86400:
            definition = cached['definition']
        else:
            definition = self.wiki.template_aliases(name)
            self.store.set_state(key, dict(at=time.time(), definition=definition))
        self.loaded[name] = definition
        return definition


def alias_readouts(store, config):
    rows = []
    for name in dict.fromkeys([config['source_template'], config['replacement_template'], *config['ignored_templates']]):
        cached = store.get_state('sections:aliases:' + hashlib.sha256(name.encode()).hexdigest()[:40], {})
        rows.append(dict(template=name, at=cached.get('at'), aliases=cached.get('definition', {}).get('aliases', [])))
    return rows


def refresh_inventory(wiki, store, slug, now=None, *, config=None, force=True):
    config = config or get_config(store, slug)
    now = time.time() if now is None else now
    cached = store.get_state(slug + ':inventory', {})
    if not force and cached.get('selection') == inventory_signature(config) and now - cached['checked_at'] < config['articles_cache_days'] * 86400:
        return cached
    catalogue = Catalogue(wiki, store, config['redirects_cache_days'])
    definition = catalogue.aliases(config['source_template'])
    names = []
    seen = set()
    for name in [definition['name'], *definition['aliases']]:
        if normalize(name) not in seen:
            names.append(name)
            seen.add(normalize(name))
    rows, articles, limited = [], {}, False
    for name in names:
        titles = set()
        for item in wiki.template_articles(name):
            title = item['title']
            if title in titles:
                continue
            if config['embeddedin_limit'] and len(titles) >= config['embeddedin_limit']:
                limited = True
                break
            titles.add(title)
        source = 'Шаблон:' + name
        rows.append(dict(title=source, count=len(titles), articles=sorted(titles)))
        for title in titles:
            articles.setdefault(title, []).append(source)
    result = dict(source='Шаблон:' + config['source_template'], sources=['Шаблон:' + name for name in names],
                  selection=inventory_signature(config), checked_at=now, articles=articles, total=len(articles),
                  categories=sorted(rows, key=lambda row: (-row['count'], row['title'])), limited=limited)
    store.set_state(slug + ':inventory', result)
    store.set_state(slug + ':monitor_error', dict(code='inventory-limited', at=now) if limited else None)
    from ...issues import annotate_report
    annotate_report(store, slug, report(store, slug, config))
    return result


def report(store, slug, config=None):
    config = config or get_config(store, slug)
    inventory = store.get_state(slug + ':inventory')
    if inventory and inventory.get('selection') != inventory_signature(config):
        inventory = None
    current = inventory['articles'] if inventory else {}
    checked = store.checked_articles(slug)
    retries = {job['title'] for job in store.queue(slug) if job['kind'] == 'article'}
    pending = current if not config['resume'] else {title: sources for title, sources in current.items() if title not in checked or title in retries}
    result = store.get_state(slug + ':result', {})
    valid = result.get('signature') == signature(config)
    entries = {title: dict(item) for title, item in checked.items() if title in current and item['outcome'] != 'edited'}
    if valid:
        entries.update({item['title']: item for field in ('manual', 'skipped') for item in result.get(field, []) if item['title'] in current})
    manual, skipped = [], []
    for title, item in sorted(entries.items()):
        row = dict(title=title, reason=item.get('reason') or 'Замена не требуется', categories=current[title],
                   verified_at=item.get('checked_at') or result.get('at'), retry_queued=title in retries)
        (manual if item['outcome'] in {'conflicting-markers', 'error'} else skipped).append(row)
    imported = store.get_state(slug + ':legacy_import', {})
    return dict(problems=len(manual), skipped=len(skipped), skipped_articles=skipped, manual=manual, nuances=0,
                pages=len(current), to_process=len(pending) if inventory else None, category_total=len(current) if inventory else None,
                checked=len(current.keys() & checked.keys()), remembered=len(checked),
                latest_check=inventory['checked_at'] if inventory else None, source='Шаблон:' + config['source_template'],
                sources=inventory['sources'] if inventory else ['Шаблон:' + config['source_template']],
                categories=inventory['categories'] if inventory else [],
                pending_articles=[dict(title=title, categories=sources) for title, sources in sorted(pending.items())],
                after_run=result.get('at') if valid else imported.get('at'),
                monitor_error=store.get_state(slug + ':monitor_error'), aliases=alias_readouts(store, config),
                limited=inventory.get('limited', False) if inventory else False,
                cache_origin=inventory.get('origin') if inventory else None)
