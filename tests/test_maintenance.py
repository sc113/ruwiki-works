import json
import time
from datetime import datetime
from unittest.mock import Mock

import mwparserfromhell
import pytest

from toolforge_app.overview import build_overview
from toolforge_app.processors.maintenance.config import defaults, fields, form_value, get_config
from toolforge_app.processors.maintenance.history import History, template_occurrences
from toolforge_app.processors.maintenance.inventory import refresh_inventory, report, scan
from toolforge_app.processors.maintenance.service import InventoryMonitor, MaintenanceWorker, MOSCOW, next_daily
from toolforge_app.processors.maintenance.transform import update_dates, update_rq
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiClient, WikiError
from test_web import set_session

DATES, RQ = 'maintenance-dates', 'maintenance-rq'
DATE_CAT = 'Категория:Статьи с шаблоном Проверить факты без даты'
DATE_CAT2 = 'Категория:Вторая без дат'


class MaintenanceWiki:
    """Small real history fixtures; category contents change after fake saves."""
    def __init__(self, texts=None, *, remains=False):
        self.texts = texts or ['Текст', '{{проверить факты}}']
        self.base = Revision('Пример', len(self.texts), 1, self.texts[-1])
        self.edits, self.calls = [], []
        self.remains, self.fail_monitor, self.conflict = remains, False, False
        self.request_guard = lambda: None

    def category_members(self, title, *, kind='page', namespace=0, start_prefix=''):
        self.request_guard()
        self.calls.append((title, kind, namespace, start_prefix))
        if self.fail_monitor:
            raise WikiError('network')
        if namespace == 14:
            return [{'title': DATE_CAT}, {'title': DATE_CAT2}]
        if namespace == 10:
            return [{'title': 'Шаблон:Проверить факты'}, {'title': 'Шаблон:Нет источников'}]
        if self.edits and not self.remains:
            return []
        return [{'title': self.base.title}]

    def fetch(self, title):
        self.request_guard()
        if title.startswith('Категория:'):
            return Revision(title, 1, 1, '{{Категория к ежемесячной очистке|Проверить факты}}')
        return self.base

    def template_aliases(self, name):
        self.request_guard()
        canonical = name[:1].upper() + name[1:]
        aliases = [canonical, name]
        if name.lower() == 'проверить факты':
            aliases += ['Факты', 'Проверка фактов']
        if name.lower() == 'rq':
            aliases += ['RQ', 'Рк']
        return {'name': canonical, 'aliases': aliases}

    def history_index(self, title, revision, limit=0):
        self.request_guard()
        if limit and len(self.texts) > limit:
            raise WikiError('revision-limit')
        return [{'id': i + 1, 'timestamp': f'2020-01-{i + 1:02d}T23:59:59Z'} for i in range(len(self.texts))]

    def historical_text(self, title, revision):
        self.request_guard()
        return self.texts[revision - 1]

    def edit_article(self, base, text, summary, allowed):
        self.request_guard()
        assert base.title in allowed and base.revision == self.base.revision
        if self.conflict:
            raise WikiError('editconflict')
        self.edits.append((base.title, text, summary))
        self.base = Revision(base.title, base.revision + 1, time.time(), text)
        return self.base.revision


def configure(store, slug, **changes):
    config = defaults(slug)
    config.update(section_templates_auto=False, **changes)
    store.set_state(slug + ':config', config)
    return config


def queued(store, slug):
    store.enqueue('manual:normal', 'full', time.time() - 1, processor=slug, requested_by='admin')
    return store.queue(slug)[0]


def transform(wiki, slug=DATES, **changes):
    config = defaults(slug)
    config.update(changes)
    history = History(wiki, wiki.base, config, lambda *a, **kw: None)
    definitions = [wiki.template_aliases('Проверить факты'), wiki.template_aliases('Нет источников')]
    if slug == DATES:
        return update_dates(wiki.base, definitions, config, history, lambda *a, **kw: None)
    return update_rq(wiki.base, definitions, wiki.template_aliases('Rq'), config, history, lambda *a, **kw: None)


