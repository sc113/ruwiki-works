"""Public counts based on recorded runs and successfully saved page edits."""
import calendar
import json
from datetime import datetime

from .processors import TASKS, get_task, get_processor


def month_shift(moment, offset):
    number = moment.year * 12 + moment.month - 1 + offset
    year, month = divmod(number, 12)
    return moment.replace(year=year, month=month + 1, day=1)


def bucket():
    return dict(runs=0, test_runs=0, failed=0, checked=0, skipped=0, edits=0,
                templates=0, pages=set(), articles=set())


def public_bucket(value):
    return {key: len(number) if isinstance(number, set) else number for key, number in value.items()}


def statistics(store, zone, month=None, now=None):
    current = datetime.now(zone) if now is None else datetime.fromtimestamp(now, zone)
    selected = datetime.strptime(month, '%Y-%m').replace(tzinfo=zone) if month else current.replace(day=1)
    selected = selected.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = month_shift(selected, 1)
    start = month_shift(selected, -11)
    months = {month_shift(start, index).strftime('%Y-%m'): bucket() for index in range(12)}
    days = {f'{selected:%Y-%m}-{day:02d}': bucket()
            for day in range(1, calendar.monthrange(selected.year, selected.month)[1] + 1)}
    tasks = {task.slug: bucket() for task in TASKS if task.enabled}
    total = bucket()

    def targets(at, slug):
        date = datetime.fromtimestamp(at, zone)
        monthly = months.get(date.strftime('%Y-%m'))
        if monthly is None:
            return []
        result = [monthly]
        daily = days.get(date.strftime('%Y-%m-%d'))
        if daily is not None:
            result.extend([daily, total, tasks[slug]])
        return result

    for run in store.statistics_runs(start.timestamp(), end.timestamp()):
        slug = run['processor']
        if slug not in tasks:
            continue
        summary = json.loads(run['summary'])
        for value in targets(run['started_at'], slug):
            value['runs'] += 1
            value['test_runs'] += bool(run['dry_run'])
            value['failed'] += run['status'] == 'failed'
            value['checked'] += summary.get('checked') or 0
            value['skipped'] += summary.get('skipped') or 0
        if run['dry_run']:
            continue
        for event in json.loads(run['events']):
            if event['code'] not in {'edited', 'table_edited'} or not event.get('title'):
                continue
            for value in targets(event.get('at', run['started_at']), slug):
                value['edits'] += 1
                value['pages'].add(event['title'])
                if get_task(slug).processor not in {'obkat', 'categories'}:
                    value['articles'].add(event['title'])
                value['templates'] += event.get('template_count', len(event.get('changes') or []))

    return dict(month=selected.strftime('%Y-%m'), total=public_bucket(total),
        today=public_bucket(days.get(current.strftime('%Y-%m-%d'), bucket())),
        days=[dict(date=date, **public_bucket(value)) for date, value in reversed(list(days.items()))
              if date <= current.strftime('%Y-%m-%d')],
        months=[dict(date=date, **public_bucket(value)) for date, value in reversed(list(months.items()))],
        tasks=[dict(task=get_task(slug), module=get_processor(get_task(slug).processor), **public_bucket(value))
               for slug, value in tasks.items()], since=store.statistics_since())
