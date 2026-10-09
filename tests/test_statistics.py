from datetime import datetime

from sqlalchemy import update

from toolforge_app.statistics import statistics
from toolforge_app.storage import runs
from toolforge_app.web import create_app


def at(settings, value):
    return datetime.fromisoformat(value).replace(tzinfo=settings.zone).timestamp()


def saved_run(store, settings, *, slug='maintenance-dates', start='2026-10-09T03:00:00', events=(),
              dry=False, kind='daily', summary=None, status='success'):
    run_id = store.start_run(kind, processor=slug, dry_run=dry)
    store.finish_run(run_id, events, summary or dict(checked=1, changed=1, errors=0), {}, status=status)
    with store.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(started_at=at(settings, start),
            finished_at=max([at(settings, start)] + [e['at'] for e in events])))
    return run_id


def edit(settings, page, time='2026-10-09T03:01:00', count=1, code='edited'):
    return dict(at=at(settings, time), title=page, code=code, message='private diagnostic',
                changes=[dict(template='Template', action='updated') for _ in range(count)])


def test_saved_edits_unique_articles_and_template_operations_are_separate(settings, store):
    saved_run(store, settings, events=[edit(settings, 'A', count=2)])
    saved_run(store, settings, slug='maintenance-rq', events=[edit(settings, 'A'), edit(settings, 'B')])
    saved_run(store, settings, slug='obkat', events=[edit(settings, 'Википедия:Обсуждение категорий', count=0),
        edit(settings, 'Википедия:Сводка', code='table_edited', count=0)])
    saved_run(store, settings, dry=True, events=[edit(settings, 'Dry')])
    saved_run(store, settings, kind='import', events=[edit(settings, 'Import')])
    data = statistics(store, settings.zone, '2026-10', now=at(settings, '2026-10-09T12:00:00'))
    assert data['total'] == dict(runs=4, test_runs=1, failed=0, checked=4, skipped=0, edits=5,
                                 templates=4, pages=4, articles=2)
    assert data['today'] == data['total']
    assert data['days'][0]['edits'] == 5 and data['months'][0]['templates'] == 4


def test_moscow_midnight_cross_month_edits_and_partial_failure(settings, store):
    saved_run(store, settings, start='2026-09-30T23:59:59', status='failed', summary=dict(errors=1),
              events=[edit(settings, 'A', '2026-10-01T00:00:01')])
    saved_run(store, settings, start='2026-10-01T00:00:00', summary=dict(errors=0, report_problems=3))
    data = statistics(store, settings.zone, '2026-10', now=at(settings, '2026-10-09T12:00:00'))
    assert data['total']['runs'] == 1 and data['total']['edits'] == 1 and data['total']['failed'] == 0
    september = next(row for row in data['months'] if row['date'] == '2026-09')
    assert september['runs'] == 1 and september['edits'] == 0 and september['failed'] == 1
    october_first = next(row for row in data['days'] if row['date'] == '2026-10-01')
    assert october_first['articles'] == 1


def test_statistics_has_no_fixed_recent_runs_cutoff(settings, store):
    for index in range(105):
        saved_run(store, settings, events=[edit(settings, 'A')])
    data = statistics(store, settings.zone, '2026-10')
    assert data['total']['runs'] == 105 and data['total']['edits'] == 105 and data['total']['articles'] == 1


def test_statistics_is_public_and_contains_only_counts(settings, store):
    saved_run(store, settings, events=[edit(settings, 'PRIVATE_TITLE')])
    client = create_app(settings, store).test_client()
    response = client.get('/statistics?month=2026-10')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'Изменений шаблонов' in text and 'PRIVATE_TITLE' not in text and 'private diagnostic' not in text
    assert client.get('/statistics?view=months').status_code == 200
    for query in ('month=2026-1', 'month=2026-13', 'month=9999-01', 'view=secret'):
        assert client.get('/statistics?' + query).status_code == 400