@pytest.mark.parametrize('mode,day', [(1, 4), (2, 2), (3, 2)])
def test_search_modes_handle_removal_and_reinstallation(mode, day):
    wiki = MaintenanceWiki(['Текст', '{{Факты}}', 'Снято', '{{проверить факты}}', '{{проверить факты}}'])
    text, changes, _ = transform(wiki, search_mode=mode)
    assert f'дата=2020-01-{day:02d}' in text
    assert changes[0]['revision'] == day


def test_dates_preserve_parameters_comments_and_existing_dates():
    text = '<!-- {{проверить факты}} -->\n<nowiki>{{проверить факты}}</nowiki>\n' \
           '{{Факты|1=03.04.2019|причина={{уточнить|а=б}}|дата=}}\n{{Нет источников|дата=2018-05-06}}'
    wiki = MaintenanceWiki(['Без шаблонов', text])
    updated, changes, _ = transform(wiki)
    assert '<!-- {{проверить факты}} -->' in updated and '<nowiki>{{проверить факты}}</nowiki>' in updated
    assert 'причина={{уточнить|а=б}}' in updated and 'дата=2018-05-06' in updated
    assert '03.04.2019' not in updated and 'дата=2020-01-02' in updated
    assert len(changes) == 1


def test_sections_have_distinct_dates_and_heading_templates_are_ignored():
    wiki = MaintenanceWiki(['== История ==\n{{Факты}}\n== География ==\nТекст',
                            '== История ==\n{{Факты}}\n== География ==\n{{Факты}}',
                            '== История {{Нет источников}} ==\n{{Факты}}\n== География ==\n{{Факты}}'])
    updated, changes, _ = transform(wiki, section_templates=['Факты'])
    assert '== История {{Нет источников}} ==' in updated
    assert [c['date'] for c in changes] == ['2020-01-01', '2020-01-02']
    assert len(template_occurrences(mwparserfromhell.parse(updated))) == 2
    wiki.texts = ['Без шаблонов', updated]
    wiki.base = Revision('Пример', 2, 1, updated)
    assert transform(wiki)[0] == updated


def test_section_rename_is_matched_but_two_similar_headings_are_ambiguous():
    wiki = MaintenanceWiki(['== История развития ==\n{{Факты}}', '== История развития города ==\n{{Факты}}'])
    assert transform(wiki, section_templates=['Факты'])[1][0]['date'] == '2020-01-01'
    wiki = MaintenanceWiki(['== История города ==\n{{Факты}}\n== История городов ==\n{{Факты}}',
                            '== История города ==\n{{Факты}}'])
    with pytest.raises(WikiError, match='ambiguous-section-history'):
        transform(wiki, section_templates=['Факты'])


def test_revision_limit_prevents_guessed_dates():
    with pytest.raises(WikiError, match='revision-limit'):
        transform(MaintenanceWiki(), max_revisions=1)


def test_multiple_identical_templates_in_one_section_are_not_given_guessed_dates():
    wiki = MaintenanceWiki(['{{Факты}}', '{{Факты}}\n{{Факты}}'])
    with pytest.raises(WikiError, match='ambiguous-template-occurrence'):
        transform(wiki)


def test_rq_preserves_unknown_parameters_topic_and_nested_content():
    old = '{{Рк|check<!--важно-->|sources|custom|topic=  Медицина |reason={{уточнить|x=y}}}}'
    wiki = MaintenanceWiki(['Текст', old])
    text, changes, notes = transform(wiki, RQ)
    assert 'topic=  Медицина ' in text and 'reason={{уточнить|x=y}}' in text
    assert '|2=custom' in text and '<!--важно-->' in text
    assert '{{Проверить факты|дата=2020-01-02}}' in text and '{{Нет источников|дата=2020-01-02}}' in text
    assert len(changes) == 2 and not notes
    wiki.texts[-1] = text
    wiki.base = Revision(wiki.base.title, wiki.base.revision, 1, text)
    assert transform(wiki, RQ)[0] == text


def test_rq_uses_earlier_standalone_history_and_deduplicates_synonyms():
    wiki = MaintenanceWiki(['{{Нет источников}}', '{{Rq|sources|source}}'])
    text, changes, _ = transform(wiki, RQ)
    assert text == '{{Rq|\n{{Нет источников|дата=2020-01-01}}\n}}'
    assert len(changes) == 1


