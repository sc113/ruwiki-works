"""Stable problem identities and dates of actual observations, not page views."""
import hashlib
import json
from collections import Counter


def annotate_report(store, slug, report):
    if not report or not report.get('latest_check'):
        return report
    entries, counts = [], Counter()
    obkat = slug == 'obkat'
    scopes = {p['title']: p['checked_at'] for p in store.all_pages()} if obkat else {slug: report['latest_check']}
    items = [row for row in report.get('items', []) if row['section'] == 'problems'] if obkat else report.get('manual', [])
    for row in items:
        identity = (row['page'], row['type'], row.get('display_title', ''), row.get('parent_title', '')) if obkat else (row['title'], row['reason'])
        counts[identity] += 1
        key = hashlib.sha256(json.dumps([identity, counts[identity]], ensure_ascii=False).encode()).hexdigest()
        row['issue_key'] = key
        at = row['checked_at'] if obkat else report['latest_check'] if slug.startswith('maintenance-') else row.get('verified_at') or report.get('after_run') or report['latest_check']
        entries.append(dict(issue_key=key, scope=row['page'] if obkat else slug, at=at))
    history = store.observe_problems(slug, entries, scopes)
    for row in items:
        record = history.get(row['issue_key'])
        if record:
            row['problem_history'] = {key: record[key] for key in ('first_seen', 'last_seen', 'episodes')}
    return report
