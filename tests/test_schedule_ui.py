import time

import pytest

from toolforge_app.processors.maintenance.config import defaults, get_config
from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.web import create_app
from test_maintenance import MaintenanceWiki, form_data
from test_web import set_session


@pytest.mark.parametrize('slug', ['maintenance-dates', 'maintenance-rq', 'maintenance-rq-unwrap'])
def test_inline_schedule_changes_only_time_and_preserves_due_work(settings, store, slug):
    original = defaults(slug)
    original.update(autosave=False, debug_output=True)
    store.set_state(slug + ':config', original)
    store.enqueue('due', 'daily', time.time() - 1, processor=slug)
    store.enqueue('future', 'daily', time.time() + 86400, processor=slug)
    store.enqueue('manual', 'full', time.time() - 1, processor=slug)
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    response = client.post('/admin/maintenance/' + slug + '/schedule',
                           data={'csrf': 'csrf-test', 'run_time': '04:15'})
    assert response.status_code == 302 and '#settings' not in response.location
    assert get_config(store, slug) == dict(original, run_time='04:15')
    assert len(store.queue(slug)) == 2
    assert all(job['due_at'] < time.time() for job in store.queue(slug))
    # Changing another setting later must preserve the inline schedule.
    updated = dict(original, max_revisions=15) if slug != 'maintenance-rq-unwrap' else dict(original, autosave=True)
    values = form_data(updated, slug)
    values.pop('run_time')
    client.post('/admin/maintenance/' + slug + '/settings', data=values)
    expected = dict(updated, run_time='04:15')
    if 'section_templates' in expected:
        expected['section_templates_auto'] = original['section_templates_auto']
    assert get_config(store, slug) == expected


@pytest.mark.parametrize('value', ['', '24:00', '03:60', '3:00', '04:00:30', '<script>'])
def test_inline_schedule_rejects_invalid_time_without_changing_config_or_queue(settings, store, value):
    slug = 'maintenance-rq'
    worker = MaintenanceWorker(settings, store, slug, MaintenanceWiki())
    worker.schedule(time.time())
    before = store.queue(slug)
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    client.post('/admin/maintenance/' + slug + '/schedule', data={'csrf': 'csrf-test', 'run_time': value})
    assert get_config(store, slug) == defaults(slug) and store.queue(slug) == before
