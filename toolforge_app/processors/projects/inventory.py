"""Discover annual pages and cache only their current findings and metadata."""
import copy
import re
import time

from ...issues import annotate_report
from ...wiki import bot_edit_block
from . import PREFIX
from .config import get_config
from .rules import page_report

RULES_VERSION = 1


def selection(config):
    return [RULES_VERSION, config['start_year'], config['spacing']]


def discover(wiki, config):
    params = dict(action='query', list='allpages', apnamespace=4,
                  apprefix=PREFIX.split(':', 1)[1], apfilterredir='nonredirects', aplimit=500)
    titles = set()
    while True:
        data = wiki.request(params)
        for item in data['query']['allpages']:
            match = re.fullmatch(re.escape(PREFIX) + r'(\d{4})', item['title'])
            if match and config['start_year'] <= int(match[1]) <= 2099:
                titles.add(item['title'])
        if 'continue' not in data:
            return sorted(titles, reverse=True)
        params.update(data['continue'])


def record(base, now, config, bot_username):
    row = page_report(base, now, spacing=config['spacing'],
                      excluded=bot_edit_block(base.text, bot_username) == 'bot-excluded')
    return row


def completed(row):
    return (row['closed'] and not row['pending'] and not row['excluded'] and
            not any(item['section'] == 'problems' for item in row['items']))


def complete_year(store, row):
    complete = completed(row)
    marks = store.get_state('projects:completed_years', {})
    if complete:
        marks[row['title']] = dict(revision=row['revision'], at=row['checked_at'])
    else:
        marks.pop(row['title'], None)
    store.set_state('projects:completed_years', marks)
    row['completed'] = complete


def refresh(wiki, store, config, bot_username='', *, now=None, force=False, include_completed=False):
    now = time.time() if now is None else now
    titles = discover(wiki, config)
    previous = store.get_state('projects:inventory', {})
    old = {row['title']: row for row in previous.get('pages', [])}
    marks = store.get_state('projects:completed_years', {})
    selected = [title for title in titles if include_completed or title not in marks or title not in old]
    metadata = wiki.revisions(selected, content=force)
    changed = [base.title for base in metadata if not base.missing and (force or
               previous.get('selection') != selection(config) or base.title not in old or
               old[base.title]['revision'] != base.revision)]
    bases = {base.title: base for base in metadata if not base.missing} if force else {
        base.title: base for base in wiki.revisions(changed, content=True)}
    rows = [copy.deepcopy(old[title]) for title in titles if title not in selected and title in old]
    for base in metadata:
        if base.missing:
            continue
        row = record(bases[base.title], now, config, bot_username) if base.title in bases else copy.deepcopy(old[base.title])
        row['checked_at'] = now
        for item in row['items']:
            item['checked_at'] = now
        row['completed'] = completed(row)
        if row['completed']:
            marks[row['title']] = dict(revision=row['revision'], at=now)
        else:
            marks.pop(row['title'], None)
        rows.append(row)
    rows.sort(key=lambda row: row['title'], reverse=True)
    inventory = dict(checked_at=now, pages=rows, selection=selection(config))
    store.set_state('projects:inventory', inventory)
    store.set_state('projects:completed_years', marks)
    store.set_state('projects:monitor_error', None)
    return inventory, bases


def report(store, slug='projects', config=None):
    config = config or get_config(store, slug)
    inventory = store.get_state('projects:inventory', {})
    rows = copy.deepcopy(inventory.get('pages', []))
    items = [item for row in rows for item in row['items']]
    problems = [i for i in items if i['section'] == 'problems']
    pending = [dict(title=row['title'], categories=[]) for row in rows if row['pending']]
    value = dict(items=items, problems=len(problems), nuances=len(items) - len(problems),
                 pages=len(rows), page_rows=rows, manual=problems, pending_articles=pending,
                 completed_years=sorted(row['title'].rsplit('/', 1)[-1] for row in rows if row.get('completed')),
                 to_process=len(pending) if inventory else None, skipped_articles=[], skipped=0,
                 source=PREFIX.rstrip('/'), sources=[PREFIX.rstrip('/')], latest_check=inventory.get('checked_at', 0),
                 monitor_error=store.get_state('projects:monitor_error'),
                 after_run=store.get_state('projects:last_run_at'), categories=[],
                 excluded=[dict(title=row['title'], reason='Запрет бота') for row in rows if row['excluded']])
    # Removed annual pages resolve their previous findings at this observation.
    scopes = {row['title']: row['checked_at'] for row in rows}
    if inventory:
        for title in store.get_state('projects:observed_titles', []):
            scopes.setdefault(title, inventory['checked_at'])
        titles = [row['title'] for row in rows]
        if titles != store.get_state('projects:observed_titles', []):
            store.set_state('projects:observed_titles', titles)
    return annotate_report(store, slug, value, scopes=scopes)
