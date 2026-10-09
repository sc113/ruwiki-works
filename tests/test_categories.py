import json
import time
from datetime import datetime
from unittest.mock import Mock

import pytest

from toolforge_app.processors.categories.config import defaults, parse_form
from toolforge_app.processors.categories.formats import SOURCE_PAGE, expected_category, parse_formats
from toolforge_app.processors.categories.inventory import (api_snapshot, refresh_inventory, report, snapshot_key,
                                                         missing_page, refresh_formats)
from toolforge_app.processors.categories.service import CategoryWorker, CategoryMonitor
from toolforge_app.schedules import get_schedule, next_weekly_time, save_schedule
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiClient, WikiError
from test_web import set_session

CREATE, FORMAT = 'categories-create', 'categories-format'
TABLE = '''{| class="wikitable"
! colspan="2" |GenerateTemplates: надстрочные
|-
| "Нет источников с <text_month_year>"
| <nowiki>{{Категория обслуживания|desc=С <month_name> <year> года.}}\\n[[Категория:Служебная|<yyyy-mm>]]</nowiki>
|-
! colspan="2" |GenerateTemplates2: надстрочные
|-
| "Неавторитетный источник с <text_month_year>" || <nowiki>Проверить авторитетность</nowiki>
|-
! colspan="2" |GenerateTemplates2: боксы
|-
| "Грубый перевод с <text_month_year> года" || <nowiki>Грубый перевод|par=язык|val=en</nowiki>
|}'''
TITLE = 'Категория:Википедия:Грубый перевод с октября 2026 года'
OTHER = 'Категория:Википедия:Неавторитетный источник с октября 2026'


def test_wiki_table_pipes_blocks_and_placeholders():
    definitions = parse_formats(TABLE)
    assert len(definitions['simple']) == 2 and len(definitions['complex']) == 1
    config = defaults(CREATE)
    ordinary = expected_category(TITLE, definitions, config)
    assert ordinary['text'] == '{{Категория к ежемесячной очистке|Грубый перевод|par=язык|val=en}}'
    assert ordinary['summary'] == 'Создание категории обслуживания с шаблоном ' + ordinary['text']
    complex = expected_category('Категория:Википедия:Нет источников с января 2010', definitions, config)
    assert complex['text'] == '{{Категория обслуживания|desc=С января 2010 года.}}\n[[Категория:Служебная|2010-01]]'
    assert complex['summary'] == 'Создание категории обслуживания для надстрочного шаблона'
    assert not expected_category('Статья', definitions, config)
    assert not expected_category(TITLE.replace('Википедия:', ''), definitions, config)


@pytest.mark.parametrize('bad', [TABLE.replace('GenerateTemplates2: боксы', 'Другое'),
                               TABLE.replace('<text_month_year>', '<unknown>'),
                               TABLE.replace('"Грубый перевод с <text_month_year> года"', '"Нечто"'),
                               TABLE.replace('Проверить авторитетность</nowiki>', 'Проверить авторитетность}}}</nowiki>')])
def test_malformed_source_stops_instead_of_using_partial_table(bad):
    with pytest.raises(WikiError):
        parse_formats(bad)


class CategoryWiki:
    def __init__(self):
        self.table = TABLE
        self.source_revision = 100
        self.data = {TITLE: Revision(TITLE, missing=True), OTHER: Revision(OTHER, 7, 1, 'old formatting')}
        self.population = {TITLE: 3, OTHER: 2, 'Категория:Другое': 5}
        self.edits, self.reads = [], []
        self.request_guard = lambda: None
        self.error = None

    def fetch(self, title):
        return Revision(SOURCE_PAGE, self.source_revision, 1, self.table)

    def scan(self, prefix):
        return dict(missing=[(title, count) for title, count in self.population.items()
                             if count and (title not in self.data or self.data[title].missing)],
                    existing=[(title, base.revision) for title, base in self.data.items() if not base.missing],
                    backend='replica', complete=True)

    def category_pages(self, titles):
        self.request_guard()
        self.reads.extend(titles)
        return {title: dict(base=self.data[title], population=self.population.get(title, 0)) for title in titles}

    def edit_category(self, base, text, summary, allowed, create=False):
        self.request_guard()
        if self.error:
            raise WikiError(self.error)
        assert base.title in allowed and base.missing == create
        assert self.data[base.title].revision == base.revision
        self.edits.append((base.title, text, summary, create))
        self.data[base.title] = Revision(base.title, base.revision + 1, time.time(), text)
        return base.revision + 1


