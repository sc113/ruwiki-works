import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event

import pytest
from sqlalchemy import update

from toolforge_app.console import console_data
from toolforge_app.dispatcher import Dispatcher
from toolforge_app.execution import ready_jobs
from toolforge_app.overview import build_overview
from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.storage import Store, leases
from toolforge_app.web import create_app
from toolforge_app.worker import Worker
from test_maintenance import MaintenanceWiki, configure, queued
from test_web import set_session


DATES, RQ, UNWRAP = 'maintenance-dates', 'maintenance-rq', 'maintenance-rq-unwrap'


def workers(settings, store, wiki):
    configure(store, DATES)
    configure(store, RQ)
    return Worker(settings, store, wiki), [
        MaintenanceWorker(settings, store, DATES, MaintenanceWiki()),
        MaintenanceWorker(settings, store, RQ, MaintenanceWiki(['Текст', '{{rq|check}}'])),
        MaintenanceWorker(settings, store, UNWRAP, MaintenanceWiki(['{{rq|{{Факты|дата=2020-01-01}}}}']))]


def test_task_workers_cannot_overtake_higher_actions(settings, store, wiki):
    obkat, actions = workers(settings, store, wiki)
    # Request in reverse order: processing must still follow the website.
    jobs = {slug: queued(store, slug) for slug in [UNWRAP, RQ, DATES]}
    store.enqueue('manual:normal', 'full', time.time() - 1)
    jobs['obkat'] = store.queue()[0]
    for action in reversed(actions):
        action.execute(jobs[action.slug])
        assert not action.wiki.calls
    assert store.list_runs(processor=None) == []
    obkat.execute(jobs['obkat'])
    for action in actions:
        action.execute(jobs[action.slug])
    runs = sorted(store.list_runs(processor=None), key=lambda run: run['started_at'])
    assert [run['processor'] for run in runs] == ['obkat', DATES, RQ, UNWRAP]
    assert all(run['status'] == 'success' for run in runs)
    assert all(previous['finished_at'] <= following['started_at'] for previous, following in zip(runs, runs[1:]))


def test_dispatcher_drains_actions_in_order_without_overlapping(settings, store, wiki):
    obkat, actions = workers(settings, store, wiki)
    store.set_state('live_sync_initialized', True)
    store.set_state('last_reconcile', time.time())
    for slug in [UNWRAP, RQ, DATES, 'obkat']:
        queued(store, slug)
    assert Dispatcher(settings, store, obkat=obkat, maintenance=actions, translations=[], sections=[]).tick()
    runs = sorted(store.list_runs(processor=None), key=lambda run: run['started_at'])
    assert [run['processor'] for run in runs] == ['obkat', DATES, RQ, UNWRAP]
    assert all(previous['finished_at'] <= following['started_at'] for previous, following in zip(runs, runs[1:]))
    assert not ready_jobs(store)
    assert all(any(job['kind'] == 'daily' for job in store.queue(slug)) for slug in [DATES, RQ, UNWRAP])


def test_paused_and_future_actions_do_not_block_ready_jobs(settings, store):
    future = time.time() + 3600
    store.enqueue('later', 'full', future)
    queued(store, DATES)
    second = queued(store, RQ)
    store.change_control(DATES, 'pause', 'admin')
    assert ready_jobs(store) == [second]
    store.change_control(DATES, 'resume', 'admin')
    assert [job['processor'] for job in ready_jobs(store)] == [DATES, RQ]
    store.change_control(DATES, 'stop', 'admin')
    assert ready_jobs(store) == [second]
    store.change_control(RQ, 'stop', 'admin')
    assert ready_jobs(store) == []


