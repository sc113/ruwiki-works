import json
import time
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import pytest

from toolforge_app.processors.projects import PREFIX
from toolforge_app.processors.projects.config import defaults, parse_form
from toolforge_app.processors.projects.inventory import discover, refresh, report
from toolforge_app.processors.projects.rules import analyze, edit_comment, format_discussion, page_report
from toolforge_app.processors.projects.service import ProjectMonitor, ProjectWorker
from toolforge_app.schedules import get_schedule
from toolforge_app.statistics import statistics
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiClient, WikiError
from test_web import set_session

NAV = '<noinclude>{{ОБПРО-Навигация}}</noinclude>\n\n'
TITLE = PREFIX + '2026'
OPEN = NAV + '== [[Проект:Пример]] ==\nОбоснование и обсуждение.\n'
CLOSED = OPEN + '=== Итог ===\nОбсуждение завершено.\n'


def test_level_two_closure_navigation_and_idempotence():
    text, closed, error = format_discussion(CLOSED)
    assert '== <s>[[Проект:Пример]]</s> ==' in text
    assert '=== Итог ===' in text and '<s>Итог</s>' not in text
    assert text.startswith('<noinclude>{{ОБПРО-Навигация|closed=1}}</noinclude>')
    assert closed and not error and format_discussion(text)[0] == text
    assert text.split('=== Итог ===', 1)[1] == CLOSED.split('=== Итог ===', 1)[1]


@pytest.mark.parametrize('title', ['Предварительный итог', 'Оспоренный итог', 'Неитог', '<s>Итог</s>'])
def test_only_active_final_conclusion_closes(title):
    text, closed, _ = format_discussion(OPEN + '=== ' + title + ' ===\nОбсуждаем.\n')
    assert not closed and 'closed=1' not in text
    assert '== [[Проект:Пример]] ==' in text
    assert any(issue['type'] == 'no_itog' for issue in analyze(text))


def test_spacing_rules_are_idempotent_and_preserve_discussion_text():
    source = '<noinclude>{{ОБПРО-Навигация}}</noinclude>\n==Первый==\n\n\nОбсуждение.\n===Итог===\n\nРешено.\n\n\n==Второй==\n===Подноминация===\n\nТекст.\n\n'
    result, _, _ = format_discussion(source, spacing=True)
    assert '}}</noinclude>\n\n== <s>Первый</s> ==\nОбсуждение.\n\n=== Итог ===\nРешено.' in result
    assert '== Второй ==\n=== Подноминация ===\nТекст.' in result
    assert '\n\n\n' not in result and result.endswith('Текст.')
    assert format_discussion(result, spacing=True)[0] == result


def test_partial_subnominations_and_conclusion_children():
    source = OPEN + '=== Первый ===\nПодноминация.\n==== Итог ====\nРешение.\n=== Второй ===\nОткрыта.\n'
    text, closed, _ = format_discussion(source)
    assert '=== <s>Первый</s> ===' in text and '=== Второй ===' in text
    assert not closed and '== [[Проект:Пример]] ==' in text
    assert {'no_itog', 'sub_itog_no_main'} <= {issue['type'] for issue in analyze(text)}
    whole = source + '=== Итог ===\nОбщий итог.\n==== Дополнение к решению ====\nТекст.\n'
    result, closed, _ = format_discussion(whole)
    assert closed and '=== <s>Второй</s> ===' in result
    assert '==== Дополнение к решению ====' in result


def test_edit_comments_describe_only_actual_changes():
    before = OPEN.replace('== [[Проект:Пример]] ==', '==[[Проект:Пример]]==').strip()
    after = format_discussion(before, spacing=True)[0]
    assert edit_comment(before, after) == 'форматирование заголовков и пустых строк'
    after = format_discussion(CLOSED, spacing=True)[0]
    comment = edit_comment(CLOSED, after)
    assert 'зачёркивание завершённых обсуждений' in comment
    assert 'отметка закрытого года в [[ш:ОБПРО-Навигация]]' in comment
    assert 'обновление навигации' not in comment
    bare = OPEN.replace('<noinclude>{{ОБПРО-Навигация}}</noinclude>', '{{ОБПРО-Навигация}}').strip()
    assert edit_comment(bare, format_discussion(bare)[0]) == 'оформление [[ш:ОБПРО-Навигация]]'


