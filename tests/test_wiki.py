from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from toolforge_app.processors.obkat.report import TABLE_TITLE, page_title
from toolforge_app.wiki import Revision, WikiClient, WikiError, bot_may_edit


@pytest.mark.parametrize("text,allowed", [
    ("Discussion", True), ("{{nobots}}", False),
    ("{{bots|allow=ExampleBot}}", True), ("{{bots|allow=OtherBot}}", False),
    ("{{bots|deny=ExampleBot}}", False), ("{{bots|deny=all}}", False),
    ("{{bots|deny=none}}", True), ("<!-- {{nobots}} -->Discussion", True),
    ("<nowiki>{{nobots}}</nowiki>Discussion", True),
    ("{{Template:Nobots}}", False), ("{{Шаблон: Bots|deny=ExampleBot}}", False),
    ("{{Редактирую}}", False), ("{{Wip}}", False), ("{{In_use}}", False),
    ("<!-- {{Редактирую}} -->", True)])
def test_bot_exclusion_is_preserved(text, allowed):
    assert bot_may_edit(text, "ExampleBot") is allowed


def test_credentials_are_never_used_when_writes_disabled(settings):
    client = WikiClient(settings)
    with patch.object(client, "request") as request:
        with pytest.raises(WikiError, match="writes-disabled"):
            client.edit(Revision(page_title(settings.start_month), 1, 1, "text"), "updated", "summary")
        request.assert_not_called()


def test_api_edit_uses_fetched_revision_and_never_creates_pages(settings):
    settings.wiki_write = True
    settings.bot_username = "ExampleBot"
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock(side_effect=[{"query": {"tokens": {"csrftoken": "TOKEN"}}},
        {"edit": {"result": "Success", "newrevid": 123}}])
    base = Revision(page_title(settings.start_month), 122, 1791198000, "text")
    assert client.edit(base, "updated", "summary") == 123
    params = client.request.call_args.args[0]
    assert params["baserevid"] == 122 and params["nocreate"] == 1
    assert params["assertuser"] == "ExampleBot" and params["basetimestamp"]
    assert params['bot'] == 1 and params['notminor'] == 1 and 'minor' not in params
    assert params['watchlist'] == 'nochange' and params['summary'] == 'summary'


def test_article_edits_preserve_minor_flag(settings):
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock(side_effect=[{'query': {'tokens': {'csrftoken': 'TOKEN'}}},
                                     {'edit': {'result': 'Success', 'newrevid': 2}}])
    base = Revision('Пример', 1, 1, 'old')
    client.edit_article(base, 'new', 'summary', {'Пример'})
    params = client.request.call_args.args[0]
    assert params['bot'] == 1 and params['minor'] == 1 and 'notminor' not in params


def test_in_use_page_is_rejected_before_login_or_api_requests(settings):
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock()
    with pytest.raises(WikiError, match='page-in-use'):
        client.edit_article(Revision('Пример', 1, 1, '{{Пишу}} text'), 'new', 'summary', {'Пример'})
    client.login.assert_not_called()
    client.request.assert_not_called()


def test_writer_rejects_out_of_scope_page_before_logging_in(settings):
    settings.wiki_write = True
    client = WikiClient(settings)
    client.login = Mock()
    with pytest.raises(WikiError, match="outside-scope"):
        client.edit(Revision("Участник:admin", 1, 1, "text"), "updated", "summary")
    client.login.assert_not_called()


def test_category_members_pagination_preserves_namespace_and_sort_prefix(settings):
    client = WikiClient(settings)
    client.request = Mock(side_effect=[{'query': {'categorymembers': [{'title': 'А'}]},
                                      'continue': {'cmcontinue': 'NEXT', 'continue': '-||'}},
                                     {'query': {'categorymembers': [{'title': 'Б'}]}}])
    rows = list(client.category_members('Категория:Пример', start_prefix='А'))
    assert [row['title'] for row in rows] == ['А', 'Б']
    second = client.request.call_args.args[0]
    assert second['cmcontinue'] == 'NEXT' and second['cmnamespace'] == 0
    assert second['cmstartsortkeyprefix'] == 'А' and second['cmtype'] == 'page'


def test_template_redirects_resolve_to_canonical_with_all_alias_pages(settings):
    client = WikiClient(settings)
    client.request = Mock(side_effect=[{'query': {'pages': [{'title': 'Шаблон:Проверить факты',
                                                          'redirects': [{'title': 'Шаблон:Факты'}]}]},
                                      'continue': {'rdcontinue': 'NEXT'}},
                                     {'query': {'pages': [{'title': 'Шаблон:Проверить факты',
                                                          'redirects': [{'title': 'Шаблон:Проверка фактов'}]}]}}])
    definition = client.template_aliases('Факты')
    assert definition['name'] == 'Проверить факты'
    assert set(definition['aliases']) == {'Проверить факты', 'Факты', 'Проверка фактов'}


def test_revision_index_freezes_at_base_revision_and_continues(settings):
    client = WikiClient(settings)
    client.request = Mock(side_effect=[{'query': {'pages': [{'revisions': [{'revid': 1, 'timestamp': '2020-01-01T00:00:00Z'}]}]},
                                      'continue': {'rvcontinue': 'NEXT'}},
                                     {'query': {'pages': [{'revisions': [{'revid': 5, 'timestamp': '2020-01-02T00:00:00Z'}]}]}}])
    records = client.history_index('Пример', 5)
    assert [record['id'] for record in records] == [1, 5]
    assert client.request.call_args.args[0]['rvendid'] == 5
    assert client.request.call_args.args[0]['rvdir'] == 'newer'


@pytest.mark.parametrize('revisions,limit,code', [
    ([{'revid': 1}], 0, 'hidden-history'),
    ([{'revid': 1, 'timestamp': '2020-01-01T00:00:00Z'}], 0, 'incomplete-history'),
    ([{'revid': 1, 'timestamp': '2020-01-01T00:00:00Z'},
      {'revid': 5, 'timestamp': '2020-01-02T00:00:00Z'}], 1, 'revision-limit'),
])
def test_revision_index_rejects_missing_or_limited_history(settings, revisions, limit, code):
    client = WikiClient(settings)
    client.request = Mock(return_value={'query': {'pages': [{'revisions': revisions}]}})
    with pytest.raises(WikiError, match=code):
        client.history_index('Пример', 5, limit)


def test_historical_revision_content_cannot_be_taken_from_another_article(settings):
    client = WikiClient(settings)
    client.request = Mock(return_value={'query': {'pages': [{'title': 'Другая',
        'revisions': [{'revid': 1, 'slots': {'main': {'content': 'wrong text'}}}]}]}})
    with pytest.raises(WikiError, match='history-title-mismatch'):
        client.historical_text('Пример', 1)
