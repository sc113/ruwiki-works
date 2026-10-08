import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from toolforge_app.dispatcher import Dispatcher
from toolforge_app.execution import ready_jobs
from toolforge_app.overview import build_overview
from toolforge_app.processors.logs import public_run
from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.runtime import job_enabled
from toolforge_app.storage import Store
from toolforge_app.web import create_app
from toolforge_app.worker import Worker
from test_maintenance import MaintenanceWiki, configure
from test_web import set_session


def client_for(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    return client


@pytest.mark.parametrize('slug', ['obkat', 'maintenance-dates', 'translations-categories', 'sections-empty-to-fill'])
def test_manual_click_opens_stable_live_request_and_coalesces(settings, store, slug):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    client = client_for(settings, store)
    endpoint = '/admin/obkat/run' if slug == 'obkat' else '/admin/tasks/' + slug + '/run'
    first = client.post(endpoint, data={'csrf': 'csrf-test'})
    second = client.post(endpoint, data={'csrf': 'csrf-test'})
    assert first.status_code == 302 and '/launches/' in first.location
    assert first.location == second.location
    assert len(store.queue(slug)) == 1
    assert 'live-launch' in client.get(first.location).get_data(as_text=True)
    assert job_enabled(store, store.queue(slug)[0])
    assert not settings.wiki_write


def test_obkat_manual_scan_ignores_quiet_timer_and_links_actual_run(settings, store, wiki):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    store.enqueue('page:old', 'page', time.time()+900, title=wiki.title, revision=10)
    client = client_for(settings, store)
    response = client.post('/admin/obkat/run', data={'csrf': 'csrf-test'})
    receipt = store.run_request(response.location.rsplit('/', 1)[-1])
    worker = Worker(settings, store, wiki)
    with patch.object(worker, 'watch', side_effect=AssertionError('Must not wait for observation')):
        worker.tick()
    run = store.list_runs()[0]
    assert run['kind'] == 'full' and run['status'] == 'success' and run['dry_run']
    assert not wiki.edits
    assert store.run_request(receipt['id'])['run_id'] == run['id']
    assert client.get(response.location).location.endswith('/runs/' + run['id'])
    fragment = client.get(response.location + '/fragment').get_data(as_text=True)
    assert 'data-run-url="/runs/' + run['id'] in fragment
    assert any(e['code'] == 'scan' for e in json.loads(run['events']))
    assert 'Подробности' in client.get('/runs/' + run['id'] + '/fragment').get_data(as_text=True)


def test_requested_action_is_prioritized_and_scans_with_automatic_executor_off(settings, store, wiki):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    store.enqueue('older', 'full', 1)
    configure(store, 'maintenance-dates')
    receipt = store.request_run('maintenance-dates', 'admin')
    obkat = Worker(settings, store, wiki)
    action = MaintenanceWorker(settings, store, 'maintenance-dates', MaintenanceWiki())
    with patch.object(obkat, 'tick', side_effect=AssertionError('Unrequested work must stay off')):
        Dispatcher(settings, store, obkat=obkat, maintenance=[action], translations=[], sections=[]).tick()
    run = store.list_runs(processor='maintenance-dates')[0]
    assert run['status'] == 'success' and run['dry_run']
    assert not action.wiki.edits and action.wiki.calls
    assert store.run_request(receipt['id'])['run_id'] == run['id']
    assert store.queue()[0]['key'] == 'older'


def test_later_global_disable_interrupts_requested_run_and_new_click_gets_new_journal(settings, store, wiki):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    receipt = store.request_run('obkat', 'admin')
    worker = Worker(settings, store, wiki)
    original = worker.event
    def disable(code, message, *args, **kwargs):
        original(code, message, *args, **kwargs)
        if code == 'started':
            store.set_state('service:executor', dict(enabled=False, at=time.time()))
    worker.event = disable
    worker.execute(store.queue()[0])
    assert store.list_runs()[0]['status'] == 'paused'
    assert not ready_jobs(store)
    another = store.request_run('obkat', 'admin')
    assert another['id'] != receipt['id'] and another['run_id'] is None
    assert len(store.queue()) == 1 and ready_jobs(store)


def test_concurrent_clicks_share_one_request(settings, store):
    other = Store(settings.database_url)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda s: s.request_run('maintenance-rq', 'admin'), [store, other]))
        assert receipts[0]['id'] == receipts[1]['id']
        assert len(store.queue('maintenance-rq')) == 1
    finally:
        other.engine.dispose()


