import csv
import json
import time
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from toolforge_app.config import Settings
from toolforge_app.dispatcher import Dispatcher
from toolforge_app.processors.sections import TASK_SLUGS
from toolforge_app.processors.sections.config import defaults, fields, get_config, parse_form
from toolforge_app.processors.sections.history import HEADERS, MODES, import_legacy
from toolforge_app.processors.sections.inventory import Catalogue, refresh_inventory, report
from toolforge_app.processors.sections.service import SectionMonitor, SectionWorker
from toolforge_app.processors.sections.transform import normalize, switch_templates
from toolforge_app.storage import Store
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiClient, WikiError
from test_web import set_session

FORWARD, BACKWARD = TASK_SLUGS


def definition(name, *aliases):
    return dict(name=name, aliases=[name, *aliases])


def transform(text, slug=FORWARD):
    config = defaults(slug)
    return switch_templates(text, slug, definition(config['source_template'], 'Empty section' if slug == FORWARD else 'Expand section'),
                            definition(config['replacement_template']), [definition(name) for name in config['ignored_templates']])


@pytest.mark.parametrize('content', [
    'Обычный текст.', '[[Статья]]', '[https://example.org ссылка]', '{{Неизвестный шаблон}}',
    '[[ :Категория:Видимая ссылка]]', '<nowiki>Текст</nowiki>', '=== Подраздел ===\nТекст.',
])
def test_forward_switch_preserves_everything_except_name(content):
    text = '== История ==\n{{ Пустой раздел |дата=2021-02-03|1={{Вложенный|x=y}}}}\n' + content
    changed, changes, issues, _ = transform(text)
    assert changed == text.replace(' Пустой раздел ', ' Дополнить раздел ')
    assert len(changes) == 1 and changes[0]['section'] == 'История' and not issues


@pytest.mark.parametrize('content', [
    '', '\n <!-- пояснение -->', '{{Основная статья|Статья}}', '[[Файл:X.png|thumb|Подпись]]',
    '[[Category:X]]', '<ref>Источник</ref>', '&nbsp;&#160;&#xA0;',
    '=== Подраздел ===\n<!-- комментарий -->\n==== Подподраздел ====\n{{См. также|X}}',
])
def test_empty_rules_do_not_count_headings_or_service_content(content):
    text = '== История ==\n{{Дополнить раздел|дата=2021-02-03}}\n' + content
    changed, changes, issues, _ = transform(text, BACKWARD)
    assert changed == text.replace('Дополнить раздел', 'Пустой раздел')
    assert len(changes) == 1 and not issues
    assert transform(changed)[0] == changed  # No oscillation between directions.


def test_child_content_propagates_upward_and_sibling_content_stays_separate():
    text = '== A ==\n{{Пустой раздел}}\n=== B ===\n{{Пустой раздел}}\n==== C ====\nТекст\n== D ==\n{{Пустой раздел}}\n'
    changed, changes, _, _ = transform(text)
    assert [row['section'] for row in changes] == ['A', 'B']
    assert changed.endswith('== D ==\n{{Пустой раздел}}\n')
    assert all(row['reason'] == 'Есть непустые подразделы' for row in changes)


def test_aliases_case_namespace_and_duplicate_occurrences():
    text = '== A ==\n{{ empty_section|дата=x}}{{Template:Empty section|дата=y}}\nТекст'
    changed, changes, _, _ = transform(text)
    assert changed == '== A ==\n{{ дополнить раздел|дата=x}}{{Дополнить раздел|дата=y}}\nТекст'
    assert len(changes) == 2


def test_nested_examples_comments_and_heading_templates_are_untouched():
    text = '<!-- {{Пустой раздел}} -->\n<nowiki>{{Пустой раздел}}</nowiki>\n== {{Пустой раздел}} ==\n{{RQ|{{Пустой раздел}}}}'
    assert transform(text)[0] == text
    lead = '{{Пустой раздел|дата=x}}\nТекст\n== A ==\n{{Пустой раздел}}'
    changed, changes, _, _ = transform(lead)
    assert changed.startswith('{{Дополнить раздел|дата=x}}') and len(changes) == 1


