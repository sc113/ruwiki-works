import time
from unittest.mock import Mock, patch

import pytest

from toolforge_app.connections import record_check
from toolforge_app.dispatcher import Dispatcher, Monitor
from toolforge_app.health import component_health, system_status
from toolforge_app.overview import build_overview
from toolforge_app.processors.daily_worker import Controlled, DailyWorker
from toolforge_app.runtime import ObservationPaused, execution_settings, service_enabled, writes_enabled
from toolforge_app.web import create_app
from toolforge_app.wiki import WikiClient, WikiError
from toolforge_app.worker import RunControlled, Worker
from test_web import set_session


@pytest.mark.parametrize('slug', ['obkat', 'maintenance-dates'])
def test_running_pass_keeps_its_actual_mode_when_write_switch_changes(settings, store, slug):
    settings.wiki_write = True
    run_id = store.start_run('full', 'admin', dry_run=True, processor=slug)
    card = next(card for card in build_overview(settings, store)['cards'] if card['task'].slug == slug)
    assert card['running'] and card['dry_run']
    store.finish_run(run_id, [], {}, {}, status='success')
    card = next(card for card in build_overview(settings, store)['cards'] if card['task'].slug == slug)
    assert not card['running'] and not card['dry_run']


def test_paused_executor_stays_online_and_leaves_queue_intact(settings, store):
    store.set_state('service:executor', {'enabled': False})
    store.enqueue('manual', 'full', 1)
    workers = [Mock() for _ in range(8)]
    dispatcher = Dispatcher(settings, store, obkat=workers[0], maintenance=workers[1:4],
                            translations=workers[4:6], sections=workers[6:8])
    assert dispatcher.tick()
    assert component_health(settings, store, 'executor')['online']
    assert not component_health(settings, store, 'executor')['enabled']
    assert system_status(settings, store)['status'] == 'paused'
    for worker in workers:
        worker.tick.assert_not_called()
        worker.schedule.assert_not_called()
    assert len(store.queue()) == 1
    assert not any(row['active_key'] == 'service:executor' for row in store.list_notifications())


def test_explicit_restart_runs_while_automatic_executor_is_off(settings, store, wiki):
    store.set_state('service:executor', {'enabled': False, 'at': time.time() - 1})
    store.change_control('obkat', 'restart', 'admin')
    job = store.queue()[0]
    receipt = store.run_request(job_key=job['key'])
    assert receipt and receipt['run_id'] is None
    Worker(settings, store, wiki).tick()
    run = store.list_runs()[0]
    assert run['status'] == 'success' and run['dry_run']
    assert store.run_request(receipt['id'])['run_id'] == run['id']
    assert not store.queue() and not service_enabled(store, 'executor')


def test_disabled_monitor_sends_heartbeat_without_network_or_scheduling(settings, store):
    store.set_state('service:monitor', {'enabled': False})
    monitor = Monitor(settings, store)
    with patch.object(monitor.obkat, 'watch') as watch, patch.object(monitor.inventory, 'tick') as inventory:
        monitor.tick()
    watch.assert_not_called()
    inventory.assert_not_called()
    assert component_health(settings, store, 'monitor')['online']
    assert not store.queue()


def test_monitor_switch_interrupts_observation_without_recording_failure(settings, store):
    monitor = Monitor(settings, store)
    def disable(now):
        store.set_state('service:monitor', {'enabled': False})
        monitor.obkat.outer_renew()
    with patch.object(monitor.obkat, 'watch', side_effect=disable):
        with pytest.raises(ObservationPaused):
            monitor.tick()
    assert component_health(settings, store, 'monitor')['online']
    assert not store.get_state('worker_error')


def test_global_pause_finishes_current_run_as_paused_and_preserves_job(settings, store, wiki):
    store.enqueue('page:test', 'page', 1, title=wiki.title)
    worker = Worker(settings, store, wiki)
    event = worker.event
    def pause(code, message, *args, **kwargs):
        event(code, message, *args, **kwargs)
        if code == 'started':
            store.set_state('service:executor', {'enabled': False})
    worker.event = pause
    worker.execute(store.queue()[0])
    assert store.list_runs()[0]['status'] == 'paused'
    assert len(store.queue()) == 1 and not wiki.edits


def test_global_pause_interrupts_active_workers_at_next_checkpoint(settings, store):
    store.set_state('service:executor', {'enabled': False})
    obkat = Worker(settings, store)
    obkat.execution_generation = store.control()['generation']
    with pytest.raises(RunControlled, match='paused'):
        obkat.check_control()
    daily = DailyWorker(settings, store, 'maintenance-dates')
    daily.generation = store.control('maintenance-dates')['generation']
    with pytest.raises(Controlled, match='paused'):
        daily.checkpoint()