def test_global_slot_blocks_another_process_but_not_edit_observation(settings, store, wiki):
    configure(store, DATES)
    configure(store, RQ)
    first_wiki = MaintenanceWiki()
    first = MaintenanceWorker(settings, store, DATES, first_wiki)
    first_job, second_job = queued(store, DATES), queued(store, RQ)
    entered, release = Event(), Event()
    original = first_wiki.category_members

    def block(*args, **kwargs):
        if not entered.is_set():
            first.event('article', 'Внутренняя диагностика', title='Текущая статья')
            entered.set()
            assert release.wait(10), 'Test did not release the first action'
        return original(*args, **kwargs)

    first_wiki.category_members = block
    another_store = Store(settings.database_url)
    other_wiki = MaintenanceWiki(['Текст', '{{rq|check}}'])
    second = MaintenanceWorker(settings, another_store, RQ, other_wiki)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.execute, first_job)
            try:
                assert entered.wait(10)
                second.execute(second_job)
                assert not other_wiki.calls
                assert len(store.list_runs(processor=None)) == 1
                cards = {card['task'].slug: card for card in build_overview(settings, store)['cards']}
                assert cards[DATES]['status'] == 'running'
                assert cards[RQ]['status'] == 'queued' and cards[RQ]['queue_position'] == 2
                assert ready_jobs(store, exclude_running=True) == [second_job]
                # A separate observer reads edits while the execution lease is held.
                store.set_state('last_reconcile', time.time())
                now = time.time()
                wiki.recent = [{'title': wiki.title, 'revid': 11,
                                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}]
                assert Worker(settings, another_store, wiki).watch(now)
                page_job = store.queue()[0]
                assert page_job['revision'] == 11 and page_job['due_at'] > now + settings.quiet_minutes * 60 - 1
                client = create_app(settings, store).test_client()
                public = client.get(f'/tasks/{DATES}/status-fragment').get_data(as_text=True)
                assert 'Обработка идёт' in public and 'Текущая статья' not in public
                set_session(client, 'admin')
                admin = client.get(f'/tasks/{DATES}/status-fragment').get_data(as_text=True)
                assert 'Текущая статья' in admin and f'/runs/{first.run_id}' in admin
                feed = console_data(store)
                assert len(feed['running']) == 1 and feed['waiting'][0]['task'].slug == RQ
            finally:
                release.set()
            future.result(timeout=10)
        second.execute(second_job)
        assert other_wiki.calls
        assert all(run['status'] == 'success' for run in store.list_runs(processor=None))
        assert not console_data(store)['running']
    finally:
        another_store.engine.dispose()


def test_lease_loss_stops_processing_before_wiki_save(settings, store):
    settings.wiki_write = True
    configure(store, DATES)
    wiki = MaintenanceWiki()
    worker = MaintenanceWorker(settings, store, DATES, wiki)
    original = wiki.historical_text
    stolen = False
    with ExitStack() as contexts:
        def steal(*args, **kwargs):
            nonlocal stolen
            text = original(*args, **kwargs)
            if not stolen:
                with store.engine.begin() as conn:
                    conn.execute(update(leases).where(leases.c.key == 'execution').values(expires_at=0))
                assert contexts.enter_context(store.worker_lease(processor='execution'))
                stolen = True
            return text
        wiki.historical_text = steal
        worker.execute(queued(store, DATES))
        assert stolen and not wiki.edits
        assert store.list_runs(processor=DATES)[0]['status'] == 'failed'
        # Cleanup by the previous owner must not release the replacement lease.
        assert store.lease_active('execution')
    assert not store.lease_active('execution')


@pytest.mark.parametrize('username', [None, 'Another user', 'Admin'])
def test_unified_console_is_public_but_hides_diagnostics_on_both_endpoints(settings, store, username):
    run_id = store.start_run('daily', processor=DATES)
    store.finish_run(run_id, [dict(at=time.time(), code='debug', message='PRIVATE_DIAGNOSTICS')],
                     dict(checked=12, changed=0, skipped=2, errors=1), {})
    client = create_app(settings, store).test_client()
    set_session(client, username)
    for path in ('/runs', '/runs/history-fragment', '/journal', '/journal/fragment'):
        response = client.get(path)
        assert response.status_code == 200
        assert 'PRIVATE_DIAGNOSTICS' not in response.get_data(as_text=True)
        if path.startswith('/runs'):
            assert run_id in response.get_data(as_text=True)
    assert 'Работы бота' in client.get('/').get_data(as_text=True)


def test_console_collects_all_actions_and_filter_keeps_run_links(settings, store):
    for number, slug in enumerate([DATES, RQ]):
        run = store.start_run('full', processor=slug)
        event = dict(at=time.time() + number, code='debug', message=f'Подробности {number}', title='Пример')
        store.finish_run(run, [event], {}, {})
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    full = client.get('/console').get_data(as_text=True)
    assert 'Подробности 0' in full and 'Подробности 1' in full
    filtered = client.get('/console/fragment?task=' + RQ).get_data(as_text=True)
    assert 'Подробности 0' not in filtered and 'Подробности 1' in filtered
    assert '/runs/' + store.list_runs(processor=RQ)[0]['id'] in filtered
    assert client.get('/console?task=missing').status_code == 400


def test_parameters_are_initially_closed_for_admin_but_all_fields_are_available(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    page = client.get('/processors/maintenance?task=' + DATES).get_data(as_text=True)
    opening = page.split('<details id="settings"', 1)[1].split('>', 1)[0]
    assert 'open' not in opening
    assert 'settings-group' in page and 'name="section_templates"' in page
    assert 'name="run_time"' in page and 'name="search_mode"' in page