def test_existing_archive_marker_is_preserved_and_navigation_is_normalized():
    source = '{{ОБПРО-Навигация|closed=1}}\n' + OPEN.removeprefix(NAV)
    result, closed, _ = format_discussion(source)
    assert not closed and result.startswith('<noinclude>{{ОБПРО-Навигация|closed=1}}</noinclude>')
    assert format_discussion('<noinclude>{{ОБПРО-Навигация|closed=1}}\n</noinclude>\n' + CLOSED.removeprefix(NAV))[1]


def test_no_invented_conclusions_and_no_closure_of_empty_pages():
    for source in ('{{ОБПРО-Навигация}}\n', OPEN):
        result, closed, _ = format_discussion(source)
        assert not closed and 'closed=' not in result and '=== Итог ===' not in result


def test_ambiguous_navigation_is_not_rewritten():
    source = '{{ОБПРО-Навигация}}\n{{ОБПРО-Навигация|closed=1}}\n' + CLOSED.removeprefix(NAV)
    result, _, error = format_discussion(source)
    assert error and result.count('{{ОБПРО-Навигация') == 2
    assert result.split('== ', 1)[0] == source.split('== ', 1)[0]


def test_comments_examples_and_their_spacing_are_untouched():
    example = '<nowiki>\n== Example ==\n\n=== Итог ===\n</nowiki>\n<!--\n== Fake ==\n=== Итог ===\n-->\n'
    source = NAV + example + CLOSED.removeprefix(NAV)
    text, closed, _ = format_discussion(source, spacing=True)
    assert example in text and closed
    assert not any('Fake' in issue['title'] or 'Example' in issue['title'] for issue in analyze(source))


def test_structural_checks_use_project_levels_and_rename_subjects():
    good = OPEN + '=== Комментарий ===\nРеплика.\n=== Итог ===\nИтог.\n'
    assert not any(i['type'] == 'service_wrong_level' for i in analyze(good))
    bad = OPEN + '==== Итог ====\nИтог.\n'
    assert {'level_skip', 'wrong_itog_level'} <= {i['type'] for i in analyze(bad)}
    duplicate = OPEN + '== [[Проект:Пример|Другой ярлык]] ==\nПовторное обсуждение.\n'
    assert any(i['type'] == 'duplicate_nomination' for i in analyze(duplicate))
    rename = NAV + '== [[Проект:A]] → [[Проект:B]] ==\nТекст.\n== [[Проект:B]] → [[Проект:C]] ==\nТекст.\n'
    assert not any(i['type'] == 'duplicate_nomination' for i in analyze(rename))


def test_malformed_heading_is_not_struck():
    source = NAV + '== <s>Broken ==\nОбсуждение.\n=== Итог ===\nИтог.\n'
    result, _, _ = format_discussion(source)
    assert '== <s>Broken ==' in result and '<s><s>' not in result
    assert any(i['type'] == 'unclosed_tag' for i in analyze(result))


class ProjectWiki:
    def __init__(self, source=CLOSED):
        self.data = {TITLE: Revision(TITLE, 10, time.time() - 100, source.strip())}
        self.edits, self.content_reads, self.metadata_reads = [], [], []
        self.request_guard = lambda: None
        self.error = None

    def request(self, params):
        self.request_guard()
        assert params['apfilterredir'] in {'all', 'redirects', 'nonredirects'}
        return {'query': {'allpages': [{'title': title} for title in self.data]}}

    def revisions(self, titles, content=False):
        self.request_guard()
        if content:
            self.content_reads.extend(titles)
        else:
            self.metadata_reads.extend(titles)
        return [self.data[title] if content else replace(self.data[title], text='') for title in titles]

    def edit_project_discussion(self, base, text, summary, allowed):
        self.request_guard()
        if self.error:
            raise WikiError(self.error)
        assert base.title in allowed and self.data[base.title].revision == base.revision
        self.edits.append((base.title, text, summary))
        self.data[base.title] = replace(base, revision=base.revision + 1, text=text.strip())
        return base.revision + 1