def test_write_switch_has_server_ceiling_and_blocks_existing_authenticated_session(settings, store):
    store.set_state('service:writes', {'enabled': True})
    assert not writes_enabled(settings, store)
    settings.wiki_write = True
    client = WikiClient(settings, store)
    client.logged_in = True
    store.set_state('service:writes', {'enabled': False})
    assert not execution_settings(settings, store).wiki_write
    with patch.object(client, 'request') as request:
        with pytest.raises(WikiError, match='writes-disabled'):
            client.login()
        request.assert_not_called()
    assert settings.wiki_write is True


def test_overview_shows_global_pause_and_effective_write_mode(settings, store):
    settings.wiki_write = True
    store.set_state('service:executor', {'enabled': False})
    store.set_state('service:writes', {'enabled': False})
    overview = build_overview(settings, store)
    assert all(card['status'] == 'paused' and card['dry_run'] for card in overview['cards'])


@pytest.mark.parametrize('username', [None, 'Other', 'Admin'])
def test_runtime_switches_require_exact_admin(settings, store, username):
    client = create_app(settings, store).test_client()
    set_session(client, username)
    for component in ('executor', 'monitor', 'writes'):
        assert client.post('/admin/services/' + component,
            data={'csrf': 'csrf-test', 'action': 'disable'}).status_code == 403
        assert service_enabled(store, component)


def test_runtime_switches_have_csrf_and_do_not_change_task_controls(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    store.set_state('executor_heartbeat', time.time())
    store.enqueue('manual', 'full', time.time())
    assert client.get('/admin/services/executor').status_code == 405
    assert client.post('/admin/services/executor', data={'action': 'disable'}).status_code == 400
    assert client.post('/admin/services/executor', data={'csrf': 'csrf-test', 'action': 'disable'}).status_code == 302
    assert not service_enabled(store, 'executor') and len(store.queue()) == 1
    assert store.control()['mode'] == 'active'
    assert client.post('/admin/services/executor', data={'csrf': 'csrf-test', 'action': 'enable'}).status_code == 302
    assert service_enabled(store, 'executor')


def test_enable_rejects_dead_process_and_server_write_prohibition(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    assert client.post('/admin/services/executor', data={'csrf': 'csrf-test', 'action': 'enable'}).status_code == 409
    assert client.post('/admin/services/writes', data={'csrf': 'csrf-test', 'action': 'enable'}).status_code == 409
    assert settings.wiki_write is False


def test_writes_can_only_be_enabled_after_matching_connection_verification(settings, store):
    settings.wiki_write = True
    settings.bot_username, settings.bot_login, settings.bot_password = 'Bot', 'Bot@app', 'password'
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    assert client.post('/admin/services/writes', data={'csrf': 'csrf-test', 'action': 'enable'}).status_code == 409
    record_check(settings, store, 'bot', {'verified': True, 'code': ''})
    assert client.post('/admin/services/writes', data={'csrf': 'csrf-test', 'action': 'enable'}).status_code == 302
    assert writes_enabled(settings, store)
    assert client.post('/admin/services/writes', data={'csrf': 'csrf-test', 'action': 'disable'}).status_code == 302
    assert not writes_enabled(settings, store)


def test_admin_page_contains_all_statuses_and_task_actions(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    html = client.get('/admin').get_data(as_text=True)
    for phrase in ('Сайт', 'База данных', 'Исполнитель', 'Монитор', 'Запись в Википедию',
                   'Очередь задач', 'Вход admin через Wikimedia OAuth', 'Учётная запись бота'):
        assert phrase in html
    assert 'action="/admin/services/monitor"' in html
    assert 'action="/admin/services/executor"' in html
    assert 'id="tasks"' in html
    assert 'value="connections"' in html
    result = client.post('/admin/tasks/obkat/control', data={
        'csrf': 'csrf-test', 'action': 'pause', 'return_to': 'connections'})
    assert result.location.endswith('/admin/connections#tasks')


def test_live_admin_status_is_private_and_reflects_switch_changes(settings, store):
    client = create_app(settings, store).test_client()
    assert client.get('/admin/status-fragment').status_code == 403
    set_session(client, 'admin')
    store.set_state('service:executor', {'enabled': False})
    body = client.get('/admin/status-fragment').get_data(as_text=True)
    assert 'Обработка выключена' in body and 'value="enable"' in body
    assert '<html' not in body and 'bot_password' not in body and 'oauth_secret' not in body


def test_preview_cannot_operate_services(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    with client.session_transaction() as session:
        session.update(admin_preview=True, csrf='test-csrf')
    assert client.post('/admin/services/monitor', data={'csrf': 'test-csrf', 'action': 'disable'}).status_code == 302
    assert service_enabled(store, 'monitor')