@pytest.fixture
def category_wiki(monkeypatch):
    wiki = CategoryWiki()
    monkeypatch.setattr('toolforge_app.processors.categories.inventory.replica_snapshot', wiki.scan)
    return wiki


def queue(store, slug, kind='full'):
    store.enqueue('test:' + str(time.time_ns()), kind, time.time(), processor=slug)
    return next(job for job in store.queue(slug) if job['kind'] == kind)


def test_creation_reads_current_population_and_uses_original_comment(settings, store, category_wiki):
    settings.wiki_write = True
    worker = CategoryWorker(settings, store, CREATE, category_wiki)
    worker.execute(queue(store, CREATE))
    assert len(category_wiki.edits) == 1 and category_wiki.edits[0][3]
    assert category_wiki.edits[0][2] == 'Создание категории обслуживания с шаблоном ' + category_wiki.edits[0][1]
    assert report(store, CREATE)['to_process'] == 0
    run = store.list_runs(processor=CREATE)[0]
    assert run['status'] == 'success' and json.loads(run['summary'])['changed'] == 1
    assert len(snapshot_key('Википедия:') + ':header') <= 80
    assert 'formats' not in json.loads(run['report'])


def test_dry_run_never_saves_or_marks_categories_checked(settings, store, category_wiki):
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert not category_wiki.edits and not store.checked_articles(FORMAT)
    assert json.loads(store.list_runs(processor=FORMAT)[0]['summary'])['proposed'] == 1


def test_formatter_incremental_revision_and_affected_format_invalidation(settings, store, category_wiki):
    settings.wiki_write = True
    worker = CategoryWorker(settings, store, FORMAT, category_wiki)
    worker.execute(queue(store, FORMAT))
    assert category_wiki.edits[0][2:] == ('обновление оформления категории', False)
    category_wiki.reads.clear()
    worker.execute(queue(store, FORMAT))
    assert not category_wiki.reads
    # A different format changes; this category's checked proof remains valid.
    category_wiki.table = TABLE.replace('val=en', 'val=fr')
    category_wiki.source_revision += 1
    worker.execute(queue(store, FORMAT))
    assert not category_wiki.reads
    category_wiki.table = category_wiki.table.replace('Проверить авторитетность</nowiki>', 'Проверить авторитетность|x=1</nowiki>')
    category_wiki.source_revision += 1
    worker.execute(queue(store, FORMAT))
    assert category_wiki.reads == [OTHER] and len(category_wiki.edits) == 2
    category_wiki.reads.clear()
    base = category_wiki.data[OTHER]
    category_wiki.data[OTHER] = Revision(OTHER, base.revision + 1, time.time(), base.text + '\nExtra')
    worker.execute(queue(store, FORMAT))
    assert category_wiki.reads == [OTHER]


def test_month_end_forces_full_check_and_keeps_history(settings, store, category_wiki):
    settings.wiki_write = True
    worker = CategoryWorker(settings, store, FORMAT, category_wiki)
    worker.execute(queue(store, FORMAT))
    category_wiki.reads.clear()
    worker.execute(queue(store, FORMAT, 'month_end'))
    assert category_wiki.reads == [OTHER] and len(store.list_runs(processor=FORMAT)) == 2
    assert len(category_wiki.edits) == 1


def test_changed_source_is_validated_on_every_run_and_old_cache_not_used(settings, store, category_wiki):
    settings.wiki_write = True
    CategoryWorker(settings, store, CREATE, category_wiki).execute(queue(store, CREATE))
    category_wiki.data[TITLE] = Revision(TITLE, missing=True)
    category_wiki.table = 'broken table'
    category_wiki.source_revision += 1
    CategoryWorker(settings, store, CREATE, category_wiki).execute(queue(store, CREATE))
    run = store.list_runs(processor=CREATE)[0]
    assert run['status'] == 'failed' and len(category_wiki.edits) == 1
    assert store.get_state(CREATE + ':worker_error')['code'] == 'category-formats-invalid'