def test_rq_existing_nested_date_is_not_replaced_or_claimed_as_inserted():
    wiki = MaintenanceWiki(['Текст', '{{Rq|check|{{Факты|дата=2017-01-01}}|topic=X}}'])
    text, changes, _ = transform(wiki, RQ)
    assert '{{Факты|дата=2017-01-01}}' in text and '|topic=X' in text
    assert changes[0]['date'] == '2017-01-01' and changes[0]['action'] == 'removed_parameter'
    assert text.count('{{Факты') == 1


def test_rq_all_skips_entire_article_without_history_requests():
    wiki = MaintenanceWiki(['{{Rq|check}}\n{{Rq|all<!--note-->|sources}}'])
    wiki.history_index = Mock(side_effect=AssertionError('History must not be fetched'))
    text, changes, notes = transform(wiki, RQ)
    assert text == wiki.base.text and not changes and notes


def test_daily_schedule_is_moscow_and_rolls_over_at_three():
    config = defaults(DATES)
    before = datetime(2026, 10, 6, 2, 59, tzinfo=MOSCOW).timestamp()
    at = datetime(2026, 10, 6, 3, 0, tzinfo=MOSCOW).timestamp()
    assert next_daily(config, before) == at
    assert next_daily(config, at) == datetime(2026, 10, 7, 3, 0, tzinfo=MOSCOW).timestamp()


def test_category_scan_is_one_level_articles_only_and_deduplicates():
    wiki = MaintenanceWiki()
    inventory = scan(wiki, DATES, defaults(DATES), now=100)
    assert inventory['total'] == 1 and len(inventory['categories']) == 2
    assert sum(c['count'] for c in inventory['categories']) == 2
    assert [c for c in wiki.calls if c[1] == 'subcat'] == [(defaults(DATES)['meta_category'], 'subcat', 14, '')]
    assert all(call[2] == 0 for call in wiki.calls[1:])


def test_monitor_refreshes_every_six_hours_including_failures(settings, store):
    wiki = MaintenanceWiki()
    monitor = InventoryMonitor(settings, store, wiki)
    monitor.tick(now=2000)
    assert report(store, DATES)['to_process'] == 1
    calls = len(wiki.calls)
    monitor.tick(now=2001)
    assert len(wiki.calls) == calls
    wiki.fail_monitor = True
    monitor.tick(now=23600)
    assert report(store, DATES)['monitor_error']['code'] == 'network'
    calls = len(wiki.calls)
    monitor.tick(now=23605)
    assert len(wiki.calls) == calls
    wiki.fail_monitor = False
    monitor.tick(now=45200)
    assert report(store, DATES)['monitor_error'] is None


@pytest.mark.parametrize('slug', [DATES, RQ])
def test_real_run_records_changes_and_verifies_categories(settings, store, slug):
    settings.wiki_write = True
    configure(store, slug)
    wiki = MaintenanceWiki(['Текст', '{{Rq|check}}'] if slug == RQ else None)
    worker = MaintenanceWorker(settings, store, slug, wiki)
    worker.execute(queued(store, slug))
    run = store.list_runs(processor=slug)[0]
    assert run['status'] == 'success' and len(wiki.edits) == 1
    assert report(store, slug)['to_process'] == 0 and report(store, slug)['problems'] == 0
    assert json.loads(run['summary'])['changed'] == 1
    assert not store.queue(slug) and not store.list_runs(processor='obkat')


@pytest.mark.parametrize('slug', [DATES, RQ])
def test_remaining_articles_are_manual_errors_and_wait_until_next_daily_run(settings, store, slug):
    settings.wiki_write = True
    configure(store, slug)
    wiki = MaintenanceWiki(['Текст', '{{Rq|check}}'] if slug == RQ else None, remains=True)
    worker = MaintenanceWorker(settings, store, slug, wiki)
    worker.execute(queued(store, slug))
    assert store.list_runs(processor=slug)[0]['status'] == 'failed'
    assert report(store, slug)['problems'] == 1
    worker.tick()
    assert len(store.list_runs(processor=slug)) == 1
    assert len(store.queue(slug)) == 1 and store.queue(slug)[0]['kind'] == 'daily'
    assert store.queue(slug)[0]['due_at'] > time.time()
    wiki.remains = False
    wiki.request_guard = lambda: None  # Independent read-only monitor client.
    refresh_inventory(wiki, store, slug)
    assert report(store, slug)['problems'] == 0


