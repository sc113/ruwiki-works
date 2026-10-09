import json
import time
from unittest.mock import Mock

import pytest

from toolforge_app.overview import build_overview
from toolforge_app.processors.maintenance.config import defaults, fields, get_config
from toolforge_app.processors.maintenance.inventory import refresh_inventory, report, scan
from toolforge_app.processors.maintenance.service import InventoryMonitor, MaintenanceWorker
from toolforge_app.processors.maintenance.transform import update_dates
from toolforge_app.processors.maintenance.unwrap import unwrap_rq
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision
from test_maintenance import MaintenanceWiki, form_data, queued, transform
from test_web import set_session

UNWRAP = 'maintenance-rq-unwrap'


def unwrap(text):
    wiki = MaintenanceWiki([text])
    return unwrap_rq(wiki.base, wiki.template_aliases('Rq'), defaults(UNWRAP), lambda *a, **kw: None)


@pytest.mark.parametrize('wrapper', ['Rq', 'rq', 'RQ', 'Рк', 'Template:Rq'])
def test_unwrap_preserves_inner_template_date_parameters_and_comments(wrapper):
    inner = '{{Нет источников|дата=2017-05-08|причина=Первичная публикация}}<!-- пояснение -->'
    before = 'Текст\n{{' + wrapper + '|1=\n' + inner + '\n}}\nКонец'
    text, changes, notes = unwrap(before)
    assert text == 'Текст\n' + inner + '\nКонец'
    assert len(changes) == 1 and changes[0]['date'] == '2017-05-08'
    assert changes[0]['action'] == 'unwrapped' and not notes
    assert unwrap(text)[0] == text and not unwrap(text)[1]


@pytest.mark.parametrize('rq', [
    '{{Rq|{{Нет источников}}{{Проверить факты}}}}',
    '{{Rq|{{Нет источников}}|topic=Медицина}}',
    '{{Rq|{{Нет источников}}|topic=}}',
    '{{Rq|{{Нет источников}}|fromlang=en}}',
    '{{Rq|{{Нет источников}}|раздел=да}}',
    '{{Rq|{{Нет источников}}|custom}}',
    '{{Rq|check}}',
    '{{Rq|2={{Нет источников}}}}',
    '{{Rq|1={{Нет источников}}|1={{Проверить факты}}}}',
    '{{Rq|{{#if:1|текст}}}}',
])
def test_unwrap_refuses_multiple_problems_and_parameters_without_discarding_them(rq):
    text, changes, notes = unwrap(rq)
    assert text == rq and not changes and notes


def test_unwrap_counts_problem_templates_at_top_level_and_preserves_helpers():
    inner = '{{Нет источников|дата=2018-01-01|причина={{Уточнить|1=источник}}}}'
    assert unwrap('{{Rq|' + inner + '}}')[0] == inner


def test_unwrap_leaves_comments_nowiki_and_heading_templates_untouched():
    wrapper = '{{Rq|{{Нет источников}}}}'
    before = '<!-- ' + wrapper + ' -->\n<nowiki>' + wrapper + '</nowiki>\n== ' + wrapper + ' ==\n' + wrapper + '\n' + wrapper
    text, changes, _ = unwrap(before)
    assert '<!-- ' + wrapper + ' -->' in text and '<nowiki>' + wrapper + '</nowiki>' in text
    assert '== ' + wrapper + ' ==' in text and len(changes) == 2
    assert text.endswith('{{Нет источников}}\n{{Нет источников}}')


def test_nested_wrappers_are_processed_inside_out_and_are_idempotent():
    text, changes, _ = unwrap('{{Rq|{{Rq|{{Нет источников|дата=2015-01-01}}}}}}')
    assert text == '{{Нет источников|дата=2015-01-01}}' and len(changes) == 2
    assert not unwrap(text)[1]


def test_rq_conversion_retains_wrapper_for_the_separate_unwrap_task():
    wiki = MaintenanceWiki(['Текст', '{{Rq|sources}}'])
    converted, changes, _ = transform(wiki, 'maintenance-rq')
    assert converted == '{{Rq|\n{{Нет источников|дата=2020-01-02}}\n}}' and len(changes) == 1
    assert unwrap(converted)[0] == '{{Нет источников|дата=2020-01-02}}'


def test_dates_task_never_inserts_a_date_on_the_rq_container():
    wiki = MaintenanceWiki(['Текст', '{{Rq|check}}'])
    history = Mock()
    text, changes, _ = update_dates(wiki.base, [wiki.template_aliases('Rq')], defaults('maintenance-dates'),
                                     history, lambda *a, **kw: None)
    assert text == wiki.base.text and not changes
    history.find.assert_not_called()


def test_unwrap_category_scan_has_its_own_root_and_no_subcategory_queries():
    wiki = MaintenanceWiki()
    inventory = scan(wiki, UNWRAP, defaults(UNWRAP))
    assert inventory['source'] == defaults(UNWRAP)['target_category'] and inventory['total'] == 1
    assert wiki.calls == [(defaults(UNWRAP)['target_category'], 'page', 0, '')]


@pytest.mark.parametrize('write', [True, False])
def test_unwrap_worker_needs_no_history_and_has_its_own_logs(settings, store, write):
    settings.wiki_write = write
    wiki = MaintenanceWiki(['{{Rq|{{Нет источников|дата=2010-10-10}}}}'])
    wiki.history_index = Mock(side_effect=AssertionError('No history for unwrap'))
    MaintenanceWorker(settings, store, UNWRAP, wiki).execute(queued(store, UNWRAP))
    run = store.list_runs(processor=UNWRAP)[0]
    assert run['status'] == 'success' and run['dry_run'] is not write
    assert len(wiki.edits) == int(write)
    assert not store.list_runs(processor='maintenance-rq') and not store.queue(UNWRAP)
    assert report(store, UNWRAP)['problems'] == 0
    events = json.loads(run['events'])
    changes = next(event['changes'] for event in events if event['code'] in {'edited', 'would_edit'})
    assert changes[0]['date'] == '2010-10-10' and changes[0]['action'] == 'unwrapped'
    assert not any(event['code'] == 'section_templates' for event in events)


