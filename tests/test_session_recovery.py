"""Bounded recovery of explicitly rejected edits, without losing write guards."""
import json
from unittest.mock import Mock

import pytest

from toolforge_app.wiki import Revision, WikiClient, WikiError
from toolforge_app.web import create_app
from toolforge_app.processors.logs import public_run
from toolforge_app.processors.daily_worker import DailyWorker
from toolforge_app.worker import Worker
from toolforge_app.console import decode_run
from test_web import set_session


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


def client_with_session(settings, first='assertuserfailed', second=None, *, identity='ExampleBot'):
    settings.wiki_write = True
    settings.edit_interval = 0
    settings.bot_username = 'ExampleBot'
    settings.bot_login, settings.bot_password = 'ExampleBot@app', 'PRIVATE-PASSWORD'
    client = WikiClient(settings)
    client.logged_in = True
    client.session.cookies.set('session', 'old')
    calls = []
    edits = []
    def send(url, **kwargs):
        params = kwargs.get('params', kwargs.get('data'))
        calls.append(dict(params))
        if params['action'] == 'login':
            assert not client.session.cookies.get('session')
            return response({'login': {'result': 'Success'}})
        if params.get('meta') == 'userinfo':
            return response({'query': {'userinfo': {'name': identity}}})
        if params.get('meta') == 'tokens':
            return response({'query': {'tokens': {'logintoken': 'LOGIN'} if params.get('type') == 'login'
                                       else {'csrftoken': 'FRESH' if edits else 'OLD'}}})
        assert params['action'] == 'edit'
        edits.append(dict(params))
        error = first if len(edits) == 1 else second
        return response({'error': {'code': error}} if error else {'edit': {'result': 'Success', 'newrevid': 11}})
    client.session.get = Mock(side_effect=send)
    client.session.post = Mock(side_effect=send)
    return client, calls, edits


@pytest.mark.parametrize('code', ['assertuserfailed', 'assertnameduserfailed', 'notloggedin', 'badtoken'])
@pytest.mark.parametrize('create', [False, True])
def test_session_recovery_refreshes_identity_and_token_preserving_guards(settings, code, create):
    client, calls, edits = client_with_session(settings, code)
    client.event = Mock()
    base = Revision('Категория:Пример', 10, 1792195200, 'old', missing=create)
    assert client.edit_category(base, 'new', 'summary', {base.title}, create=create) == 11
    assert len(edits) == 2
    assert sum(p['action'] == 'login' for p in calls) == 1
    assert sum(p.get('meta') == 'userinfo' for p in calls) == 1
    assert edits[0]['token'] == 'OLD' and edits[1]['token'] == 'FRESH'
    assert {k:v for k,v in edits[0].items() if k != 'token'} == {k:v for k,v in edits[1].items() if k != 'token'}
    assert edits[1]['bot'] == edits[1]['notminor'] == 1 and edits[1]['assertuser'] == 'ExampleBot'
    if create:
        assert edits[1]['createonly'] == 1 and 'nocreate' not in edits[1]
    else:
        assert edits[1]['baserevid'] == 10 and edits[1]['basetimestamp'] and edits[1]['nocreate'] == 1
    assert [call.args[0] for call in client.event.call_args_list] == ['auth_retry', 'auth_recovered']
    assert 'PRIVATE-PASSWORD' not in repr(client.event.call_args_list)


@pytest.mark.parametrize('second', ['assertuserfailed', 'badtoken', 'editconflict'])
def test_retry_stops_after_second_rejection_or_intervening_edit(settings, second):
    client, calls, edits = client_with_session(settings, second=second)
    client.event = Mock()
    with pytest.raises(WikiError, match=second):
        client.edit_article(Revision('Пример', 10, 1792195200, 'old'), 'new', 'summary', {'Пример'})
    assert len(edits) == 2 and sum(p['action'] == 'login' for p in calls) == 1
    assert edits[1]['minor'] == 1 and edits[1]['baserevid'] == 10
    assert [call.args[0] for call in client.event.call_args_list] == ['auth_retry']