def queue(store, kind='full'):
    key = 'test:' + str(time.time_ns())
    store.enqueue(key, kind, time.time(), processor='projects')
    return next(j for j in store.queue('projects') if j['key'] == 'projects:' + key)


def test_discovery_continuation_and_scope(settings):
    wiki = Mock()
    wiki.request.side_effect = [dict(query=dict(allpages=[dict(title=PREFIX + '2027')]), **{'continue': {'apcontinue':'next'}}),
                               dict(query=dict(allpages=[dict(title=PREFIX + '2009'), dict(title=PREFIX + '2026/Other'), dict(title=PREFIX + '2008')]))]
    assert discover(wiki, defaults('projects')) == [PREFIX + '2027', PREFIX + '2009']
    assert wiki.request.call_args_list[1].args[0]['apcontinue'] == 'next'


def test_monitor_reads_only_new_revisions_and_tracks_actual_observations(settings, store):
    wiki = ProjectWiki(OPEN + '==== Итог ====\nОшибка уровня.\n')
    monitor = ProjectMonitor(settings, store, wiki)
    assert monitor.tick(now=1000, force=True)
    first = report(store)['items'][0]['problem_history']['first_seen']
    assert first == 1000 and len(wiki.content_reads) == 1
    monitor.tick(now=2000, force=True)
    assert len(wiki.content_reads) == 1
    assert report(store)['items'][0]['problem_history']['last_seen'] == 2000
    wiki.data[TITLE] = replace(wiki.data[TITLE], revision=11, text=CLOSED)
    monitor.tick(now=3000, force=True)
    assert len(wiki.content_reads) == 2 and report(store)['problems'] == 0
    assert not wiki.edits


def test_monitor_observes_every_twelve_hours_and_respects_saved_interval(settings, store):
    wiki = ProjectWiki(OPEN)
    monitor = ProjectMonitor(settings, store, wiki)
    assert monitor.tick(now=1000)
    reads = len(wiki.metadata_reads)
    assert not monitor.tick(now=44199)
    assert len(wiki.metadata_reads) == reads
    assert monitor.tick(now=44200)
    assert len(wiki.metadata_reads) == reads + 1
    store.set_state('projects:config', {'search_minutes': 60})
    assert monitor.tick(now=47800)
    assert get_schedule(settings, store, 'projects')['search_minutes'] == 60


def test_daily_worker_saves_only_changed_annual_pages_without_summary(settings, store):
    settings.wiki_write = True
    wiki = ProjectWiki()
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert len(wiki.edits) == 1 and wiki.edits[0][0] == TITLE
    assert 'ОБКАТ' not in wiki.edits[0][2] and report(store)['to_process'] == 0
    run = store.list_runs(processor='projects')[0]
    assert run['status'] == 'success' and json.loads(run['summary'])['changed'] == 1
    assert run['table_text'] == ''
    worker.execute(queue(store))
    assert len(wiki.edits) == 1
    assert json.loads(store.list_runs(processor='projects')[0]['summary'])['changed'] == 0


def test_dry_run_keeps_pending_changes_and_never_saves(settings, store):
    wiki = ProjectWiki()
    ProjectWorker(settings, store, wiki=wiki).execute(queue(store))
    assert not wiki.edits and report(store)['to_process'] == 1
    run = store.list_runs(processor='projects')[0]
    assert run['dry_run'] and json.loads(run['summary'])['proposed'] == 1
    assert not store.get_state('projects:completed_years', {})


def test_mediawiki_normalization_does_not_requeue_or_count_empty_edits(settings, store):
    settings.wiki_write = True
    wiki = ProjectWiki(OPEN)
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert not wiki.edits and report(store)['to_process'] == 0
    # The API can report Success with the old revision for an empty edit.
    wiki.data[TITLE] = replace(wiki.data[TITLE], text=wiki.data[TITLE].text + '\n')
    wiki.edit_project_discussion = Mock(return_value=10)
    worker.execute(queue(store))
    run = store.list_runs(processor='projects')[0]
    assert json.loads(run['summary'])['changed'] == 0
    assert any(event['code'] == 'unchanged' for event in json.loads(run['events']))
    assert not any(event['code'] == 'edited' for event in json.loads(run['events']))


