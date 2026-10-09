import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from toolforge_app.overview import build_overview
from toolforge_app.schedules import get_schedule, month_end_deadline, save_schedule
from toolforge_app.storage import Store
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiError
from toolforge_app.worker import Worker
from test_web import set_session


def update_obkat(settings, store, minutes=35, clock='22:15'):
    return save_schedule(settings, store, 'obkat',
                         {'quiet_minutes': str(minutes), 'month_end_time': clock}, 'admin')


def test_shared_editor_saves_both_obkat_timers_and_public_values(settings, store):
    store.set_state('obkat:config', {'other': 'preserved'})
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    response = client.post('/admin/tasks/obkat/schedule', data={
        'csrf': 'csrf-test', 'quiet_minutes': '35', 'month_end_time': '22:15'})
    assert response.location.endswith('/processors/obkat')
    assert get_schedule(settings, store, 'obkat') == {'quiet_minutes': 35, 'month_end_time': '22:15', 'search_minutes': 5}
    assert store.get_state('obkat:config')['other'] == 'preserved'
    html = client.get(response.location).get_data(as_text=True)
    assert 'name="quiet_minutes" value="35"' in html and 'name="month_end_time" value="22:15"' in html
    assert '/admin/tasks/obkat/schedule' in html
    with client.session_transaction() as session:
        session.pop('username')
    html = client.get(response.location).get_data(as_text=True)
    assert 'Через 35 минут' in html and '22:15 МСК' in html
    assert 'name="quiet_minutes"' not in html
    now = datetime(2026, 10, 6, tzinfo=settings.zone).timestamp()
    card = build_overview(settings, store, now)['cards'][0]
    assert card['month_end'] == month_end_deadline('2026-10', '22:15', settings.zone)


@pytest.mark.parametrize('values', [
    {'quiet_minutes': '0', 'month_end_time': '22:15'},
    {'quiet_minutes': '1441', 'month_end_time': '22:15'},
    {'quiet_minutes': '2.5', 'month_end_time': '22:15'},
    {'quiet_minutes': '25', 'month_end_time': '24:00'},
    {'quiet_minutes': '25'},
    {'quiet_minutes': '25', 'month_end_time': '22:15', 'extra': 'x'},
])
def test_schedule_validation_is_atomic(settings, store, values):
    update_obkat(settings, store)
    store.clear_state_if('obkat:retime_requested', store.get_state('obkat:retime_requested'))
    store.enqueue('page:test', 'page', time.time() + 60, revision=1)
    before_config, before_queue = store.get_state('obkat:config'), store.queue()
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    client.post('/admin/tasks/obkat/schedule', data=dict(values, csrf='csrf-test'))
    assert store.get_state('obkat:config') == before_config and store.queue() == before_queue
    assert store.get_state('obkat:retime_requested') is None


@pytest.mark.parametrize('username', [None, 'OtherUser', 'Admin'])
def test_shared_schedule_requires_exact_admin(settings, store, username):
    client = create_app(settings, store).test_client()
    set_session(client, username)
    assert client.post('/admin/tasks/obkat/schedule', data={
        'csrf': 'csrf-test', 'quiet_minutes': '35', 'month_end_time': '22:15'}).status_code == 403
    assert store.get_state('obkat:config') is None


