import json
import time
from datetime import datetime

import pytest

from toolforge_app.dispatcher import Dispatcher
from toolforge_app.overview import build_overview
from toolforge_app.processors.translations.config import CATEGORIES, defaults, get_config, parse_form
from toolforge_app.processors.translations.inventory import refresh_inventory, report
from toolforge_app.processors.translations.history import import_logs, read_legacy_log, HEADERS
from toolforge_app.processors.translations.outcomes import LABELS
from toolforge_app.processors.translations.inventory import signature
from toolforge_app.storage import Store
from toolforge_app.processors.translations.service import TranslationMonitor, TranslationWorker
from toolforge_app.processors.translations.transform import parse_creation_comment, parse_talk, update_templates
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiError, WikiClient
from test_web import set_session

CATS, TALK = 'translations-categories', 'translations-talk'
LANGUAGES = {'en', 'de', 'simple', 'be-tarask', 'zh-min-nan'}
DEFINITIONS = [{'name': 'Грубый перевод', 'aliases': ['Грубый перевод', 'ГП']},
               {'name': 'Плохой перевод', 'aliases': ['Плохой перевод']}]


class TranslationWiki:
    def __init__(self, text='{{Грубый перевод|дата=2020-01-01}}', comment='Перевод [[:en:Original_title|Перевод]]',
                 talk='{{Переведённая статья|en|Original title}}'):
        self.title = 'Пример'
        self.data = {self.title: Revision(self.title, 10, time.time()-3600, text),
                     'Обсуждение:' + self.title: Revision('Обсуждение:' + self.title, 20, time.time()-3600, talk)}
        self.comment, self.calls, self.edits = comment, [], []
        self.network_error = False
        self.request_guard = lambda: None

    def category_members(self, category, **kwargs):
        self.request_guard()
        self.calls.append(('category', category))
        if self.network_error:
            raise WikiError('network')
        assert kwargs['namespace'] == 0
        article = self.data[self.title]
        return [] if '|язык=en|' in article.text else [{'title': self.title}]

    def template_aliases(self, name):
        self.request_guard()
        if name == 'Переведённая статья':
            return {'name': name, 'aliases': [name, 'Переведенная статья']}
        return next(item for item in DEFINITIONS if item['name'] == name)

    def wikipedia_languages(self):
        self.request_guard()
        return LANGUAGES

    def fetch(self, title):
        self.request_guard()
        self.calls.append(('fetch', title))
        return self.data.get(title, Revision(title, missing=True))

    def revisions(self, titles):
        return [self.fetch(title) for title in titles]

    def creation_comment(self, title, revision):
        self.request_guard()
        self.calls.append(('comment', title))
        return dict(revid=1, comment=self.comment)

    def edit_article(self, base, text, summary, allowed):
        self.request_guard()
        assert base.title in allowed and base.title == self.title
        assert base.revision == self.data[base.title].revision
        self.edits.append((base.title, text, summary))
        self.data[base.title] = Revision(base.title, base.revision+1, time.time(), text)
        return base.revision+1


def job(store, slug):
    store.enqueue('manual:normal', 'full', time.time()-1, processor=slug)
    return store.queue(slug)[0]


def form_data(slug, **values):
    config = dict(defaults(slug), **values)
    return {key: '\n'.join(value) if isinstance(value, list) else 'on' if value is True else '' if value is False else value
            for key, value in config.items()} | {'csrf': 'csrf-test'}


@pytest.mark.parametrize('comment,expected', [
    ('Перевод [[:en:Original_title|Переведённое название]]', ('en', 'Original title')),
    ('Перевод [[simple:Example]]', ('simple', 'Example')),
    ('[[https://de.wikipedia.org/wiki/Geschichte|История]]', ('de', 'Geschichte')),
    ('[https://en.wikipedia.org/w/index.php?title=Original_title&oldid=25 Источник]', ('en', 'Original title')),
    ('[[//en.wikipedia.org/wiki/Original_title#History|Источник]] [[:en:Original title]]', ('en', 'Original title')),
    ('https://be-tarask.wikipedia.org/wiki/%D0%9C%D1%96%D1%80', ('be-tarask', 'Мір')),
    ('Создано переводом страницы «[[:en:Special:Redirect/revision/589912530|Balata]]»', ('en', 'Balata')),
    ('[[:en:Special:PermanentLink/589912530|Balata]] [[:en:Balata]]', ('en', 'Balata')),
    ('[[:en:Special:Diff/589912530|Balata]]', ('en', 'Balata')),
    ('[[https://en.wikipedia.org/wiki/Special:Redirect/revision/589912530|Balata]]', ('en', 'Balata')),
    ('[https://en.wikipedia.org/wiki/Special:PermanentLink/589912530 Balata]', ('en', 'Balata')),
])
def test_creation_comment_extracts_target_not_link_label(comment, expected):
    assert parse_creation_comment(comment, LANGUAGES) == expected