def test_pending_new_articles_are_not_presented_as_manual_errors(settings, store):
    wiki = MaintenanceWiki()
    configure(store, DATES)
    refresh_inventory(wiki, store, DATES)
    assert report(store, DATES)['to_process'] == 1 and report(store, DATES)['problems'] == 0


@pytest.mark.parametrize('global_write,autosave', [(False, True), (True, False)])
def test_dry_run_has_full_proposals_but_no_fake_cleanup_errors(settings, store, global_write, autosave):
    settings.wiki_write = global_write
    configure(store, DATES, autosave=autosave)
    wiki = MaintenanceWiki()
    MaintenanceWorker(settings, store, DATES, wiki).execute(queued(store, DATES))
    run = store.list_runs(processor=DATES)[0]
    assert run['status'] == 'success' and run['dry_run']
    assert json.loads(run['summary'])['proposed'] == 1
    assert not wiki.edits and report(store, DATES)['problems'] == 0
    assert store.get_state(DATES + ':result')['verification'] is False


def test_article_error_is_public_manual_issue_with_no_save(settings, store):
    settings.wiki_write = True
    configure(store, DATES)
    wiki = MaintenanceWiki(['Текст', '{{проверить факты}}'])
    wiki.conflict = True
    MaintenanceWorker(settings, store, DATES, wiki).execute(queued(store, DATES))
    assert not wiki.edits and report(store, DATES)['problems'] == 1
    assert any(e.get('error') == 'editconflict' for e in json.loads(store.list_runs(processor=DATES)[0]['events']))


@pytest.mark.parametrize('slug,text', [(DATES, '{{проверить факты|дата=2020-01-01}}'),
                                     (RQ, '{{rq}}'),
                                     ('maintenance-rq-unwrap', '{{Rq|{{Нет источников}}|topic=X}}')])
def test_dry_run_records_only_unprocessable_articles_as_manual_issues(settings, store, slug, text):
    configure(store, slug)
    MaintenanceWorker(settings, store, slug, MaintenanceWiki([text])).execute(queued(store, slug))
    run = store.list_runs(processor=slug)[0]
    assert run['dry_run'] and run['status'] == 'failed'
    assert report(store, slug)['problems'] == 1
    assert json.loads(run['summary'])['errors'] == 0
    client = create_app(settings, store).test_client()
    assert 'Пример' in client.get('/tasks/' + slug, follow_redirects=True).get_data(as_text=True)
    public = client.get('/runs/' + run['id'] + '/log.json').get_json()
    assert any(event['message'] == 'Требует исправления' for event in public['events'])


@pytest.mark.parametrize('slug', [DATES, RQ, 'maintenance-rq-unwrap'])
@pytest.mark.parametrize('write', [False, True])
@pytest.mark.parametrize('block', ['{{nobots}}', '{{bots|deny=all}}'])
def test_bot_exclusions_are_visible_skips_and_never_manual_errors(settings, store, slug, write, block):
    settings.wiki_write = write
    configure(store, slug)
    wiki = MaintenanceWiki([block + '{{Rq|check}}{{проверить факты}}'])
    MaintenanceWorker(settings, store, slug, wiki).execute(queued(store, slug))
    run = store.list_runs(processor=slug)[0]
    result = report(store, slug)
    assert run['status'] == 'success' and not wiki.edits
    assert result['problems'] == 0 and result['to_process'] == 0
    assert result['pages'] == 1 and result['skipped'] == 1
    assert result['excluded'][0]['title'] == 'Пример'
    assert json.loads(run['summary'])['errors'] == 0
    client = create_app(settings, store).test_client()
    html = client.get('/processors/maintenance?task=' + slug + '&view=pending').get_data(as_text=True)
    assert 'Запрет бота' in html and 'Остались только страницы с запретом бота' in html
    events = client.get('/runs/' + run['id'] + '/log.json').get_json()['events']
    assert len(events) == 1 and events[0]['message'] == 'Пропущено'
    wiki.fail_monitor = False
    wiki.category_members = lambda *args, **kwargs: []
    refresh_inventory(wiki, store, slug)
    assert not report(store, slug)['excluded']


