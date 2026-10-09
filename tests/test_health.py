import time
from datetime import datetime

import pytest

from toolforge_app.health import system_status
from toolforge_app.processors import TASKS
from toolforge_app.web import create_app
from test_web import set_session


def prime(store, now):
    for component in ('executor', 'monitor'):
        store.set_state(component + '_heartbeat', now)
    store.set_state('last_poll', now)
    for task in TASKS[1:]:
        store.set_state(task.slug + ':inventory', {'checked_at': now})


def test_service_health_incident_is_deduplicated_readable_and_recurs(settings, store):
    prime(store, 10000)
    assert system_status(settings, store, 10001)['status'] == 'success'
    store.set_state('executor_heartbeat', 9000)
    assert system_status(settings, store, 10001)['status'] == 'offline'
    entry = store.list_notifications()[0]
    assert entry['active_key'] == 'service:executor'
    store.read_notification(entry['id'], 10002)
    system_status(settings, store, 10003)
    assert len(store.list_notifications()) == 1 and store.unread_notifications() == 0
    store.set_state('executor_heartbeat', 10004)
    system_status(settings, store, 10005)
    assert store.list_notifications()[0]['resolved_at'] == 10005
    store.set_state('executor_heartbeat', 9000)
    system_status(settings, store, 10006)
    assert len(store.list_notifications()) == 2 and store.unread_notifications() == 1


def test_old_backlog_notifications_are_retired_without_discarding_failures(settings, store):
    prime(store, 10000)
    store.sync_notifications({'run:maintenance-rq': dict(title='Замена параметров RQ: осталось проблем',
        message='Ручные исправления', target='/runs')}, 9999)
    system_status(settings, store, 10001)
    row = store.list_notifications()[0]
    assert row['resolved_at'] == 10001 and row['read_at'] == 10001
    assert store.unread_notifications() == 0


def test_paused_and_stopped_service_are_distinct_from_process_failure(settings, store):
    prime(store, 10000)
    for task in TASKS:
        store.change_control(task.slug, 'pause', 'admin')
    assert system_status(settings, store, 10001)['status'] == 'paused'
    for task in TASKS:
        store.change_control(task.slug, 'stop', 'admin')
    store.set_state('executor_heartbeat', 1)
    store.set_state('monitor_heartbeat', 1)
    state = system_status(settings, store, 10002)
    assert state['status'] == 'stopped' and state['active_alerts'] == 0


def test_stale_inventory_and_failed_run_are_reported_without_private_diagnostics(settings, store):
    prime(store, 100000)
    store.set_state('maintenance-dates:inventory', {'checked_at': 1})
    run = store.start_run('daily', 'worker', processor='translations-talk')
    store.finish_run(run, [], {}, {}, status='failed')
    store.set_state('translations-talk:worker_error', {'code': 'SECRET_DIAGNOSTIC'})
    state = system_status(settings, store, 100001)
    entries = store.list_notifications()
    assert 'maintenance-dates' in state['stale']
    assert {row['active_key'] for row in entries} == {'stale:maintenance-dates', 'run:translations-talk'}
    assert 'SECRET_DIAGNOSTIC' not in str(entries)


def test_missed_daily_deadline_does_not_flag_a_task_waiting_for_another_run(settings, store):
    now = datetime(2026, 10, 6, 4, 30, tzinfo=settings.zone).timestamp()
    prime(store, now)
    store.set_state('maintenance-dates:schedule_started_at', now - 86400)
    system_status(settings, store, now)
    assert any(row['active_key'] == 'missed:maintenance-dates' for row in store.list_notifications())
    run = store.start_run('full', 'admin', processor='obkat')
    assert system_status(settings, store, now)['status'] == 'running'
    assert all(row['active_key'] != 'missed:maintenance-dates' for row in store.list_notifications())
    store.finish_run(run, [], {}, {})


@pytest.mark.parametrize('username', [None, 'Other', 'Admin'])
def test_notifications_are_admin_only(settings, store, username):
    client = create_app(settings, store).test_client()
    set_session(client, username)
    assert client.get('/notifications').status_code == 403


def test_notification_read_requires_csrf_and_preview_cannot_mutate(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    set_session(client, 'admin')
    assert client.get('/notifications').status_code == 200
    row = store.list_notifications()[0]
    url = '/admin/notifications/' + row['id'] + '/read'
    assert client.post(url).status_code == 400
    with client.session_transaction() as session:
        session.pop('username')
        session['admin_preview'] = True
    assert client.post(url, data={'csrf': 'csrf-test'}).status_code == 302
    assert not store.list_notifications()[0]['read_at']
    with client.session_transaction() as session:
        session.pop('admin_preview')
        session['username'] = 'admin'
    assert client.post(url, data={'csrf': 'csrf-test'}).status_code == 302
    assert store.list_notifications()[0]['read_at']


def test_background_health_probe_is_independent_from_web_health(settings, store):
    client = create_app(settings, store).test_client()
    assert client.get('/healthz').status_code == 200
    assert client.get('/healthz/workers/executor').status_code == 503
    store.set_state('executor_heartbeat', time.time())
    assert client.get('/healthz/workers/executor').status_code == 200
    assert client.get('/healthz/workers/unknown').status_code == 404