@pytest.mark.parametrize('code', ['assertbotfailed', 'blocked', 'permissiondenied', 'editconflict', 'spamblacklist'])
def test_rights_and_page_errors_do_not_trigger_relogin(settings, code):
    client, calls, edits = client_with_session(settings, first=code)
    with pytest.raises(WikiError, match=code):
        client.edit_article(Revision('Пример', 10, 1, 'old'), 'new', 'summary', {'Пример'})
    assert len(edits) == 1 and not any(p['action'] == 'login' for p in calls)


def test_recovery_rejects_wrong_account_before_second_save(settings):
    client, calls, edits = client_with_session(settings, identity='OtherBot')
    with pytest.raises(WikiError, match='unexpected-bot-identity'):
        client.edit_article(Revision('Пример', 10, 1, 'old'), 'new', 'summary', {'Пример'})
    assert len(edits) == 1


def test_ambiguous_save_is_never_retried_by_session_recovery(settings):
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock(side_effect=[{'query': {'tokens': {'csrftoken': 'OLD'}}}, WikiError('edit-outcome-unknown')])
    with pytest.raises(WikiError, match='edit-outcome-unknown'):
        client.edit_article(Revision('Пример', 10, 1, 'old'), 'new', 'summary', {'Пример'})
    assert client.login.call_count == 1 and client.request.call_count == 2


def test_old_auth_failure_has_clear_public_and_admin_explanation(settings, store):
    run_id = store.start_run('daily', processor='maintenance-rq')
    events = [dict(at=1, code='article', title='Пример', message='Начало'),
              dict(at=2, code='error', title='Пример', message='Не удалось обработать страницу',
                   error='assertuserfailed', private='PRIVATE-PASSWORD')]
    store.finish_run(run_id, events, {'errors': 1}, {}, status='failed')
    projected = public_run(decode_run(store.run(run_id)))
    assert len(projected['events']) == 1
    assert projected['events'][0]['message'] == 'Сбой авторизации бота; правка не сохранена'
    assert 'PRIVATE-PASSWORD' not in json.dumps(projected) and 'assertuserfailed' not in json.dumps(projected)
    client = create_app(settings, store).test_client()
    public = client.get('/runs/' + run_id).get_data(as_text=True)
    assert 'Сбой авторизации бота; правка не сохранена' in public
    set_session(client, settings.admin_username)
    admin = client.get('/runs/' + run_id).get_data(as_text=True)
    assert 'Сбой авторизации бота; правка не сохранена' in admin and 'assertuserfailed' in admin


@pytest.mark.parametrize('slug', ['obkat', 'maintenance-rq', 'categories-create'])
def test_workers_attach_session_recovery_to_live_admin_events(settings, store, slug):
    from toolforge_app.processors.categories.service import CategoryWorker
    client = WikiClient(settings)
    worker = Worker(settings, store, client) if slug == 'obkat' else (
        CategoryWorker(settings, store, slug, client) if slug == 'categories-create' else DailyWorker(settings, store, slug, client))
    client.event('auth_retry', 'Повторный вход', title='Пример')
    assert worker.events[-1]['code'] == 'auth_retry'
    worker.event('error', 'Не обработано', title='Пример', error='assertuserfailed')
    assert worker.events[-1]['message'] == 'Сбой авторизации бота; правка не сохранена'


def test_recovery_respects_pause_before_second_edit(settings):
    from toolforge_app.processors.daily_worker import Controlled
    client, calls, edits = client_with_session(settings)
    stopped = False
    def event(code, *args, **kwargs):
        nonlocal stopped
        stopped = code == 'auth_retry'
    def guard():
        if stopped:
            raise Controlled('paused')
    client.event, client.request_guard = event, guard
    with pytest.raises(Controlled):
        client.edit_article(Revision('Пример', 10, 1, 'old'), 'new', 'summary', {'Пример'})
    assert len(edits) == 1


def test_recovery_keeps_shared_throttle_between_rejected_and_successful_edits(settings, store, monkeypatch):
    client, calls, edits = client_with_session(settings)
    client.store = store
    settings.edit_interval = 10
    clock = [1000.0]
    monkeypatch.setattr('toolforge_app.wiki.time.time', lambda: clock[0])
    monkeypatch.setattr('toolforge_app.wiki.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    assert client.edit_article(Revision('Пример', 10, 1, 'old'), 'new', 'summary', {'Пример'}) == 11
    assert len(edits) == 2 and clock[0] == 1010
    assert store.get_state('wiki:last_edit_attempt') == 1010