@pytest.mark.parametrize('comment,code', [('', 'creation-comment-empty'), ('Создана страница', 'comment-source-missing'),
    ('[[File:Image.jpg]] [[:ru:Пример]]', 'comment-source-missing'),
    ('[[:en:One]] [[:de:Two]]', 'ambiguous-source'),
    ('[[:en:{{injection}}]]', 'invalid-original'),
    ('[[:en:Special:Redirect/revision/589912530]]', 'invalid-original'),
    ('https://en.wikipedia.org/wiki/Special:Diff/589912530', 'invalid-original'),
    ('[[:en:Special:Random|Balata]]', 'invalid-original'),
    ('[[:en:Special:Redirect/revision/589912530|Special:Diff/589912530]]', 'invalid-original'),
    ('[[:en:Special:Redirect/revision/589912530|https://en.wikipedia.org/wiki/Balata]]', 'invalid-original')])
def test_uncertain_creation_source_is_not_guessed(comment, code):
    with pytest.raises(WikiError, match=code):
        parse_creation_comment(comment, LANGUAGES)


@pytest.mark.parametrize('text,code', [
    ('{{Переведённая статья|en}}', 'talk-malformed'),
    ('{{Переведённая статья|ru|Пример}}', 'talk-malformed'),
    ('{{Переведённая статья|1=en|1=de|2=Example}}', 'talk-malformed'),
    ('{{Переведённая статья|en|Special:Diff/589912530}}', 'talk-malformed'),
    ('{{Переведённая статья|en|One}}{{Переведённая статья|en|Two}}', 'ambiguous-source'),
    ('<!-- {{Переведённая статья|en|Example}} -->', 'talk-template-missing'),
])
def test_talk_sources_require_complete_unambiguous_data(text, code):
    with pytest.raises(WikiError, match=code):
        parse_talk(text, {'переведённая статья'}, LANGUAGES)


def test_template_update_preserves_section_date_and_other_text_and_is_idempotent():
    before = 'Текст\n{{ГП|раздел|2=Original title|date=2020-01-01|обс=Тема}}\n<!-- {{Грубый перевод}} -->'
    text, changes, notes = update_templates(before, DEFINITIONS, 'en', 'Original title')
    assert not notes and len(changes) == 1
    assert '|раздел|' in text and '|дата=2020-01-01' in text and '|обс=Тема' in text
    assert '|язык=en|оригинал=Original title' in text and '|2=' not in text
    assert text.endswith('<!-- {{Грубый перевод}} -->')
    assert update_templates(text, DEFINITIONS, 'en', 'Original title') == (text, [], [])


@pytest.mark.parametrize('text', ['{{Грубый перевод|язык=de}}', '{{Грубый перевод|оригинал=Other}}',
                                '{{Грубый перевод|2=Other}}', '{{Грубый перевод|язык=|язык=en}}'])
def test_template_conflicts_do_not_partly_modify_template(text):
    result, changes, notes = update_templates(text, DEFINITIONS, 'en', 'Original title')
    assert result == text and changes == [] and notes


def test_identical_template_occurrences_are_all_updated_without_global_replace():
    text = '{{Грубый перевод}} X {{Грубый перевод}} {{Плохой перевод}}'
    result, changes, notes = update_templates(text, DEFINITIONS, 'en', 'Example')
    assert len(changes) == 3 and not notes and result.count('язык=en') == 3