def test_creation_race_or_empty_population_never_overwrites(settings, store, category_wiki):
    settings.wiki_write = True
    original = category_wiki.category_pages
    def raced(titles):
        category_wiki.data[TITLE] = Revision(TITLE, 50, 1, 'Human-created content')
        return original(titles)
    category_wiki.category_pages = raced
    CategoryWorker(settings, store, CREATE, category_wiki).execute(queue(store, CREATE))
    assert not category_wiki.edits
    category_wiki.data[TITLE] = Revision(TITLE, missing=True)
    def emptied(titles):
        category_wiki.population[TITLE] = 0
        return original(titles)
    category_wiki.category_pages = emptied
    CategoryWorker(settings, store, CREATE, category_wiki).execute(queue(store, CREATE))
    assert not category_wiki.edits


def test_exclusions_and_page_failures_are_not_run_errors(settings, store, category_wiki):
    settings.wiki_write = True
    category_wiki.error = 'protectedpage'
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert store.list_runs(processor=FORMAT)[0]['status'] == 'success' and report(store, FORMAT)['problems'] == 1
    category_wiki.data[OTHER] = Revision(OTHER, 8, 1, '{{nobots}}')
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert report(store, FORMAT)['excluded'] and report(store, FORMAT)['problems'] == 0
    category_wiki.error = 'network'
    category_wiki.data[OTHER] = Revision(OTHER, 9, 1, 'old')
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert store.list_runs(processor=FORMAT)[0]['status'] == 'failed'


def test_public_reports_and_admin_controls(settings, store, category_wiki):
    CategoryMonitor(settings, store, category_wiki).tick(force=True)
    client = create_app(settings, store).test_client()
    public = client.get('/processors/categories').get_data(as_text=True)
    assert 'Создание категорий' in public and 'Проверка оформления' in public and 'Без страницы' in public
    assert '/admin/tasks/categories-create/settings' not in public
    missing = client.get('/processors/categories?view=missing&q=Другое').get_data(as_text=True)
    assert 'Другое' in missing and '5 страниц' in missing
    set_session(client, 'admin')
    admin = client.get('/processors/categories?task=categories-format').get_data(as_text=True)
    assert '/admin/tasks/categories-format/settings' in admin and 'С нуля' in admin
    assert 'Полная проверка в конце месяца' in admin and 'Понедельник' in admin
    settings.wiki_write = True
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    run = store.list_runs(processor=FORMAT)[0]
    anonymous = create_app(settings, store).test_client()
    log = anonymous.get('/runs/' + run['id'] + '/log.json').get_json()
    assert len(log['events']) == 1 and log['events'][0]['title'] == OTHER
    assert 'configuration' not in json.dumps(log) and 'diff_before' not in json.dumps(log)


def test_weekly_and_month_end_schedule_are_independent_and_editable(settings, store):
    assert get_schedule(settings, store, FORMAT) == dict(search_minutes=360, run_time='05:00', weekday=0, month_end_time='05:00')
    now = datetime(2026, 10, 9, 12, tzinfo=settings.zone).timestamp()
    CategoryWorker(settings, store, FORMAT).schedule(now)
    jobs = store.queue(FORMAT)
    assert {job['kind'] for job in jobs} == {'weekly', 'month_end'}
    dates = {job['kind']: datetime.fromtimestamp(job['due_at'], settings.zone).strftime('%Y-%m-%d %H:%M') for job in jobs}
    assert dates == {'weekly': '2026-10-12 05:00', 'month_end': '2026-10-31 05:00'}
    save_schedule(settings, store, FORMAT, dict(run_time='06:00', weekday='2', month_end_time='07:00', search_minutes='360'), 'admin')
    assert get_schedule(settings, store, FORMAT)['weekday'] == 2
    assert not store.queue(FORMAT)
    assert next_weekly_time('05:00', 0, datetime(2026, 10, 12, 5, tzinfo=settings.zone).timestamp()) == datetime(2026, 10, 19, 5, tzinfo=settings.zone).timestamp()