def test_archive_without_formal_conclusions_is_reported_and_kept_active(settings, store):
    settings.wiki_write = True
    source = OPEN.replace('{{ОБПРО-Навигация}}', '{{ОБПРО-Навигация|closed=1}}')
    wiki = ProjectWiki(source)
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert not wiki.edits and not report(store)['completed_years']
    issues = report(store)['items']
    assert len(issues) == 1 and issues[0]['type'] == 'archive_open' and issues[0]['section'] == 'problems'
    assert issues[0]['problem_history']['first_seen']
    wiki.data[TITLE] = replace(wiki.data[TITLE], revision=11, text=source + '=== Итог ===\nОформленное решение.\n')
    worker.execute(queue(store))
    assert len(wiki.edits) == 1 and report(store)['completed_years'] == ['2026']
    assert report(store)['problems'] == 0


def test_closed_parent_with_incomplete_subnominations_stays_under_review():
    source = NAV.replace('{{ОБПРО-Навигация}}', '{{ОБПРО-Навигация|closed=1}}') + '== <s>Группа</s> ==\n=== Первый ===\n==== Итог ====\nРешено.\n=== Второй ===\nЕщё обсуждается.\n'
    row = page_report(Revision(TITLE, 10, text=source), 1000)
    assert any(item['type'] == 'archive_open' and item['section'] == 'problems' for item in row['items'])


def test_discussion_edits_are_pages_and_do_not_invent_template_operations(settings, store):
    settings.wiki_write = True
    wiki = ProjectWiki(CLOSED.replace('{{ОБПРО-Навигация}}', '{{ОБПРО-Навигация|closed=1}}'))
    ProjectWorker(settings, store, wiki=wiki).execute(queue(store))
    value = statistics(store, settings.zone)['total']
    assert value['edits'] == 1 and value['pages'] == 1
    assert value['articles'] == value['templates'] == 0


def test_completed_years_leave_regular_reads_and_explicit_recheck_reopens_them(settings, store):
    settings.wiki_write = True
    wiki = ProjectWiki()
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert report(store)['completed_years'] == ['2026']
    assert store.get_state('projects:completed_years')[TITLE]['revision'] == 11
    read_count = len(wiki.content_reads)
    wiki.data[TITLE] = replace(wiki.data[TITLE], text=OPEN, revision=12)
    refresh(wiki, store, defaults('projects'), now=time.time())
    worker.execute(queue(store))
    assert len(wiki.content_reads) == read_count and not wiki.metadata_reads
    summary = json.loads(store.list_runs(processor='projects')[0]['summary'])
    assert summary['checked'] == 0 and summary['previously_checked'] == 1
    worker.execute(queue(store, 'recheck'))
    assert len(wiki.content_reads) == read_count + 1
    assert report(store)['completed_years'] == []
    assert any(item['type'] == 'no_itog' for item in report(store)['items'])


def test_closed_year_with_structural_problems_is_not_retired(settings, store):
    settings.wiki_write = True
    source = CLOSED + '==== Другой итог ====\n===== Итог =====\nНеправильная вложенность.\n'
    wiki = ProjectWiki(source)
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert report(store)['problems'] > 0 and not report(store)['completed_years']
    read_count = len(wiki.content_reads)
    worker.execute(queue(store))
    assert len(wiki.content_reads) == read_count + 1 and len(wiki.edits) == 1


def test_retiring_archives_keeps_discovery_of_new_years(settings, store):
    wiki = ProjectWiki(format_discussion(CLOSED, spacing=True)[0])
    refresh(wiki, store, defaults('projects'), now=1000)
    assert report(store)['completed_years'] == ['2026']
    wiki.data[PREFIX + '2027'] = Revision(PREFIX + '2027', 20, 1000, OPEN)
    refresh(wiki, store, defaults('projects'), now=2000)
    assert wiki.metadata_reads == [TITLE, PREFIX + '2027']
    assert report(store)['pages'] == 2 and report(store)['completed_years'] == ['2026']