@pytest.mark.parametrize('slug', [CATS, TALK])
def test_live_daily_pass_edits_article_only_and_projects_public_logs(settings, store, slug):
    settings.wiki_write = True
    wiki = TranslationWiki()
    worker = TranslationWorker(settings, store, slug, wiki)
    worker.execute(job(store, slug))
    assert len(wiki.edits) == 1 and wiki.edits[0][0] == 'Пример'
    assert bool([call for call in wiki.calls if call[0] == 'comment']) == (slug == CATS)
    assert '|язык=en|оригинал=Original title' in wiki.data['Пример'].text
    run = store.list_runs(processor=slug)[0]
    assert run['status'] == 'success' and report(store, slug)['to_process'] == 0
    client = create_app(settings, store).test_client()
    public = client.get('/runs/' + run['id'] + '/log.json').get_json()
    assert len(public['events']) == 1 and public['events'][0]['changes'][0]['language'] == 'en'
    assert 'diff_before' not in str(public) and 'configuration' not in str(public)
    assert 'original' in str(public)
    set_session(client, 'admin')
    admin = client.get('/runs/' + run['id'] + '/log.json').get_json()
    assert any('diff_before' in event for event in admin['events'])
    assert 'configuration' in str(admin)


def test_creation_revision_link_saves_its_article_title_not_the_special_page(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(comment='Создано переводом страницы «[[:en:Special:Redirect/revision/589912530|Balata]]»')
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert len(wiki.edits) == 1
    assert '|язык=en|оригинал=Balata|дата=2020-01-01' in wiki.data['Пример'].text
    assert 'Special:Redirect' not in wiki.data['Пример'].text
    assert '+оригинал=Balata' in wiki.edits[0][2]
    assert store.list_runs(processor=CATS)[0]['status'] == 'success'


def test_daily_dry_run_does_not_publish_unsaved_changes(settings, store):
    wiki = TranslationWiki()
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert wiki.edits == []
    run = store.list_runs(processor=CATS)[0]
    assert json.loads(run['summary'])['proposed'] == 1
    text = create_app(settings, store).test_client().get('/runs/' + run['id'] + '/log.txt').get_data(as_text=True)
    assert 'Подготовлено без сохранения' in text and 'Original title' not in text


def test_category_failure_is_remembered_independently_from_talk_action(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(comment='Нет источника')
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert not wiki.edits and report(store, CATS)['skipped'] == 1 and report(store, CATS)['problems'] == 0
    TranslationWorker(settings, store, TALK, wiki).execute(job(store, TALK))
    assert len(wiki.edits) == 1
    refresh_inventory(wiki, store, CATS)
    assert report(store, CATS)['problems'] == 0
    # A title already in this action's ledger stays skipped until an explicit recheck.
    wiki.data['Пример'] = Revision('Пример', 12, time.time(), '{{Плохой перевод}}')
    wiki.comment = '[[:en:Original title]]'
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert len(wiki.edits) == 1 and report(store, CATS)['to_process'] == 0
    assert report(store, CATS)['skipped'] == 1 and report(store, CATS)['problems'] == 0
    store.enqueue('manual:recheck', 'recheck', time.time()-1, processor=CATS)
    TranslationWorker(settings, store, CATS, wiki).execute(store.queue(CATS)[0])
    assert len(wiki.edits) == 2 and report(store, CATS)['problems'] == 0


@pytest.mark.parametrize('action,status', [('pause', 'paused'), ('stop', 'stopped'), ('restart', 'interrupted')])
def test_admin_controls_interrupt_before_article_edit(settings, store, action, status):
    settings.wiki_write = True
    wiki = TranslationWiki()
    fetch = wiki.fetch
    def change_after_fetch(title):
        base = fetch(title)
        if title == 'Пример':
            store.change_control(CATS, action, 'admin')
        return base
    wiki.fetch = change_after_fetch
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert not wiki.edits and store.list_runs(processor=CATS)[0]['status'] == status


def test_talk_change_prevents_save_and_failure_waits_until_next_daily_run(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki()
    wiki.revisions = lambda titles: [Revision(titles[0], 21, time.time(), 'Новый текст')]
    worker = TranslationWorker(settings, store, TALK, wiki)
    worker.execute(job(store, TALK))
    assert not wiki.edits and not store.queue(TALK)
    assert store.list_runs(processor=TALK)[0]['status'] == 'failed'
    now = datetime(2026, 10, 6, 3, 59, tzinfo=settings.zone).timestamp()
    worker.schedule(now)
    assert store.queue(TALK)[0]['due_at'] == now + 60


def test_monitor_counts_unique_articles_and_cooldown_and_invalidation(settings, store):
    wiki = TranslationWiki()
    monitor = TranslationMonitor(settings, store, wiki)
    monitor.tick(1801)
    assert all(report(store, slug)['to_process'] == 1 for slug in (CATS, TALK))
    assert len(report(store, CATS)['categories']) == 2
    calls = len(wiki.calls)
    monitor.tick(1806)
    assert len(wiki.calls) == calls
    store.patch_state(CATS + ':config', {'target_categories': ['Категория:Другая']})
    assert report(store, CATS)['to_process'] is None
    monitor.tick(1810)
    assert report(store, CATS)['to_process'] == 1
    wiki.network_error = True
    monitor.tick(23410)
    calls = len(wiki.calls)
    assert store.get_state(CATS + ':monitor_error')['code'] == 'network'
    monitor.tick(23415)
    assert len(wiki.calls) == calls and report(store, CATS)['to_process'] == 1


def test_empty_categories_need_no_source_lookups(settings, store):
    wiki = TranslationWiki(text='{{Грубый перевод|язык=en|оригинал=Example}}')
    worker = TranslationWorker(settings, store, CATS, wiki)
    worker.languages = lambda: pytest.fail('No source lookup for empty categories')
    worker.execute(job(store, CATS))
    assert store.list_runs(processor=CATS)[0]['status'] == 'success'


def test_translation_actions_are_serial_and_shared_schedule_is_editable(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki()
    first, second = TranslationWorker(settings, store, CATS, wiki), TranslationWorker(settings, store, TALK, wiki)
    second_job, first_job = job(store, TALK), job(store, CATS)
    second.execute(second_job)
    assert not wiki.calls
    first.execute(first_job)
    second.execute(second_job)
    assert len(wiki.edits) == 1 and len(store.list_runs(processor=None)) == 2
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    client.post('/admin/tasks/' + TALK + '/schedule', data={'csrf': 'csrf-test', 'run_time': '04:30'})
    assert get_config(store, TALK)['run_time'] == '04:30' and get_config(store, CATS)['run_time'] == '04:00'
    values = form_data(TALK, autosave=False)
    values.pop('run_time')
    client.post('/admin/tasks/' + TALK + '/settings', data=values)
    assert get_config(store, TALK)['run_time'] == '04:30' and not get_config(store, TALK)['autosave']


@pytest.mark.parametrize('slug', [CATS, TALK])
def test_pages_index_controls_parameters_and_previews(settings, store, slug):
    client = create_app(settings, store, admin_preview=True).test_client()
    public = client.get('/processors/translations?task=' + slug).get_data(as_text=True)
    assert '04:00 МСК' in public and 'Параметры' in public and 'Лог' in public
    assert 'name="autosave"' not in public
    assert '/tasks/' + slug in client.get('/').get_data(as_text=True)
    set_session(client, 'admin')
    admin = client.get('/processors/translations?task=' + slug).get_data(as_text=True)
    assert 'name="target_templates"' in admin and 'name="target_categories"' in admin
    assert 'name="mode" value="force"' not in admin and 'name="norm_file"' not in admin
    assert client.get('/console?task=' + slug).status_code == 200
    assert client.post('/admin/tasks/' + slug + '/run', data={'csrf': 'csrf-test'}).status_code == 302
    client.post('/admin/tasks/' + slug + '/control', data={'csrf': 'csrf-test', 'action': 'stop'})
    assert client.post('/admin/tasks/' + slug + '/run', data={'csrf': 'csrf-test'}).status_code == 409
    with client.session_transaction() as session:
        session.pop('username')
        session['admin_preview'] = True
    before = store.control(slug)
    response = client.post('/admin/tasks/' + slug + '/control', data={'csrf': 'csrf-test', 'action': 'restart'})
    assert store.control(slug) == before and '/processors/translations' in response.location


@pytest.mark.parametrize('username', [None, 'OtherUser', 'Admin'])
def test_translation_commands_require_exact_admin(settings, store, username):
    client = create_app(settings, store).test_client()
    set_session(client, username)
    for endpoint in ('run', 'settings', 'refresh', 'schedule', 'control'):
        assert client.post('/admin/tasks/' + CATS + '/' + endpoint, data={'csrf': 'csrf-test'}).status_code == 403


def test_configuration_validation_rejects_markup_and_empty_sources():
    for key, value in [('target_categories', ''), ('target_categories', 'Обсуждение:Пример'),
                       ('target_templates', '{{Шаблон}}'), ('parse_talk_template', ''), ('run_time', '24:00')]:
        with pytest.raises(ValueError):
            parse_form(form_data(TALK, **{key: value}), TALK)


def test_api_creation_comment_and_language_map(settings, monkeypatch):
    wiki = WikiClient(settings)
    requests = []
    def request(params, post=False):
        requests.append(params)
        if params.get('prop') == 'revisions':
            return {'query': {'pages': [{'title': 'Пример', 'revisions': [{'revid': 1, 'comment': '[[:en:Example]]'}]}]}}
        return {'query': {'interwikimap': [{'prefix': 'en', 'language': 'English', 'url': 'https://en.wikipedia.org/wiki/$1'},
            {'prefix': 'ru', 'language': 'Русский', 'url': 'https://ru.wikipedia.org/wiki/$1'},
            {'prefix': 'file', 'url': 'https://commons.wikimedia.org/wiki/$1'}]}}
    monkeypatch.setattr(wiki, 'request', request)
    assert wiki.creation_comment('Пример', 10)['revid'] == 1
    assert requests[0]['rvdir'] == 'newer' and requests[0]['rvlimit'] == 1 and requests[0]['rvendid'] == 10
    assert wiki.wikipedia_languages() == {'en'}


def mark_checked(store, slug, title='Пример', reason='Источник не найден'):
    store.record_article_checks(slug, [dict(title=title, checked_at=time.time(), outcome='comment-source-missing',
                                          reason=reason, run_id='')])


def recheck_job(store, slug):
    store.enqueue('manual:recheck', 'recheck', time.time()-1, processor=slug)
    return next(item for item in store.queue(slug) if item['kind'] == 'recheck')


def test_incremental_ledger_survives_restart_and_only_new_titles_are_read(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(comment='Нет данных')
    TranslationWorker(settings, store, CATS, wiki).execute(job(store, CATS))
    assert store.checked_articles(CATS)['Пример']['reason']
    # Reopen the database as a fresh process would.
    reopened = Store(settings.database_url)
    try:
        calls = len(wiki.calls)
        TranslationWorker(settings, reopened, CATS, wiki).execute(job(reopened, CATS))
        assert not any(call[0] in {'fetch', 'comment'} for call in wiki.calls[calls:])
        current = report(reopened, CATS)
        assert current['to_process'] == 0 and current['checked'] == 1 and current['skipped'] == 1 and current['problems'] == 0
        wiki.title = 'Новая статья'
        wiki.data[wiki.title] = Revision(wiki.title, 30, time.time(), '{{Грубый перевод}}')
        TranslationWorker(settings, reopened, CATS, wiki).execute(job(reopened, CATS))
        assert set(reopened.checked_articles(CATS)) == {'Пример', 'Новая статья'}
        assert reopened.checked_articles(TALK) == {}
    finally:
        reopened.engine.dispose()


def test_temporary_api_failure_is_not_marked_as_completed(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki()
    fetch = wiki.fetch
    def failing_fetch(title):
        raise WikiError('network')
    wiki.fetch = failing_fetch
    worker = TranslationWorker(settings, store, CATS, wiki)
    worker.execute(job(store, CATS))
    assert not store.checked_articles(CATS) and report(store, CATS)['to_process'] == 1
    assert report(store, CATS)['problems'] == 0 and report(store, CATS)['skipped'] == 0
    assert store.get_state(CATS + ':worker_error')['code'] == 'network'
    wiki.fetch = fetch
    worker.execute(job(store, CATS))
    assert len(wiki.edits) == 1 and 'Пример' in store.checked_articles(CATS)


def test_recheck_resets_only_selected_action_and_keeps_old_run_logs(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(comment='Нет источника')
    worker = TranslationWorker(settings, store, CATS, wiki)
    worker.execute(job(store, CATS))
    old_run = store.list_runs(processor=CATS)[0]['id']
    mark_checked(store, TALK)
    wiki.comment = '[[:en:Original title]]'
    worker.execute(recheck_job(store, CATS))
    assert len(wiki.edits) == 1 and store.run(old_run) is not None
    assert store.list_runs(processor=CATS)[0]['kind'] == 'recheck'
    assert store.checked_articles(CATS)['Пример']['outcome'] == 'edited'
    assert store.checked_articles(TALK)['Пример']['reason'] == 'Источник не найден'


def test_recheck_pause_and_resume_does_not_reset_completed_articles_again(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(comment='Нет источника')
    wiki.data['Вторая статья'] = Revision('Вторая статья', 12, time.time(), '{{Грубый перевод}}')
    names = ['Пример', 'Вторая статья']
    def members(category, **kwargs):
        wiki.request_guard()
        return [dict(title=title) for title in names]
    wiki.category_members = members
    mark_checked(store, CATS, 'Пример')
    mark_checked(store, CATS, 'Вторая статья')
    fetch = wiki.fetch
    # Sorted order is «Вторая статья», then «Пример».
    def pause_on_second(title):
        base = fetch(title)
        if title == 'Пример':
            store.change_control(CATS, 'pause', 'admin')
        return base
    wiki.fetch = pause_on_second
    pending = recheck_job(store, CATS)
    worker = TranslationWorker(settings, store, CATS, wiki)
    worker.execute(pending)
    assert store.list_runs(processor=CATS)[0]['status'] == 'paused'
    assert set(store.checked_articles(CATS)) == {'Вторая статья'}
    store.change_control(CATS, 'resume', 'admin')
    wiki.fetch = fetch
    calls = len(wiki.calls)
    worker.execute(pending)
    assert set(store.checked_articles(CATS)) == set(names)
    assert ('fetch', 'Вторая статья') not in wiki.calls[calls:]
    assert not store.queue(CATS) and len(store.list_runs(processor=CATS)) == 2


@pytest.mark.parametrize('slug', [CATS, TALK])
def test_dry_recheck_does_not_clear_ledger_or_mark_unsaved_changes(settings, store, slug):
    mark_checked(store, slug)
    before = store.checked_articles(slug)
    wiki = TranslationWiki()
    TranslationWorker(settings, store, slug, wiki).execute(recheck_job(store, slug))
    assert not wiki.edits and store.checked_articles(slug) == before
    run = store.list_runs(processor=slug)[0]
    assert json.loads(run['summary'])['proposed'] == 1
    public = create_app(settings, store).test_client().get('/runs/' + run['id'] + '/log.json').get_json()
    assert [event['code'] for event in public['events']] == ['would_edit']
    assert 'changes' not in public['events'][0]


def test_tsv_import_handles_padding_tabs_multiline_and_is_idempotent(tmp_path, store):
    header = '\t'.join(name.ljust(22) for name in HEADERS) + '\n'
    row = ['Грубый перевод', ' Пример ', '{{Грубый перевод}}', 'Комментарий\tсо вкладкой\nи строкой',
           'комментарий имеет другой формат', '', '', '{{Грубый перевод}}']
    (tmp_path / 'log_categories.tsv').write_text('\ufeff' + header + '\t'.join(row) + '\n', encoding='utf-8')
    row[4] = 'шаблон обновлен'
    (tmp_path / 'log_parse_talk.tsv').write_text(header + '\t'.join(row) + '\n', encoding='utf-8')
    result = import_logs(tmp_path, store)
    assert result[CATS]['added'] == result[TALK]['added'] == 1
    assert store.checked_articles(CATS)['Пример']['reason']
    assert store.checked_articles(TALK)['Пример']['reason'] == ''
    mark_checked(store, CATS, reason='Новая диагностика')
    assert import_logs(tmp_path, store)[CATS]['added'] == 0
    assert store.checked_articles(CATS)['Пример']['reason'] == 'Новая диагностика'


def test_invalid_second_tsv_cannot_partially_import_first(tmp_path, store):
    (tmp_path / 'log_categories.tsv').write_text('\t'.join(HEADERS) + '\n' +
        '\t'.join(['', 'Пример', '', '', 'комментарий не найден', '', '', '']) + '\n', encoding='utf-8')
    (tmp_path / 'log_parse_talk.tsv').write_text('bad header\n', encoding='utf-8')
    with pytest.raises(ValueError):
        import_logs(tmp_path, store)
    assert not store.checked_articles(CATS) and not store.checked_articles(TALK)


def test_article_keys_preserve_case_distinctions(store):
    mark_checked(store, CATS, 'Название ABC')
    mark_checked(store, CATS, 'Название Abc')
    assert len(store.checked_articles(CATS)) == 2


def test_recheck_button_is_admin_only_and_queues_without_immediate_reset(settings, store):
    mark_checked(store, CATS)
    client = create_app(settings, store, admin_preview=True).test_client()
    target = '/admin/tasks/' + CATS + '/run'
    public = client.get('/processors/translations').get_data(as_text=True)
    assert 'aria-label="Проверить всё с нуля"' not in public
    set_session(client, 'OtherUser')
    assert client.post(target, data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 403
    set_session(client, 'admin')
    assert 'aria-label="Проверить всё с нуля"' in client.get('/processors/translations').get_data(as_text=True)
    assert client.post(target, data={'mode': 'recheck'}).status_code == 400
    assert client.post(target, data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 302
    assert client.post(target, data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 302
    assert len(store.queue(CATS)) == 1 and store.checked_articles(CATS)
    assert client.post('/admin/tasks/maintenance-dates/run', data={'csrf': 'csrf-test', 'mode': 'recheck'}).status_code == 400
    with client.session_transaction() as session:
        session.pop('username')
        session['admin_preview'] = True
    client.post('/admin/tasks/' + TALK + '/run', data={'csrf': 'csrf-test', 'mode': 'recheck'})
    assert not store.queue(TALK) and store.checked_articles(CATS)


def test_missing_talk_data_is_a_persistent_skip_with_no_manual_count(settings, store):
    settings.wiki_write = True
    wiki = TranslationWiki(talk='Обсуждение статьи без шаблона перевода')
    worker = TranslationWorker(settings, store, TALK, wiki)
    worker.execute(job(store, TALK))
    worker.execute(job(store, TALK))
    current = report(store, TALK)
    assert current['skipped'] == 1 and current['problems'] == 0 and current['to_process'] == 0
    assert current['skipped_articles'][0]['reason'] == 'На СО нет шаблона с данными перевода'
    assert sum(call == ('fetch', 'Обсуждение:Пример') for call in wiki.calls) == 1
    overview = build_overview(settings, store)
    assert overview['total_problems'] == 0
    summaries = [json.loads(run['summary']) for run in store.list_runs(processor=TALK)]
    assert sorted(item['skipped_added'] for item in summaries) == [0, 1]
    assert all(item['report_skipped'] == 1 for item in summaries)


@pytest.mark.parametrize('text,comment', [
    ('{{Грубый перевод|язык=de}}', '[[:en:Original title]]'),
    ('{{Грубый перевод}}', '[[:en:One]][[:de:Two]]'),
])
def test_conflicting_translation_sources_remain_problems_instead_of_skips(settings, store, text, comment):
    settings.wiki_write = True
    wiki = TranslationWiki(text=text, comment=comment)
    worker = TranslationWorker(settings, store, CATS, wiki)
    worker.execute(job(store, CATS))
    worker.execute(job(store, CATS))
    current = report(store, CATS)
    assert not wiki.edits and current['problems'] == 1 and current['skipped'] == 0
    assert current['manual'][0]['reason'] and current['to_process'] == 0
    summaries = [json.loads(run['summary']) for run in store.list_runs(processor=CATS)]
    assert sum(item['problems'] for item in summaries) == 1 and sum(item['skipped'] for item in summaries) == 0
    assert sorted(item['problems_added'] for item in summaries) == [0, 1]
    assert all(item['report_problems'] == 1 for item in summaries)


def test_imported_and_saved_result_skips_have_neutral_public_report(settings, store):
    wiki = TranslationWiki()
    refresh_inventory(wiki, store, CATS)
    store.record_article_checks(CATS, [dict(title='Пример', outcome='legacy',
        reason='комментарий имеет другой формат', checked_at=time.time(), run_id='')])
    # An older snapshot stored a no-data result under "manual" without its outcome.
    store.set_state(CATS + ':result', dict(signature=signature(get_config(store, CATS)), at=time.time(),
        manual=[dict(title='Пример', reason=LABELS['comment-source-missing'])]))
    current = report(store, CATS)
    assert current['problems'] == 0 and current['skipped'] == 1 and current['to_process'] == 0
    client = create_app(settings, store).test_client()
    skipped = client.get('/processors/translations?view=skipped').get_data(as_text=True)
    assert 'Пропущенные статьи' in skipped and 'В комментарии к созданию нет ссылки на оригинал' in skipped
    assert 'Исправить 1' not in skipped and 'Проверьте' not in skipped
    default_page = client.get('/processors/translations').get_data(as_text=True)
    assert 'Новые статьи к обработке' in default_page
    assert 'Пропущено 1' in default_page
    assert '?task=translations-categories&amp;view=skipped' in client.get('/').get_data(as_text=True)
    assert client.get('/processors/maintenance?view=skipped').status_code == 404
