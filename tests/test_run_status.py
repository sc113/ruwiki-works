import json
import time

import pytest
from sqlalchemy import update

from toolforge_app.health import system_status
from toolforge_app.overview import build_overview
from toolforge_app.run_status import completion_status
from toolforge_app.storage import dump, runs
from toolforge_app.web import create_app
from test_health import prime
from test_web import set_session


@pytest.mark.parametrize('status,summary,report,expected', [
    ('success', {'errors': 0}, {'problems': 1}, 'success'),
    ('failed', {'errors': 0, 'report_problems': 1}, {}, 'success'),
    ('failed', {'errors': 1}, {'problems': 1}, 'failed'),
    ('failed', {}, {'problems': 1}, 'failed'),
    ('success', {'errors': 0, 'skipped': 1995}, {'problems': 0}, 'success'),
    ('success', {'errors': 0, 'report_problems': 0, 'problems': 2}, {}, 'success'),
    ('running', {}, {'problems': 1}, 'running'),
    ('paused', {'errors': 0}, {'problems': 1}, 'paused'),
    ('stopped', {'errors': 0}, {'problems': 1}, 'stopped'),
    ('interrupted', {'errors': 0}, {'problems': 1}, 'interrupted'),
])
def test_remaining_problems_and_execution_failures_have_distinct_outcomes(status, summary, report, expected):
    assert completion_status(status, summary, report) == expected


def legacy_run(store, *, status='failed', errors=0, problems=1, slug='maintenance-rq'):
    run_id = store.start_run('full', processor=slug)
    store.finish_run(run_id, [], {}, {}, status=status)
    events = [dict(at=1, code='unchanged', message='Нет параметров для замены', title='Пример', requires_manual=True),
              dict(at=2, code='finished', message='Обработка завершена с ошибками', title='')]
    with store.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status=status,
            summary=dump(dict(errors=errors, report_problems=problems, checked=9, proposed=8)),
            report=dump(dict(problems=problems)), events=dump(events)))
    return run_id


def test_legacy_manual_results_are_consistent_in_history_details_exports_and_filters(settings, store):
    issue_id = legacy_run(store)
    failure_id = legacy_run(store, errors=1)
    client = create_app(settings, store).test_client()
    for endpoint in ('/', '/runs?status=success', '/runs/history-fragment?status=success',
                     '/runs/recent-fragment', '/runs/' + issue_id):
        text = client.get(endpoint).get_data(as_text=True)
        assert issue_id in text or endpoint == '/runs/' + issue_id
        assert '<span class="badge success">Завершён' in text
    filtered = client.get('/runs?status=failed').get_data(as_text=True)
    assert failure_id in filtered and issue_id not in filtered
    filtered = client.get('/runs?status=success').get_data(as_text=True)
    assert issue_id in filtered and failure_id not in filtered
    public = client.get('/runs/' + issue_id + '/log.json').get_json()
    assert public['status'] == 'success' and public['events'][0]['tone'] == 'warning'
    assert 'Завершён' in client.get('/runs/' + issue_id + '/log.txt').get_data(as_text=True)
    set_session(client, 'admin')
    text = client.get('/runs/' + issue_id + '/log.txt').get_data(as_text=True)
    assert 'Обработка завершена' in text
    assert 'Обработка завершена с ошибками' not in text
    # The archived evidence is preserved; presentation normalizes old outcomes.
    with store.engine.connect() as conn:
        assert conn.execute(runs.select().where(runs.c.id == issue_id)).mappings().first()['status'] == 'failed'


def test_manual_result_does_not_change_operational_status_or_notify(settings, store):
    legacy_run(store)
    card = next(card for card in build_overview(settings, store)['cards'] if card['task'].slug == 'maintenance-rq')
    assert card['status'] == 'scheduled' and card['status_label'] == 'Запланирована'
    now = time.time()
    prime(store, now)
    system_status(settings, store, now)
    assert not store.list_notifications()


def test_new_completed_manual_run_stores_success_and_keeps_the_report(store):
    run_id = store.start_run('daily', processor='maintenance-rq')
    store.finish_run(run_id, [dict(at=1, code='finished', title='', message='Обработка завершена')],
                     dict(errors=0, report_problems=2), dict(problems=2))
    run = store.run(run_id)
    assert run['status'] == 'success'
    assert json.loads(run['events'])[-1]['message'] == 'Обработка завершена'
    assert store.list_runs(processor='maintenance-rq', status='success')[0]['id'] == run_id
