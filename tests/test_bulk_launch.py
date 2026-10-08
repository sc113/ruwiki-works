import time
from datetime import datetime
from unittest.mock import patch

import pytest

from toolforge_app.execution import ready_jobs
from toolforge_app.processors import TASKS
from toolforge_app.web import create_app
from test_web import set_session


def test_bulk_launch_is_serial_ordered_deduplicated_and_keeps_disabled_tasks(settings, store):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    store.change_control('translations-talk', 'pause', 'admin')
    store.change_control('sections-fill-to-empty', 'stop', 'admin')
    store.record_article_checks('translations-categories', [dict(title='Проверена', outcome='unchanged',
        reason='Нет источника', checked_at=1, run_id='')])
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    for _ in range(2):
        result = client.post('/admin/tasks/run-all', data={'csrf':'csrf-test'})
        assert result.status_code == 302 and result.location == '/journal'
    expected = [task.slug for task in TASKS if task.slug not in {'translations-talk','sections-fill-to-empty'}]
    assert [job['processor'] for job in ready_jobs(store)] == expected
    assert len(store.queue(None)) == 6 and len(store.pending_run_requests()) == 6
    assert all(job['kind'] == 'full' and not job['spacing'] for job in store.queue(None))
    assert 'Проверена' in store.checked_articles('translations-categories')
    assert store.control('translations-talk')['mode'] == 'paused'
    assert store.control('sections-fill-to-empty')['mode'] == 'stopped'
    assert not settings.wiki_write


def test_bulk_request_rolls_back_if_queueing_fails(store):
    original = store._request_run
    def fail(conn, processor, requested_by, **kwargs):
        if processor == 'maintenance-dates':
            raise RuntimeError('database unavailable')
        return original(conn, processor, requested_by, **kwargs)
    with patch.object(store, '_request_run', side_effect=fail):
        with pytest.raises(RuntimeError):
            store.request_runs(['obkat','maintenance-dates'], 'admin')
    assert not store.queue(None) and not store.pending_run_requests()


def test_bulk_button_and_endpoint_require_admin_and_csrf(settings, store):
    client = create_app(settings, store).test_client()
    assert 'bulk-launch' not in client.get('/').get_data(as_text=True)
    assert client.post('/admin/tasks/run-all').status_code == 400
    set_session(client, 'Other')
    assert client.post('/admin/tasks/run-all', data={'csrf':'csrf-test'}).status_code == 403
    set_session(client, 'admin')
    html = client.get('/').get_data(as_text=True)
    assert 'Проверить все' in html and 'Перезапустить' not in html
    assert client.post('/admin/tasks/obkat/run',data={'csrf':'csrf-test'}).status_code == 302


def test_log_rows_and_exports_include_moscow_date_and_milliseconds(settings, store):
    at = datetime(2026,10,9,0,0,0,123000,tzinfo=settings.zone).timestamp()
    rid = store.start_run('full')
    store.finish_run(rid,[dict(at=at,code='edited',message='Обработано',title='Статья')],{}, {})
    client = create_app(settings,store).test_client()
    for admin in (False,True):
        if admin: set_session(client,'admin')
        for path in ('/journal','/runs/'+rid+'/fragment','/runs/'+rid+'/log.txt'):
            assert '09.10.2026 00:00:00.123' in client.get(path).get_data(as_text=True)