def test_project_fix_guidance_and_group_checks_use_level_two(settings, store):
    source = OPEN + '=== Подноминация ===\nОбсуждение.\n=== По всем ===\nТекст.\n'
    assert any(issue['type'] == 'group_for_one' for issue in analyze(source))
    source = '{{ОБПРО-Навигация}}\n{{ОБПРО-Навигация|closed=1}}\n' + OPEN + '== Комментарий ==\nТекст.\n'
    row = page_report(Revision(TITLE, 10, text=source), time.time())
    issues = {item['type']: item for item in row['items']}
    assert 'третьего уровня' in issues['service_wrong_level']['fix']
    assert 'Несколько навигационных' in issues['navigation']['fix']
    refresh(ProjectWiki(source), store, defaults('projects'), now=time.time())
    client = create_app(settings, store).test_client()
    html = client.get('/processors/projects').get_data(as_text=True)
    assert 'третьего уровня' in html and 'Несколько навигационных' in html
    assert 'Оформление навигации' in client.get('/reports/projects.txt').get_data(as_text=True)


def test_nobots_and_network_failure(settings, store):
    settings.wiki_write = True
    wiki = ProjectWiki('{{nobots}}\n' + CLOSED)
    worker = ProjectWorker(settings, store, wiki=wiki)
    worker.execute(queue(store))
    assert not wiki.edits and report(store)['to_process'] == 0
    wiki.data[TITLE] = replace(wiki.data[TITLE], text=CLOSED)
    wiki.error = 'network'
    worker.execute(queue(store))
    assert store.list_runs(processor='projects')[0]['status'] == 'failed'
    assert store.get_state('projects:worker_error')['code'] == 'network'


def test_daily_deadline_and_shared_public_admin_interface(settings, store):
    worker = ProjectWorker(settings, store, wiki=ProjectWiki())
    now = datetime(2026, 10, 11, 5, 55, tzinfo=settings.zone).timestamp()
    worker.schedule(now)
    due = store.queue('projects')[0]['due_at']
    assert datetime.fromtimestamp(due, settings.zone).strftime('%H:%M') == '06:00'
    assert get_schedule(settings, store, 'projects') == {'run_time': '06:00', 'search_minutes': 720}
    ProjectMonitor(settings, store, ProjectWiki()).tick(force=True)
    client = create_app(settings, store).test_client()
    for path in ('/', '/processors/projects', '/processors/projects?view=open', '/processors/projects?view=pending',
                 '/processors/projects?view=logs', '/tasks/projects/status-fragment', '/reports/projects.json'):
        assert client.get(path).status_code == 200
    public = client.get('/processors/projects').get_data(as_text=True)
    assert '/admin/tasks/projects/settings' not in public
    set_session(client, settings.admin_username)
    admin = client.get('/processors/projects').get_data(as_text=True)
    assert '/admin/tasks/projects/settings' in admin and '/admin/tasks/projects/schedule' in admin
    assert 'name="autosave"' in admin and 'name="spacing"' in admin
    assert 'Перепроверить архивы' in admin
    assert client.post('/admin/tasks/projects/run', data={'csrf': 'csrf-test'}).status_code == 302


def test_wiki_edit_scope_and_bot_flags(settings):
    wiki = WikiClient(settings)
    wiki._save = Mock(return_value=11)
    base = Revision(TITLE, 10)
    assert wiki.edit_project_discussion(base, CLOSED, 'comment', {TITLE}) == 11
    assert wiki._save.call_args.kwargs == {'minor': False}
    for title, allowed in [(TITLE, set()), (PREFIX + '2026/Other', {PREFIX + '2026/Other'}),
                           ('Википедия:Другое/2026', {'Википедия:Другое/2026'})]:
        with pytest.raises(WikiError, match='outside-scope'):
            wiki.edit_project_discussion(Revision(title, 10), CLOSED, 'comment', allowed)