def test_shared_schedule_csrf_and_local_preview(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    set_session(client, 'admin')
    assert client.post('/admin/tasks/obkat/schedule', data={
        'quiet_minutes': '35', 'month_end_time': '22:15'}).status_code == 400
    with client.session_transaction() as session:
        session.pop('username')
        session['admin_preview'] = True
    response = client.post('/admin/tasks/obkat/schedule', data={
        'csrf': 'csrf-test', 'quiet_minutes': '35', 'month_end_time': '22:15'})
    assert response.location.endswith('/processors/obkat')
    assert store.get_state('obkat:config') is None


@pytest.mark.parametrize('slug', ['maintenance-dates', 'maintenance-rq', 'maintenance-rq-unwrap'])
def test_daily_actions_use_the_same_endpoint(settings, store, slug):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    response = client.post('/admin/tasks/' + slug + '/schedule',
                           data={'csrf': 'csrf-test', 'run_time': '04:15'})
    assert response.status_code == 302
    assert get_schedule(settings, store, slug) == {'run_time': '04:15', 'search_minutes': 360}
    assert '/admin/tasks/' + slug + '/schedule' in client.get(response.location).get_data(as_text=True)


def test_observer_and_execution_use_changed_quiet_period(settings, store, wiki):
    now = time.time()
    update_obkat(settings, store, 45)
    wiki.data[wiki.title] = Revision(wiki.title, 10, now - 1500, wiki.data[wiki.title].text)
    worker = Worker(settings, store, wiki)
    worker.observe(wiki.title, 10, now - 1500, now)
    assert store.queue()[0]['due_at'] == now + 1200
    settings.wiki_write = True
    worker.process_page(wiki.title, respect_quiet=True)
    assert wiki.edits == [] and worker.events[-1]['code'] == 'deferred'
    assert store.page(wiki.title) is None


@pytest.mark.parametrize('minutes,expected', [(5, 0), (40, 1800)])
def test_retime_pending_pages_from_actual_edit_time(settings, store, wiki, minutes, expected):
    now = time.time()
    edited = now - 600
    wiki.data[wiki.title] = Revision(wiki.title, 10, edited, wiki.data[wiki.title].text)
    worker = Worker(settings, store, wiki)
    worker.observe(wiki.title, 10, edited, now)
    update_obkat(settings, store, minutes)
    worker.retime_pages(now)
    job = store.queue()[0]
    assert job['due_at'] == now + expected and job['revision'] == 10
    assert store.get_state('obkat:retime_requested') is None and not wiki.edits


def test_retime_preserves_retry_and_new_revision(settings, store, wiki):
    now = time.time()
    worker = Worker(settings, store, wiki)
    worker.observe(wiki.title, 10, now, now)
    old = store.queue()[0]
    # A newer observation arrives while metadata for the previous revision is read.
    def revisions(titles):
        worker.observe(wiki.title, 11, now + 30, now + 30)
        return [Revision(wiki.title, 10, now, wiki.data[wiki.title].text)]
    wiki.revisions = revisions
    update_obkat(settings, store, 35)
    worker.retime_pages(now)
    newer = store.queue()[0]
    assert newer['revision'] == 11 and newer['due_at'] == now + 30 + 2100
    store.retry(newer, now)
    retry = store.queue()[0]
    update_obkat(settings, store, 5)
    worker.retime_pages(now)
    assert store.queue()[0] == retry
    store.move_job(old, now)
    assert store.queue()[0] == retry


def test_retime_failure_keeps_request_and_a_new_request_is_not_cleared(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    now = time.time()
    worker.observe(wiki.title, 10, now, now)
    update_obkat(settings, store)
    request = store.get_state('obkat:retime_requested')
    def fail(titles):
        raise WikiError('network')
    wiki.revisions = fail
    with pytest.raises(WikiError):
        worker.retime_pages(now)
    assert store.get_state('obkat:retime_requested') == request
    def change_again(titles):
        update_obkat(settings, store, 50)
        return [Revision(wiki.title, 10, now, wiki.data[wiki.title].text)]
    wiki.revisions = change_again
    worker.retime_pages(now)
    assert store.get_state('obkat:retime_requested') not in {None, request}


def test_month_end_hot_time_changes_and_completion_guard(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    now = datetime(2026, 10, 31, 22, 30, tzinfo=settings.zone).timestamp()
    update_obkat(settings, store, clock='22:15')
    worker.schedule_month_end(now)
    assert len(store.queue()) == 1 and store.queue()[0]['due_at'] == now
    update_obkat(settings, store, clock='23:45')
    worker.schedule_month_end(now)
    assert store.queue()[0]['due_at'] == month_end_deadline('2026-10', '23:45', settings.zone)
    update_obkat(settings, store, clock='22:00')
    worker.schedule_month_end(now)
    assert store.queue()[0]['due_at'] == now
    store.acknowledge(store.queue()[0])
    store.set_state('last_month_end', '2026-10')
    update_obkat(settings, store, clock='23:45')
    worker.schedule_month_end(now + 5000)
    assert not store.queue()
    card = build_overview(settings, store, now)['cards'][0]
    assert card['month_end'] == month_end_deadline('2026-11', '23:45', settings.zone)


def test_retime_leaves_claimed_and_manual_jobs_unchanged(settings, store, wiki):
    now = datetime(2026, 10, 31, 22, 30, tzinfo=settings.zone).timestamp()
    store.enqueue('month_end:2026-10', 'month_end', now, spacing=True)
    job = store.queue()[0]
    run_id = store.start_run('month_end', spacing=True)
    store.update_progress(run_id, [{'code': 'started', 'job': {
        key: job[key] for key in ('key', 'revision', 'due_at', 'spacing')}}])
    store.enqueue('manual:spacing', 'full', now, spacing=True)
    before = store.queue()
    update_obkat(settings, store, clock='23:45')
    Worker(settings, store, wiki).schedule_month_end(now)
    assert store.queue() == before


def test_settings_merge_does_not_lose_concurrent_fields(settings, store):
    other = Store(settings.database_url)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(store.patch_state, 'example:config', {'quiet_minutes': 35})
            b = pool.submit(other.patch_state, 'example:config', {'other': 'value'})
            a.result(timeout=10)
            b.result(timeout=10)
        assert store.get_state('example:config') == {'quiet_minutes': 35, 'other': 'value'}
    finally:
        other.engine.dispose()