def test_dry_run_reports_a_missing_date_even_when_another_template_can_be_updated(settings, store, monkeypatch):
    configure(store, DATES)
    wiki = MaintenanceWiki(['{{Проверить факты}}{{Нет источников}}'])
    fetch = wiki.fetch
    def with_two_templates(title):
        return Revision(title, 1, 1, '{{Категория к ежемесячной очистке|Проверить факты|Нет источников}}') if title.startswith('Категория:') else fetch(title)
    wiki.fetch = with_two_templates
    monkeypatch.setattr(History, 'find', lambda self, aliases, *a, **kw:
                        dict(date='2020-01-01', revision=1) if 'Проверить факты' in aliases else None)
    MaintenanceWorker(settings, store, DATES, wiki).execute(queued(store, DATES))
    run = store.list_runs(processor=DATES)[0]
    assert run['status'] == 'failed' and json.loads(run['summary'])['proposed'] == 1
    assert report(store, DATES)['problems'] == 1
    assert 'Нет источников' in report(store, DATES)['manual'][0]['reason']
    assert not wiki.edits


def test_remaining_article_outside_start_prefix_is_checked_for_bot_exclusion(settings, store):
    settings.wiki_write = True
    configure(store, RQ, rq_start_prefix='ZZ')
    wiki = MaintenanceWiki(['{{nobots}}{{Rq|check}}'])
    members = wiki.category_members
    wiki.category_members = lambda *a, **kw: [] if kw.get('start_prefix') else members(*a, **kw)
    MaintenanceWorker(settings, store, RQ, wiki).execute(queued(store, RQ))
    assert store.list_runs(processor=RQ)[0]['status'] == 'success' and not wiki.edits
    assert report(store, RQ)['skipped'] == 1 and report(store, RQ)['problems'] == 0


@pytest.mark.parametrize('action,status,pending', [('pause', 'paused', 1), ('stop', 'stopped', 0), ('restart', 'interrupted', 1)])
def test_maintenance_control_during_fetch_prevents_save(settings, store, action, status, pending):
    settings.wiki_write = True
    configure(store, DATES)
    wiki = MaintenanceWiki()
    original = wiki.fetch
    def fetch(title):
        value = original(title)
        if title == wiki.base.title:
            store.change_control(DATES, action, 'admin')
        return value
    wiki.fetch = fetch
    MaintenanceWorker(settings, store, DATES, wiki).execute(queued(store, DATES))
    assert not wiki.edits
    assert store.list_runs(processor=DATES)[0]['status'] == status
    assert len(store.queue(DATES)) == pending


def test_independent_jobs_leases_and_recovery(settings, store):
    for slug in (DATES, RQ):
        MaintenanceWorker(settings, store, slug, MaintenanceWiki()).schedule(time.time())
    assert store.queue(DATES)[0]['key'] != store.queue(RQ)[0]['key']
    with store.worker_lease(processor=DATES) as dates_lease, store.worker_lease(processor=RQ) as rq_lease:
        assert dates_lease and rq_lease
        with store.worker_lease(processor=DATES) as duplicate:
            assert duplicate is None
    a = store.start_run('full', processor=DATES)
    b = store.start_run('full', processor=RQ)
    store.recover_runs(DATES)
    assert store.run(a)['status'] == 'interrupted' and store.run(b)['status'] == 'running'


def test_real_wiki_article_writer_rejects_outside_scope_and_checks_stop_before_save(settings, store):
    from toolforge_app.processors.maintenance.service import Controlled
    settings.wiki_write = True
    settings.bot_username = 'ExampleBot'
    client = WikiClient(settings)
    client.login = Mock()
    base = Revision('Пример', 1, 1, 'old')
    with pytest.raises(WikiError, match='outside-scope'):
        client.edit_article(base, 'new', 'summary', {'Другая'})
    worker = MaintenanceWorker(settings, store, DATES, client)
    worker.generation = store.control(DATES)['generation']
    response = Mock(status_code=200)
    def stop_on_token():
        store.change_control(DATES, 'stop', 'admin')
        return {'query': {'tokens': {'csrftoken': 'TOKEN'}}}
    response.json.side_effect = stop_on_token
    client.session.get = Mock(return_value=response)
    client.session.post = Mock()
    with pytest.raises(Controlled):
        client.edit_article(base, 'new', 'summary', {'Пример'})
    client.session.post.assert_not_called()