def test_conflicting_markers_need_manual_choice_while_other_sections_can_change():
    text = '== A ==\n{{Пустой раздел}}{{Дополнить раздел}}\nТекст\n== B ==\n{{Пустой раздел}}\nТекст'
    changed, changes, issues, _ = transform(text)
    assert changed.startswith('== A ==\n{{Пустой раздел}}{{Дополнить раздел}}')
    assert [row['section'] for row in changes] == ['B'] and issues[0]['code'] == 'conflicting-markers'


class SectionWiki:
    def __init__(self, text='== A ==\n{{Пустой раздел|дата=2020-01-02}}\nТекст'):
        self.data = {'Пример': Revision('Пример', 10, time.time() - 3600, text)}
        self.calls, self.edits = [], []
        self.error = None

    def template_aliases(self, name):
        self.calls.append(('aliases', name))
        return definition(name)

    def template_articles(self, name):
        self.calls.append(('inventory', name))
        return [dict(title=title) for title, base in self.data.items() if '{{' + name in base.text]

    def fetch(self, title):
        self.calls.append(('fetch', title))
        if self.error:
            raise WikiError(self.error)
        return self.data.get(title, Revision(title, missing=True))

    def edit_article(self, base, text, summary, allowed):
        assert base.title in allowed and base.revision == self.data[base.title].revision
        self.edits.append((base, text, summary))
        self.data[base.title] = Revision(base.title, base.revision + 1, time.time(), text)
        return base.revision + 1


def queue(store, slug=FORWARD, kind='full'):
    store.enqueue('manual:' + kind, kind, time.time() - 1, processor=slug)
    return next(job for job in store.queue(slug) if job['kind'] == kind)


def form_data(slug=FORWARD, **changes):
    values = dict(defaults(slug), **changes)
    data = {'csrf': 'csrf-test'}
    for key, _, kind, _ in fields(slug):
        data[key] = ('on' if values[key] else '') if kind == 'bool' else '\n'.join(values[key]) if kind == 'list' else str(values[key])
    return data


@pytest.mark.parametrize('slug,text', [(FORWARD, '== A ==\n{{Пустой раздел}}\nТекст'), (BACKWARD, '== A ==\n{{Дополнить раздел}}')])
def test_both_workers_save_and_public_log_shows_only_articles_and_switches(settings, store, slug, text):
    settings.wiki_write = True
    wiki = SectionWiki(text)
    SectionWorker(settings, store, slug, wiki).execute(queue(store, slug))
    run = store.list_runs(processor=slug)[0]
    assert run['status'] == 'success' and len(wiki.edits) == 1
    assert store.checked_articles(slug)['Пример']['outcome'] == 'edited'
    client = create_app(settings, store).test_client()
    public = client.get('/runs/' + run['id'] + '/log.json').get_json()
    assert len(public['events']) == 1 and public['events'][0]['changes'][0]['action'] == 'section_switch'
    assert public['events'][0]['changes'][0]['section'] == 'A'
    assert 'Текст' not in json.dumps(public, ensure_ascii=False) and not public['report']
    set_session(client, 'admin')
    admin = client.get('/runs/' + run['id'] + '/log.json?full=1').get_json()
    assert any('configuration' in event for event in admin['events'])