def test_api_creation_flags_and_scope_guard(settings):
    wiki = WikiClient(settings)
    wiki.login = Mock()
    wiki.request = Mock(side_effect=[{'query': {'tokens': {'csrftoken': 'token'}}}, {'edit': {'result': 'Success', 'newrevid': 1}}])
    wiki.edit_category(Revision(TITLE, missing=True), 'new', 'comment', {TITLE}, create=True)
    params = wiki.request.call_args.args[0]
    assert params['createonly'] == 1 and 'nocreate' not in params and 'basetimestamp' not in params
    assert params['bot'] == 1 and params['notminor'] == 1 and 'minor' not in params
    with pytest.raises(WikiError, match='articleexists'):
        wiki.edit_category(Revision(TITLE, 1, 1, 'human'), 'new', 'comment', {TITLE}, create=True)
    with pytest.raises(WikiError, match='outside-scope'):
        wiki.edit_category(Revision('Статья', missing=True), 'new', 'comment', {'Статья'}, create=True)


def test_api_fallback_paginates_and_marks_global_report_incomplete(settings):
    wiki = WikiClient(settings)
    wiki.request = Mock(side_effect=[
        {'query': {'pages': [{'title': TITLE, 'missing': True, 'categoryinfo': {'size': 3}}]}, 'continue': {'gaccontinue': 'NEXT'}},
        {'query': {'pages': [{'title': OTHER, 'categoryinfo': {'size': 2}}]}},
        {'query': {'pages': [{'title': OTHER, 'lastrevid': 7}]}}])
    result = api_snapshot(wiki, 'Википедия:')
    assert result['missing'] == [(TITLE, 3)] and result['existing'] == [(OTHER, 7)]
    assert not result['complete'] and result['backend'] == 'api'
    assert wiki.request.call_args_list[1].args[0]['gaccontinue'] == 'NEXT'


def test_month_end_monday_has_one_full_job(settings, store):
    now = datetime(2026, 11, 29, 12, tzinfo=settings.zone).timestamp()
    worker = CategoryWorker(settings, store, FORMAT)
    worker.schedule(now)
    worker.schedule(now + 1)
    assert len(store.queue(FORMAT)) == 1 and store.queue(FORMAT)[0]['kind'] == 'month_end'


def test_mixed_category_batch_keeps_redirects_and_hidden_content_local(settings):
    wiki = WikiClient(settings)
    wiki.request = Mock(return_value={'query': {'pages': [
        {'title': TITLE, 'missing': True, 'categoryinfo': {'size': 3}},
        {'title': OTHER, 'redirect': True},
        {'title': 'Категория:Скрытая', 'revisions': [{'revid': 1}]},
    ]}})
    rows = wiki.category_pages([TITLE, OTHER, 'Категория:Скрытая'])
    assert rows[TITLE]['base'].missing and rows[TITLE]['population'] == 3
    assert rows[OTHER]['error'] == 'redirect' and rows['Категория:Скрытая']['error'] == 'hidden-content'
    assert 'redirects' not in wiki.request.call_args.args[0]


def test_replica_lag_does_not_leave_saved_categories_pending(settings, store, category_wiki, monkeypatch):
    settings.wiki_write = True
    stale = category_wiki.scan('Википедия:')
    monkeypatch.setattr('toolforge_app.processors.categories.inventory.replica_snapshot', lambda prefix: stale)
    CategoryWorker(settings, store, CREATE, category_wiki).execute(queue(store, CREATE))
    assert len(category_wiki.edits) == 1 and report(store, CREATE)['to_process'] == 0
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert report(store, FORMAT)['to_process'] == 0
    category_wiki.reads.clear()
    CategoryWorker(settings, store, FORMAT, category_wiki).execute(queue(store, FORMAT))
    assert not category_wiki.reads