def recorded(store):
    run_id = store.start_run('daily', 'admin', dry_run=False, processor=RQ)
    events = [dict(at=1, code='started', message='PRIVATE_DIAGNOSTIC', title='', configuration={'debug_article': 'PRIVATE_SETTING'}),
              dict(at=2, code='history', message='PRIVATE_HISTORY', title='Unchanged article'),
              dict(at=3, code='error', message='PRIVATE_ERROR', title='Failed article', error='SECRET_ERROR'),
              dict(at=4, code='edited', message='Updated', title='Обработанная статья', revision=9,
                   changes=[dict(template='Нет источников', date='2020-01-02', action='inserted_template',
                                 parameter='sources', revision=1, section='PRIVATE_SECTION')],
                   diff_before='PRIVATE_OLD_TEXT', diff_after='PRIVATE_NEW_TEXT')]
    store.finish_run(run_id, events, {'changed': 1, 'errors': 1}, {'private': 'PRIVATE_REPORT'})
    return run_id


@pytest.mark.parametrize('suffix', ['', '/log.txt', '/log.json'])
def test_all_public_log_formats_hide_diagnostics_but_show_changes(settings, store, suffix):
    run_id = recorded(store)
    client = create_app(settings, store).test_client()
    result = client.get('/runs/' + run_id + suffix)
    assert result.status_code == 200
    text = result.get_data(as_text=True)
    assert 'PRIVATE_' not in text and 'SECRET_ERROR' not in text
    assert 'Unchanged article' not in text and 'Failed article' in text
    # JSON escapes are parsed to test the same public content.
    if suffix == '/log.json':
        assert [event['title'] for event in result.get_json()['events']] == ['Failed article', 'Обработанная статья']
    else:
        assert 'Обработанная статья' in text and 'Нет источников' in text and '2020-01-02' in text
    set_session(client, 'admin')
    admin = client.get('/runs/' + run_id + suffix).get_data(as_text=True)
    if not suffix:
        assert 'PRIVATE_DIAGNOSTIC' in admin
        assert 'PRIVATE_OLD_TEXT' in client.get('/runs/' + run_id + '/events/3').get_data(as_text=True)
    else:
        assert 'PRIVATE_SETTING' in admin and 'PRIVATE_OLD_TEXT' in admin


def test_obkat_export_cannot_bypass_private_maintenance_logs(settings, store):
    run_id = recorded(store)
    client = create_app(settings, store).test_client()
    for extension in ('json', 'txt', 'csv'):
        assert client.get('/reports/obkat.' + extension + '?run=' + run_id).status_code == 404
    assert client.get('/runs/' + run_id + '/table.wiki').status_code == 404


def form_data(config, slug):
    values = {key: form_value(config, key, kind) for key, _, kind, _ in fields(slug) if kind != 'bool'}
    values.update({key: 'on' for key, _, kind, _ in fields(slug) if kind == 'bool' and config[key]})
    values['csrf'] = 'csrf-test'
    return values


