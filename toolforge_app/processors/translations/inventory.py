"""Both extraction actions share category selection, with independent results."""
import json
import time

from .config import get_config
from .outcomes import is_skipped, skip_reason


def signature(config):
    return json.dumps({key: config[key] for key in ('target_categories', 'target_templates', 'parse_talk_template') if key in config}, ensure_ascii=False, sort_keys=True)


def refresh_inventory(wiki, store, slug, now=None, *, config=None):
    config = config or get_config(store, slug)
    articles, categories = {}, []
    for category in config['target_categories']:
        titles = sorted({item['title'] for item in wiki.category_members(category, namespace=0)})
        categories.append(dict(title=category, count=len(titles), articles=titles))
        for title in titles:
            articles.setdefault(title, []).append(category)
    result = dict(source=config['target_categories'][0], sources=config['target_categories'],
                  checked_at=time.time() if now is None else now, articles=articles, total=len(articles),
                  categories=sorted(categories, key=lambda item: (-item['count'], item['title'])))
    store.set_state(slug + ':inventory', result)
    store.set_state(slug + ':monitor_error', None)
    from ...issues import annotate_report
    annotate_report(store, slug, report(store, slug, config))
    return result


def report(store, slug, config=None):
    config = config or get_config(store, slug)
    inventory = store.get_state(slug + ':inventory')
    if inventory and inventory['sources'] != config['target_categories']:
        inventory = None
    current = inventory['articles'] if inventory else {}
    checked = store.checked_articles(slug)
    retries = {job['title'] for job in store.queue(slug) if job['kind'] == 'article'}
    pending = {title: categories for title, categories in current.items() if title not in checked or title in retries}
    result = store.get_state(slug + ':result', {})
    valid = result.get('signature') == signature(config)
    entries = {title: dict(item) for title, item in checked.items()
               if title in current and (item['reason'] or item['outcome'] == 'unchanged')}
    if valid:
        entries.update({item['title']: item for field in ('manual', 'skipped') for item in result.get(field, [])
                        if item['title'] in current})
    manual, skipped = [], []
    for title, item in sorted(entries.items()):
        skip = is_skipped(item)
        row = dict(title=title, reason=skip_reason(item) if skip else item['reason'], categories=current[title],
                   verified_at=item.get('checked_at') or result.get('at'), retry_queued=title in retries)
        (skipped if skip else manual).append(row)
    imported = store.get_state(slug + ':legacy_import', {})
    return dict(problems=len(manual), skipped=len(skipped), skipped_articles=skipped,
                nuances=0, pages=len(current), to_process=len(pending) if inventory else None,
                category_total=len(current) if inventory else None, checked=len(current.keys() & checked.keys()),
                latest_check=inventory['checked_at'] if inventory else None, source=config['target_categories'][0],
                sources=config['target_categories'], categories=inventory['categories'] if inventory else [], manual=manual,
                pending_articles=[dict(title=title, categories=categories) for title, categories in sorted(pending.items())],
                after_run=result.get('at') if valid else imported.get('at'), monitor_error=store.get_state(slug + ':monitor_error'))