def test_incremental_visits_persist_and_only_new_articles_are_fetched(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('== A ==\n{{Пустой раздел}}')
    SectionWorker(settings, store, FORWARD, wiki).execute(queue(store))
    assert report(store, FORWARD)['skipped'] == 1 and report(store, FORWARD)['problems'] == 0
    reopened = Store(settings.database_url)
    try:
        wiki.calls.clear()
        wiki.data['Новая'] = Revision('Новая', 1, time.time() - 60, '{{Пустой раздел}} Текст')
        SectionWorker(settings, reopened, FORWARD, wiki).execute(queue(reopened))
        assert [call for call in wiki.calls if call[0] == 'fetch'] == [('fetch', 'Новая')]
        assert len(reopened.checked_articles(FORWARD)) == 2 and not reopened.checked_articles(BACKWARD)
    finally:
        reopened.engine.dispose()


def test_recheck_is_deferred_until_execution_and_preserves_other_action_and_runs(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('== A ==\n{{Пустой раздел}}')
    SectionWorker(settings, store, FORWARD, wiki).execute(queue(store))
    store.record_article_checks(BACKWARD, [dict(title='Другая', checked_at=1, outcome='unchanged', reason='', run_id='')])
    job = queue(store, kind='recheck')
    assert 'Пример' in store.checked_articles(FORWARD)
    wiki.calls.clear()
    SectionWorker(settings, store, FORWARD, wiki).execute(job)
    assert ('fetch', 'Пример') in wiki.calls and len(store.list_runs(processor=FORWARD)) == 2
    assert 'Другая' in store.checked_articles(BACKWARD)


def test_recheck_resume_does_not_clear_completed_visits_twice(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('== A ==\n{{Пустой раздел}}')
    wiki.data['Я'] = Revision('Я', 1, time.time() - 60, '{{Пустой раздел}}')
    original = wiki.fetch
    def fetch(title):
        base = original(title)
        if title == 'Я':
            store.change_control(FORWARD, 'pause', 'admin')
        return base
    wiki.fetch = fetch
    worker = SectionWorker(settings, store, FORWARD, wiki)
    worker.execute(queue(store, kind='recheck'))
    assert store.list_runs(processor=FORWARD)[0]['status'] == 'paused'
    store.change_control(FORWARD, 'resume', 'admin')
    wiki.fetch, wiki.calls = original, []
    worker.execute(store.queue(FORWARD)[0])
    assert ('fetch', 'Пример') not in wiki.calls


def test_dry_runs_and_technical_errors_never_suppress_later_execution(settings, store):
    wiki = SectionWiki()
    worker = SectionWorker(settings, store, FORWARD, wiki)
    worker.execute(queue(store, kind='recheck'))
    assert not wiki.edits and not store.checked_articles(FORWARD)
    settings.wiki_write = True
    wiki.error = 'network'
    worker.execute(queue(store))
    assert store.list_runs(processor=FORWARD)[0]['status'] == 'failed' and not store.checked_articles(FORWARD)
    assert report(store, FORWARD)['problems'] == 0 and report(store, FORWARD)['to_process'] == 1
    assert store.get_state(FORWARD + ':worker_error')['code'] == 'network'
    wiki.error = None
    worker.execute(queue(store))
    assert len(wiki.edits) == 1 and report(store, FORWARD)['problems'] == 0


def test_in_use_page_is_not_saved_or_permanently_marked_as_skipped(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('{{Редактирую}}\n== A ==\n{{Пустой раздел}}\nТекст')
    worker = SectionWorker(settings, store, FORWARD, wiki)
    worker.execute(queue(store))
    assert not wiki.edits and not store.checked_articles(FORWARD)
    assert store.list_runs(processor=FORWARD)[0]['status'] == 'failed'
    assert report(store, FORWARD)['to_process'] == 1
    base = wiki.data['Пример']
    wiki.data['Пример'] = Revision(base.title, base.revision + 1, time.time(), base.text.replace('{{Редактирую}}', ''))
    worker.execute(queue(store))
    assert len(wiki.edits) == 1


def test_resume_setting_and_article_limit_control_the_actual_pass(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('{{Пустой раздел}}')
    wiki.data['Я'] = Revision('Я', 1, 1, '{{Пустой раздел}}')
    store.set_state(FORWARD + ':config', dict(limit_articles=1))
    worker = SectionWorker(settings, store, FORWARD, wiki)
    worker.execute(queue(store))
    assert list(store.checked_articles(FORWARD)) == ['Пример']
    assert report(store, FORWARD)['to_process'] == 1
    worker.execute(queue(store))
    assert len(store.checked_articles(FORWARD)) == 2
    # An obsolete single-article setting cannot restrict a new service run.
    store.patch_state(FORWARD + ':config', dict(resume=False, limit_articles=0, debug_article='Я'))
    wiki.calls.clear()
    worker.execute(queue(store))
    assert [call for call in wiki.calls if call[0] == 'fetch'] == [('fetch', 'Пример'), ('fetch', 'Я')]


def test_alias_overlap_aborts_before_editing(settings, store):
    settings.wiki_write = True
    class OverlapWiki(SectionWiki):
        def template_aliases(self, name):
            return definition('Пустой раздел', name)
    wiki = OverlapWiki()
    SectionWorker(settings, store, FORWARD, wiki).execute(queue(store))
    assert not wiki.edits and not store.checked_articles(FORWARD)
    assert store.get_state(FORWARD + ':worker_error')['code'] == 'template-overlap'


def test_ambiguity_is_remembered_as_manual_problem_not_an_ordinary_skip(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('{{Пустой раздел}}{{Дополнить раздел}}')
    worker = SectionWorker(settings, store, FORWARD, wiki)
    worker.execute(queue(store))
    result = report(store, FORWARD)
    assert result['problems'] == 1 and result['skipped'] == 0 and result['to_process'] == 0
    wiki.calls.clear()
    worker.execute(queue(store))
    assert ('fetch', 'Пример') not in wiki.calls and not wiki.edits


def test_limits_cache_and_manual_refresh(settings, store):
    wiki = SectionWiki()
    wiki.data['Я'] = Revision('Я', 1, 1, '{{Пустой раздел}}')
    config = dict(defaults(FORWARD), embeddedin_limit=1, articles_cache_days=2)
    store.set_state(FORWARD + ':config', config)
    first = refresh_inventory(wiki, store, FORWARD, 100, config=config)
    assert first['limited'] and first['total'] == 1 and report(store, FORWARD)['monitor_error']
    wiki.calls.clear()
    assert refresh_inventory(wiki, store, FORWARD, 200, config=config, force=False) == first
    assert not wiki.calls
    refresh_inventory(wiki, store, FORWARD, 200, config=config, force=True)
    assert any(call[0] == 'inventory' for call in wiki.calls)
    # Default 0 cache age refreshes every monitor pass, without article fetches.
    monitor = SectionMonitor(settings, store, wiki)
    monitor.tick(1000, force=True)
    wiki.calls.clear()
    monitor.tick(1100)
    assert not wiki.calls
    queue_job = store.enqueue('refresh:test', 'redirects', 1100, title=FORWARD, processor='sections-monitor')
    monitor.tick(1200)
    assert any(call[0] == 'aliases' for call in wiki.calls) and not store.queue('sections-monitor')
    assert not wiki.edits


def test_template_inventory_follows_continuation_and_scans_aliases(settings, store, monkeypatch):
    wiki = WikiClient(settings)
    calls = []
    def request(params, post=False):
        calls.append(dict(params))
        return {'query': {'embeddedin': [{'title': 'Б' if 'eicontinue' in params else 'А'}]}, **({} if 'eicontinue' in params else {'continue': {'eicontinue': 'next'}})}
    monkeypatch.setattr(wiki, 'request', request)
    assert [item['title'] for item in wiki.template_articles('Пустой раздел')] == ['А', 'Б']
    assert calls[0]['einamespace'] == 0 and calls[1]['eicontinue'] == 'next'
    class AliasWiki(SectionWiki):
        def template_aliases(self, name):
            return definition(name, 'Empty section')
        def template_articles(self, name):
            return [dict(title='А')] if name == 'Пустой раздел' else [dict(title='А'), dict(title='Б')]
    inventory = refresh_inventory(AliasWiki(), store, FORWARD)
    assert inventory['total'] == 2 and len(inventory['articles']['А']) == 2


def test_direction_settings_and_schedules_are_independent_and_controls_require_admin(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, None)
    for slug in TASK_SLUGS:
        public = client.get('/processors/sections?task=' + slug).get_data(as_text=True)
        assert 'Пропущено' in public and '04:00 МСК' in public and 'name="autosave"' not in public
        assert client.post('/admin/tasks/' + slug + '/run', data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 403
    set_session(client, 'admin')
    response = client.post('/admin/tasks/' + FORWARD + '/settings', data=form_data(limit_articles=10, ignored_templates=['Основная статья']))
    assert response.status_code == 302 and get_config(store, FORWARD)['limit_articles'] == 10
    assert get_config(store, BACKWARD)['limit_articles'] == 0
    client.post('/admin/tasks/' + FORWARD + '/schedule', data={'csrf': 'csrf-test', 'run_time': '05:30'})
    assert get_config(store, FORWARD)['run_time'] == '05:30' and get_config(store, BACKWARD)['run_time'] == '04:00'
    html = client.get('/processors/sections?task=' + FORWARD).get_data(as_text=True)
    assert all('name="' + field[0] + '"' in html for field in fields(FORWARD))
    assert 'Обновить перенаправления' in html and 'value="recheck"' in html
    assert client.post('/admin/tasks/' + FORWARD + '/run', data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 302
    assert client.post('/admin/tasks/' + FORWARD + '/refresh', data={'csrf': 'csrf-test', 'mode': 'redirects'}).status_code == 302
    assert client.post('/admin/tasks/maintenance-dates/refresh', data={'csrf': 'csrf-test', 'mode': 'redirects'}).status_code == 400


@pytest.mark.parametrize('changes', [dict(source_template='{{Bad}}'), dict(replacement_template='Пустой раздел'),
                                      dict(limit_articles=-1), dict(redirects_cache_days='x'), dict(run_time='25:00')])
def test_config_validation(changes):
    with pytest.raises(ValueError):
        parse_form(form_data(**changes), FORWARD)


def tsv_fixture(path):
    for slug, mode in MODES.items():
        with (path / f'log_{mode}.tsv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle, delimiter='\t')
            writer.writerow(HEADERS)
            for title, status in [('Успех', 'saved'), ('Без замены', 'no_changes_needed'), ('Ошибка', 'error_save'), ('Без сохранения', 'changes_not_saved')]:
                writer.writerow([title, mode, '2026-10-01 01:00:00', status, 'true', '1.0', 'Причина\nс переносом'])
        (path / f'articles_{mode}.json').write_text(json.dumps([dict(title='Без замены', namespace_id=0, timestamp='2026-10-01 01:00:00')]), encoding='utf-8')
    (path / 'templates_redirects.json').write_text(json.dumps({'Пустой раздел': {'empty section': 'Пустой раздел'}}), encoding='utf-8')


def test_record_import_is_idempotent_keeps_failed_articles_pending_and_preserves_live_marks(tmp_path, store):
    tsv_fixture(tmp_path)
    values = import_legacy(tmp_path, store)
    assert values[FORWARD] == dict(total=2, added=2)
    assert set(store.checked_articles(FORWARD)) == {'Успех', 'Без замены'}
    assert report(store, FORWARD)['skipped'] == 1 and report(store, FORWARD)['cache_origin'] == 'legacy'
    assert import_legacy(tmp_path, store)[FORWARD]['added'] == 0
    store.record_article_checks(FORWARD, [dict(title='Без замены', checked_at=9999999999, outcome='edited', reason='', run_id='new')])
    import_legacy(tmp_path, store)
    assert store.checked_articles(FORWARD)['Без замены']['run_id'] == 'new'
    (tmp_path / 'articles_fill_to_empty.json').write_text('[{"title":"Bad","namespace_id":1}]', encoding='utf-8')
    with pytest.raises(ValueError):
        import_legacy(tmp_path, store)
    assert len(store.checked_articles(FORWARD)) == 2


def test_bulk_ledger_write_rolls_back_all_new_marks_on_invalid_entry(store):
    with pytest.raises(IntegrityError):
        store.record_article_checks(FORWARD, [dict(title='Первая', checked_at=1, outcome='unchanged', reason='', run_id=''),
                                             dict(title='Некорректная', checked_at=None, outcome='unchanged', reason='', run_id='')])
    assert not store.checked_articles(FORWARD)


def test_daily_serial_dispatcher_runs_directions_in_registry_order(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('== A ==\n{{Пустой раздел}}\nТекст\n== B ==\n{{Дополнить раздел}}')
    first = SectionWorker(settings, store, FORWARD, wiki)
    second = SectionWorker(settings, store, BACKWARD, wiki)
    queue(store, BACKWARD)
    queue(store, FORWARD)
    class Idle:
        outer_renew = lambda: None
        def tick(self):
            pass
    Dispatcher(settings, store, obkat=Idle(), maintenance=[], translations=[], sections=[first, second], categories=[]).tick()
    assert len(wiki.edits) == 2
    assert 'Дополнить раздел' in wiki.edits[0][1] and '{{Пустой раздел}}' in wiki.edits[1][1]