def test_admin_can_edit_every_setting_independently_and_manual_sections_disable_auto(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    for slug in (DATES, RQ):
        config = defaults(slug)
        config.update(run_time='04:10', search_mode=3, max_revisions=70, debug_output=True,
                      section_templates=['Факты', 'Нет источников'])
        if slug == RQ:
            config.update(rq_param_templates={'check': 'Факты'}, rq_skip_params=['all', 'skip'], rq_start_prefix='А')
        response = client.post('/admin/maintenance/' + slug + '/settings', data=form_data(config, slug))
        assert response.status_code == 302
        actual = get_config(store, slug)
        assert actual == dict(config, section_templates_auto=False)
        html = client.get('/tasks/' + slug, follow_redirects=True).get_data(as_text=True)
        assert all('name="' + key + '"' in html for key, _, _, _ in fields(slug))
    assert get_config(store, RQ)['rq_skip_params'] == ['all', 'skip']
    assert 'rq_skip_params' not in get_config(store, DATES)


@pytest.mark.parametrize('username', [None, 'Admin', 'AnotherUser'])
def test_settings_and_run_require_exact_admin_csrf_and_post(settings, store, username):
    client = create_app(settings, store).test_client()
    set_session(client, username)
    for endpoint in ('settings', 'run', 'refresh', 'schedule'):
        path = '/admin/maintenance/' + DATES + '/' + endpoint
        assert client.get(path).status_code == 405
        assert client.post(path, data={}).status_code == 400
        assert client.post(path, data={'csrf': 'csrf-test'}).status_code == 403
    assert not store.queue(DATES) and not store.get_state(DATES + ':config')


def test_invalid_config_does_not_partially_save_or_add_removed_topic_processing(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    config = defaults(RQ)
    values = form_data(config, RQ)
    values.update(run_time='25:00', rq_param_templates='topic=Факты')
    client.post('/admin/maintenance/' + RQ + '/settings', data=values)
    assert get_config(store, RQ) == config
    values['run_time'] = '03:00'
    client.post('/admin/maintenance/' + RQ + '/settings', data=values)
    assert get_config(store, RQ) == config


def test_admin_preview_shows_all_settings_but_cannot_mutate(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    set_session(client)
    client.post('/preview', data={'csrf': 'csrf-test', 'mode': 'admin'})
    html = client.get('/tasks/' + RQ, follow_redirects=True).get_data(as_text=True)
    assert all('name="' + key + '"' in html for key, _, _, _ in fields(RQ))
    for endpoint in ('settings', 'run', 'refresh', 'schedule'):
        response = client.post('/admin/maintenance/' + RQ + '/' + endpoint, data=form_data(defaults(RQ), RQ))
        assert response.status_code == 302 and 'task=maintenance-rq' in response.location
    assert not store.get_state(RQ + ':config') and not store.queue(RQ) and not store.queue('maintenance-monitor')


def test_overview_uses_independent_status_counts_and_schedule(settings, store):
    refresh_inventory(MaintenanceWiki(), store, DATES, now=time.time())
    store.set_state(RQ + ':worker_error', {'code': 'network'})
    cards = {card['task'].slug: card for card in build_overview(settings, store)['cards']}
    assert cards[DATES]['status'] == 'offline' and cards[RQ]['status'] == 'error'
    assert cards[DATES]['report']['to_process'] == 1 and cards[DATES]['report']['problems'] == 0
    assert datetime.fromtimestamp(cards[DATES]['next_scheduled'], MOSCOW).hour == 3
    client = create_app(settings, store).test_client()
    html = client.get('/').get_data(as_text=True)
    assert 'к обработке' in html and 'task=maintenance-dates' in html and 'task=maintenance-rq' in html


def test_schedule_change_cancels_only_future_daily_job_and_preserves_manual(settings, store):
    worker = MaintenanceWorker(settings, store, DATES, MaintenanceWiki())
    worker.schedule(time.time())
    queued(store, DATES)
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    config = defaults(DATES)
    config['run_time'] = '04:00'
    client.post('/admin/maintenance/' + DATES + '/settings', data=form_data(config, DATES))
    assert [job['kind'] for job in store.queue(DATES)] == ['full']
    worker.schedule(time.time())
    daily = next(job for job in store.queue(DATES) if job['kind'] == 'daily')
    assert datetime.fromtimestamp(daily['due_at'], MOSCOW).hour == 4


def test_crashed_pass_waits_until_next_daily_run(settings, store):
    configure(store, DATES)
    queued(store, DATES)
    crashed = store.start_run('full', processor=DATES)
    wiki = MaintenanceWiki()
    MaintenanceWorker(settings, store, DATES, wiki).tick()
    assert store.run(crashed)['status'] == 'interrupted'
    assert len(store.list_runs(processor=DATES)) == 1
    assert not wiki.calls and len(store.queue(DATES)) == 1
    assert store.queue(DATES)[0]['kind'] == 'daily' and store.queue(DATES)[0]['due_at'] > time.time()
