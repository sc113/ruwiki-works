import json
from unittest.mock import Mock

import pytest

from toolforge_app.edit_comments import dates_comment, fit_comment, rq_comment, translation_comment
from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.processors.translations.service import TranslationWorker
from toolforge_app.processors.sections.service import SectionWorker
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision, WikiClient
from test_maintenance import MaintenanceWiki, configure, queued, DATES, RQ
from test_translations import TranslationWiki, job, CATS, TALK
from test_sections import SectionWiki, queue, FORWARD
from test_web import set_session


def test_date_comment_matches_complete_template_and_redirect_descriptions():
    row = dict(template='Проверить факты', previous='проверить факты', date='2020-01-02', revision=123)
    assert dates_comment([row]) == 'В [[ш:Проверить факты]] добавлена дата установки: [[Special:Diff/123|2020-01-02]]'
    row['previous'] = 'Факты'
    assert dates_comment([row]) == 'Замена редиректа [[ш:Факты]] на актуальный [[ш:Проверить факты]] с добавлением даты установки: [[Special:Diff/123|2020-01-02]]'


def test_date_comment_groups_sections_and_retains_their_original_names():
    rows = [dict(template='Дополнить раздел', previous='Дополнить раздел', section_scoped=True,
                 section='История клуба', original_section='История команды', date='2020-01-02', revision=123),
            dict(template='Дополнить раздел', previous='Дополнить раздел', section_scoped=True,
                 section='', original_section='', date='2020-02-03', revision=456)]
    comment = dates_comment(rows)
    assert comment.count('[[ш:Дополнить раздел]]') == 1
    assert '«История клуба» (ранее: История команды)' in comment
    assert 'во вводной части статьи ([[Special:Diff/456|2020-02-03]])' in comment


@pytest.mark.parametrize('text,source_note', [('Текст', ''), ('{{Нет источников}}', 'шаблон уже был в статье'),
                                           ('{{source}}', 'редирект [[ш:Source]] уже был в статье')])
def test_rq_comment_retains_history_provenance_and_parameter_names(settings, store, text, source_note):
    configure(store, RQ)
    wiki = MaintenanceWiki([text, '{{Rq|sources|source}}'])
    aliases = wiki.template_aliases
    wiki.template_aliases = lambda name: dict(name='Нет источников', aliases=['Нет источников', 'source']) if name.lower() == 'нет источников' else aliases(name)
    MaintenanceWorker(settings, store, RQ, wiki).execute(queued(store, RQ))
    events = json.loads(store.list_runs(processor=RQ)[0]['events'])
    comment = next(e['edit_summary'] for e in events if e['code'] == 'edit_summary')
    assert 'sources, source → [[ш:Нет источников]]' in comment and '[[Special:Diff/' in comment
    assert source_note in comment


def test_rq_retained_date_is_not_linked_to_a_different_installation_date():
    comment = rq_comment([dict(template='Нет источников', previous='Rq', parameter='sources',
                              action='removed_parameter', date='2017-01-01', source_date='2020-01-02', revision=123)])
    assert 'дата 2017-01-01 сохранена' in comment
    assert '[[Special:Diff/123|2020-01-02]]' in comment
    assert '[[Special:Diff/123|2017-01-01]]' not in comment


@pytest.mark.parametrize('slug,source', [(CATS, 'на основе [[Special:Diff/1|первой правки]]'),
                                     (TALK, 'на [[Обсуждение:Пример|СО]] ([[Special:Diff/20|источник]])')])
def test_saved_translation_comment_lists_both_templates_and_source(settings, store, slug, source):
    settings.wiki_write = True
    wiki = TranslationWiki('{{Грубый перевод}}{{Плохой перевод}}')
    TranslationWorker(settings, store, slug, wiki).execute(job(store, slug))
    comment = wiki.edits[0][2]
    assert '[[ш:Грубый перевод]]: +язык=en, +оригинал=Original title' in comment
    assert '[[ш:Плохой перевод]]: +язык=en, +оригинал=Original title' in comment
    assert source in comment


def test_saved_sections_comment_identifies_alias_section_and_reason(settings, store):
    settings.wiki_write = True
    wiki = SectionWiki('== История ==\n{{Empty section|дата=2020-01-01}}\n=== Даты ===\nТекст.')
    aliases = wiki.template_aliases
    wiki.template_aliases = lambda name: dict(name=name, aliases=[name, 'Empty section']) if name == 'Пустой раздел' else aliases(name)
    SectionWorker(settings, store, FORWARD, wiki).execute(queue(store, FORWARD))
    comment = wiki.edits[0][2]
    assert '[[ш:Empty section]] на [[ш:Дополнить раздел]]' in comment
    assert 'в разделе «История» (есть непустые подразделы)' in comment


def test_unicode_comment_above_490_bytes_is_preserved_in_api_request(settings):
    comment = dates_comment([dict(template='Проверить факты', previous='Факты', section_scoped=True,
                                 section='Проверка содержания и источников информации ' * 4, date='2020-01-02', revision=123)])
    assert len(comment.encode('utf-8')) > 490 and len(comment) < 500
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock(side_effect=[{'query': {'tokens': {'csrftoken': 'TOKEN'}}},
                                      {'edit': {'result': 'Success', 'newrevid': 2}}])
    client.edit_article(Revision('Пример', 1, 1, 'old'), 'new', comment, {'Пример'})
    assert client.request.call_args.args[0]['summary'] == comment


def test_long_comments_preserve_unicode_links_and_final_source():
    full = translation_comment([dict(template='Грубый перевод', language='en', original='Длинное название ' * 40)],
                               source_revision=123)
    fitted = fit_comment(full)
    assert len(fitted) <= 500 and '…' in fitted
    assert '[[Special:Diff/123|первой правки]]' in fitted
    assert fitted.count('[[') == fitted.count(']]')
    link = '[[ш:' + 'А' * 240 + ']]'
    fitted = fit_comment('Слово ' * 70 + link + ' продолжение')
    assert len(fitted) <= 500 and fitted.count('[[') == fitted.count(']]')


def test_dry_run_comments_are_full_for_admin_and_absent_from_public_logs(settings, store):
    configure(store, DATES)
    MaintenanceWorker(settings, store, DATES, MaintenanceWiki()).execute(queued(store, DATES))
    run = store.list_runs(processor=DATES)[0]
    client = create_app(settings, store).test_client()
    public = client.get('/runs/' + run['id'] + '/log.json').get_json()
    assert len(public['events']) == 1 and all(e['code'] != 'edit_summary' for e in public['events'])
    set_session(client, 'admin')
    assert 'Описание правки: В [[ш:Проверить факты]]' in client.get('/runs/' + run['id']).get_data(as_text=True)