def test_unwrap_remaining_category_is_public_manual_issue_and_retries_only_next_night(settings, store):
    settings.wiki_write = True
    wiki = MaintenanceWiki(['{{Rq|{{Нет источников}}|topic=X}}'])
    worker = MaintenanceWorker(settings, store, UNWRAP, wiki)
    worker.execute(queued(store, UNWRAP))
    assert store.list_runs(processor=UNWRAP)[0]['status'] == 'success' and not wiki.edits
    assert report(store, UNWRAP)['problems'] == 1
    client = create_app(settings, store).test_client()
    html = client.get('/tasks/' + UNWRAP, follow_redirects=True).get_data(as_text=True)
    assert 'Пример' in html and 'именованные параметры RQ: topic' in html and 'Исправить 1' in html
    worker.tick()
    assert len(store.list_runs(processor=UNWRAP)) == 1
    assert store.queue(UNWRAP)[0]['kind'] == 'daily' and store.queue(UNWRAP)[0]['due_at'] > time.time()


def test_unwrap_stale_category_after_a_saved_edit_is_also_a_manual_issue(settings, store):
    settings.wiki_write = True
    wiki = MaintenanceWiki(['{{Rq|{{Нет источников}}}}'], remains=True)
    MaintenanceWorker(settings, store, UNWRAP, wiki).execute(queued(store, UNWRAP))
    assert len(wiki.edits) == 1 and store.list_runs(processor=UNWRAP)[0]['status'] == 'success'
    assert report(store, UNWRAP)['problems'] == 1
    wiki.remains = False
    refresh_inventory(wiki, store, UNWRAP)
    assert report(store, UNWRAP)['problems'] == 0


def test_monitor_tracks_all_three_categories_and_unwrap_has_independent_controls(settings, store):
    wiki = MaintenanceWiki()
    InventoryMonitor(settings, store, wiki).tick(now=time.time())
    assert report(store, UNWRAP)['to_process'] == 1
    assert any(call[0] == defaults(UNWRAP)['target_category'] for call in wiki.calls)
    store.change_control(UNWRAP, 'stop', 'admin')
    MaintenanceWorker(settings, store, UNWRAP, MaintenanceWiki()).tick()
    assert not store.queue(UNWRAP) and store.control('maintenance-rq')['mode'] == 'active'
    assert len(build_overview(settings, store)['cards']) == 10


def test_admin_can_edit_unwrap_settings_without_date_or_conversion_settings(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    config = defaults(UNWRAP)
    config.update(run_time='04:00', autosave=False, debug_output=True,
                  target_category='Категория:Другая')
    result = client.post('/admin/maintenance/' + UNWRAP + '/settings', data=form_data(config, UNWRAP))
    assert result.status_code == 302 and get_config(store, UNWRAP) == config
    html = client.get('/tasks/' + UNWRAP, follow_redirects=True).get_data(as_text=True)
    assert all('name="' + key + '"' in html for key, _, _, _ in fields(UNWRAP))
    assert 'name="rq_param_templates"' not in html and 'name="search_mode"' not in html
    assert client.post('/admin/maintenance/' + UNWRAP + '/refresh',
                       data={'csrf': 'csrf-test', 'mode': 'sections'}).status_code == 400
    assert get_config(store, 'maintenance-rq')['run_time'] == '03:00'


def test_unwrap_preview_cannot_save_settings_or_control_the_task(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    set_session(client)
    client.post('/preview', data={'csrf': 'csrf-test', 'mode': 'admin'})
    for endpoint in ('settings', 'run', 'refresh'):
        response = client.post('/admin/maintenance/' + UNWRAP + '/' + endpoint, data=form_data(defaults(UNWRAP), UNWRAP))
        assert response.status_code == 302 and UNWRAP in response.location
    assert not store.queue(UNWRAP) and not store.get_state(UNWRAP + ':config')


@pytest.mark.parametrize('suffix', ['', '/log.txt', '/log.json'])
def test_unwrap_public_logs_explain_wrapper_removal_without_claiming_date_insertion(settings, store, suffix):
    run_id = store.start_run('daily', 'admin', dry_run=False, processor=UNWRAP)
    store.finish_run(run_id, [dict(at=1, code='started', message='PRIVATE_DIAGNOSTIC', title='',
                                  configuration={'debug_article': 'PRIVATE_SETTING'}),
                             dict(at=2, code='edited', message='Обновлено', title='Пример',
                                  changes=[dict(action='unwrapped', template='Нет источников', date='2010-10-10')],
                                  diff_before='PRIVATE_BEFORE', diff_after='PRIVATE_AFTER')], {'changed': 1}, {})
    client = create_app(settings, store).test_client()
    result = client.get('/runs/' + run_id + suffix)
    assert result.status_code == 200
    payload = json.dumps(result.get_json(), ensure_ascii=False) if suffix == '/log.json' else result.get_data(as_text=True)
    assert 'PRIVATE_' not in payload and 'Убрана обёртка RQ' in payload and 'дата сохранена: 2010-10-10' in payload
    set_session(client, 'admin')
    assert 'PRIVATE_' in client.get('/runs/' + run_id + suffix).get_data(as_text=True)