def test_public_projection_is_one_safe_row_per_page_for_every_processor(settings, store):
    for slug in ['obkat', 'maintenance-dates', 'translations-talk', 'sections-empty-to-fill']:
        rid = store.start_run('full', processor=slug)
        events = [dict(at=1, code='started', message='PRIVATE', configuration={'private': 'SECRET'}),
                  dict(at=2, code='page', message='PRIVATE', title='Страница'),
                  dict(at=3, code='history', message='PRIVATE', title='Страница', diff_before='SECRET'),
                  dict(at=4, code='edited', message='PRIVATE', title='Страница', revision=12, diff_before='SECRET'),
                  dict(at=5, code='error', message='PRIVATE', title='Другая', error='SECRET')]
        store.finish_run(rid, events, {}, {})
        client = create_app(settings, store).test_client()
        for path in ['/runs/'+rid+'/fragment', '/runs/'+rid+'/log.txt', '/runs/'+rid+'/log.json']:
            text = client.get(path).get_data(as_text=True)
            assert 'PRIVATE' not in text and 'SECRET' not in text
        rows = client.get('/runs/'+rid+'/log.json').get_json()['events']
        assert [row['title'] for row in rows] == ['Страница','Другая']
        assert rows[0]['tone'] == 'success' and rows[1]['tone'] == 'error'
        set_session(client,'admin')
        assert 'PRIVATE' in client.get('/runs/'+rid+'/fragment').get_data(as_text=True)
        assert 'SECRET' in client.get('/runs/'+rid+'/events/3').get_data(as_text=True)


def test_active_manual_run_remains_visible_while_automatic_processing_off(settings, store):
    store.set_state('service:executor', dict(enabled=False, at=time.time()-10))
    receipt = store.request_run('obkat', 'admin')
    rid = store.start_run('full', job=store.queue()[0])
    cards = build_overview(settings, store)['cards']
    assert next(card for card in cards if card['task'].slug == 'obkat')['status'] == 'running'
    assert store.run_request(receipt['id'])['run_id'] == rid


def test_notifications_read_all_preserves_incidents_and_requires_auth_and_csrf(settings, store):
    store.sync_notifications({'one':dict(title='Проблема',message='Описание',target='/'),
                              'two':dict(title='Проблема2',message='Описание',target='/')}, time.time())
    original_ids = {row['id'] for row in store.list_notifications()}
    client = create_app(settings,store).test_client()
    assert client.post('/admin/notifications/read-all',data={'csrf':'x'}).status_code == 400
    set_session(client,'Other')
    assert client.post('/admin/notifications/read-all',data={'csrf':'csrf-test'}).status_code == 403
    set_session(client,'admin')
    assert client.post('/admin/notifications/read-all',data={'csrf':'csrf-test'}).status_code == 302
    assert store.unread_notifications() == 0
    assert original_ids <= {row['id'] for row in store.list_notifications()}


def test_obkat_queue_groups_months_in_one_row(settings, store):
    for index in range(3):
        store.enqueue('page:' + str(index), 'page', time.time()+900+index, title='Википедия:Обсуждение категорий/2026-0' + str(index+1))
    html = create_app(settings,store).test_client().get('/processors/obkat').get_data(as_text=True)
    queue = html.split('<ul id="queue"')[1].split('</ul>')[0]
    assert queue.count('<li>') == 1 and '3 страниц' in queue
    assert '2026-01' not in queue and '2026-02' not in queue


def test_journal_paginates_every_event_and_filters_midnight_by_event_time(settings, store):
    from datetime import datetime
    from sqlalchemy import update
    from toolforge_app.storage import runs
    midnight = datetime(2026,10,8,tzinfo=settings.zone).timestamp()
    rid = store.start_run('full')
    events = [dict(at=midnight-5+index,code='edited',title='Страница'+str(index),message='Обработано') for index in range(301)]
    store.finish_run(rid,events,{}, {})
    with store.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == rid).values(started_at=midnight-10, finished_at=midnight+400))
    client = create_app(settings,store).test_client()
    first = client.get('/journal?day=2026-10-08').get_data(as_text=True)
    second = client.get('/journal?day=2026-10-08&page=2').get_data(as_text=True)
    assert 'Страница300</a>' in first and 'Страница5</a>' in second
    assert 'Страница0</a>' not in first+second
    assert '08.10.2026' in first and '00:00:00' in second
    previous = client.get('/runs?day=2026-10-07').get_data(as_text=True)
    assert rid in previous
    assert rid not in client.get('/runs?day=2026-10-08').get_data(as_text=True)


def test_profile_menu_and_split_activity_are_role_aware(settings, store):
    client = create_app(settings,store).test_client()
    assert 'Настройки профиля' not in client.get('/').get_data(as_text=True)
    set_session(client,'admin')
    html = client.get('/').get_data(as_text=True)
    assert 'profile-menu' in html and '/admin/profile' in html
    assert 'Запуски и консоль' not in html
    assert client.get('/admin/profile').status_code == 200
    assert 'console-output' not in client.get('/runs').get_data(as_text=True)
    assert '<table class="runs-table"' not in client.get('/journal').get_data(as_text=True)
