import json
from dataclasses import replace
from unittest.mock import patch

import pytest
import requests

from toolforge_app.connections import (AuthOnlyClient, effective_settings, fingerprint, probe_bot,
                                      probe_oauth, save_credentials)
from toolforge_app.web import create_app
from toolforge_app.wiki import WikiClient, WikiError


def admin_client(settings, store):
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session.update(username='admin', csrf='test-csrf')
    return client


def test_credentials_are_encrypted_and_runtime_gate_is_preserved(settings, store):
    values = {'bot_login': 'Bot@tool', 'bot_password': 'private-app-password'}
    save_credentials(settings, store, 'bot', values)
    encrypted = store.connection_secret('bot')
    assert 'private-app-password' not in encrypted and 'Bot@tool' not in encrypted
    current = effective_settings(settings, store)
    assert current.bot_login == values['bot_login'] and current.bot_password == values['bot_password']
    assert current.wiki_write is False
    assert settings.bot_password == ''
    # Wrong signing keys must not silently resurrect obsolete environment secrets.
    with pytest.raises(WikiError, match='credentials-unreadable'):
        effective_settings(replace(settings, secret_key='other-key', bot_password='old-password'), store)


def test_corrupt_override_fails_closed(settings, store):
    store.save_connection_secret('bot', 'invalid-ciphertext')
    with pytest.raises(WikiError, match='credentials-unreadable'):
        effective_settings(settings, store)
    assert effective_settings(settings, store, names=('oauth',)).oauth_key == settings.oauth_key


def test_recovery_removes_only_selected_override(settings, store):
    save_credentials(settings, store, 'bot', {'bot_login': 'Bot@tool', 'bot_password': 'password'})
    save_credentials(settings, store, 'oauth', {'oauth_key': 'new-token', 'oauth_secret': 'new-secret'})
    store.clear_connection_secret('oauth')
    current = effective_settings(settings, store)
    assert current.oauth_secret == settings.oauth_secret
    assert current.bot_password == 'password' and not current.wiki_write


def test_worker_refreshes_credentials_and_drops_previous_session(settings, store):
    settings.wiki_write = True
    settings.bot_username = 'Bot'
    settings.bot_login, settings.bot_password = 'Bot@old', 'old-password'
    wiki = WikiClient(settings, store)
    wiki.logged_in = True
    wiki.session.cookies.set('auth', 'old-cookie')
    save_credentials(settings, store, 'bot', {'bot_login': 'Bot@new', 'bot_password': 'new-password'})
    with patch.object(wiki, 'request', side_effect=[{'query': {'tokens': {'logintoken': 'token'}}},
            {'login': {'result': 'Success'}}, {'query': {'userinfo': {'name': 'Bot'}}}]) as call:
        wiki.login()
    assert call.call_args_list[1].args[0]['lgname'] == 'Bot@new'
    assert wiki.logged_in and not wiki.session.cookies.get('auth')
    assert settings.bot_password == 'old-password'


@pytest.mark.parametrize('path', ['/admin', '/admin/connections'])
def test_connections_require_admin_and_return_after_login(settings, store, path):
    client = create_app(settings, store).test_client()
    assert client.get(path).status_code == 302
    assert client.get(path).location == '/login'
    assert 'Подключения' not in client.get('/').get_data(as_text=True)
    with client.session_transaction() as session:
        assert session['login_next'] == '/admin'
        session['username'] = 'AnotherUser'
    assert client.get(path).status_code == 403


def test_admin_status_has_all_components_without_secret_values(settings, store):
    settings.bot_username, settings.bot_login, settings.bot_password = 'Bot', 'Bot@tool', 'secret-password'
    client = admin_client(settings, store)
    html = client.get('/admin').get_data(as_text=True)
    for phrase in ('База данных', 'Монитор', 'Исполнитель', 'Запись в Википедию',
                   'Учётная запись бота', 'Wikimedia OAuth', 'Последняя проверка'):
        assert phrase in html
    assert settings.bot_password not in html and settings.oauth_secret not in html and settings.oauth_key not in html
    assert 'href="/admin/connections"' in client.get('/').get_data(as_text=True)
    assert client.get('/admin').headers['Cache-Control'] == 'no-store'


def test_check_is_csrf_protected_read_only_and_rate_limited(settings, store):
    client = admin_client(settings, store)
    assert client.get('/admin/connections/bot/check').status_code == 405
    assert client.post('/admin/connections/bot/check').status_code == 400
    with patch('toolforge_app.web.probe_bot', return_value={'verified': True, 'code': ''}) as probe:
        assert client.post('/admin/connections/bot/check', data={'csrf': 'test-csrf'}).status_code == 302
        assert client.post('/admin/connections/bot/check', data={'csrf': 'test-csrf'}).status_code == 409
        assert probe.call_count == 1
    assert not store.queue() and settings.wiki_write is False
    assert store.get_state('connection:bot')['verified']


@pytest.mark.parametrize('username', [None, 'AnotherUser', 'Admin'])
def test_connection_changes_reject_non_admin(settings, store, username):
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session['csrf'] = 'test-csrf'
        if username:
            session['username'] = username
    for operation in ('save', 'check'):
        assert client.post('/admin/connections/bot/' + operation, data={
            'csrf': 'test-csrf', 'bot_login': 'Bot@tool', 'bot_password': 'private-password'}).status_code == 403
    assert not store.connection_secret('bot')


def test_failed_replacement_keeps_working_credentials_and_does_not_echo_password(settings, store):
    save_credentials(settings, store, 'bot', {'bot_login': 'Bot@working', 'bot_password': 'working-password'})
    client = admin_client(settings, store)
    with patch('toolforge_app.web.probe_bot', return_value={'verified': False, 'code': 'login-failed'}):
        result = client.post('/admin/connections/bot/save', data={
            'csrf': 'test-csrf', 'bot_login': 'Bot@bad', 'bot_password': 'submitted-password'}, follow_redirects=True)
    assert 'submitted-password' not in result.get_data(as_text=True)
    assert effective_settings(settings, store).bot_password == 'working-password'
    assert settings.wiki_write is False


def test_verified_replacement_is_shared_and_receipt_is_invalidation_scoped(settings, store):
    client = admin_client(settings, store)
    with patch('toolforge_app.web.probe_bot', return_value={'verified': True, 'code': '', 'checks': {'bot': True}}):
        result = client.post('/admin/connections/bot/save', data={'csrf': 'test-csrf',
            'bot_login': 'Bot@tool', 'bot_password': 'private-password'}, follow_redirects=True)
    assert 'Подключён' in result.get_data(as_text=True)
    assert 'private-password' not in json.dumps(store.get_state('connection:bot'))
    assert store.get_state('connection:bot')['fingerprint'] == fingerprint(effective_settings(settings, store), 'bot')
    assert effective_settings(settings, store).bot_password == 'private-password'
    assert settings.wiki_write is False


def test_auth_checker_rejects_all_writes_before_request(settings):
    client = AuthOnlyClient(replace(settings, wiki_write=True))
    with patch.object(client.session, 'post') as post:
        with pytest.raises(WikiError, match='api'):
            client.request({'action': 'edit', 'text': 'anything'}, post=True)
        post.assert_not_called()


@pytest.mark.parametrize('identity,code', [
    ({'name': 'Bot', 'rights': ['edit', 'bot', 'minoredit']}, ''),
    ({'name': 'Bot', 'rights': ['edit', 'minoredit']}, 'insufficient-bot-grants'),
    ({'name': 'Bot', 'rights': ['edit', 'bot', 'minoredit'], 'blockid': 123}, 'blocked'),
    ({'name': 'Other', 'rights': ['edit', 'bot', 'minoredit']}, 'unexpected-bot-identity'),
])
def test_bot_verification_checks_current_rights_without_obsolete_writeapi(settings, identity, code):
    settings.bot_username, settings.bot_login, settings.bot_password = 'Bot', 'Bot@tool', 'password'
    with patch.object(AuthOnlyClient, 'request', side_effect=[{'query': {'tokens': {'logintoken': 'token'}}},
            {'login': {'result': 'Success'}}, {'query': {'userinfo': identity}}, {'query': {'userinfo': identity}}]) as call:
        result = probe_bot(settings)
    assert result['code'] == code and result['verified'] is (code == '')
    assert all(item.args[0]['action'] in {'query', 'login'} for item in call.call_args_list)
    assert settings.wiki_write is False


def test_bot_network_failure_is_safe_and_bounded(settings):
    settings.bot_username, settings.bot_login, settings.bot_password = 'Bot', 'Bot@tool', 'password'
    with patch('toolforge_app.connections.requests.Session.get', side_effect=requests.Timeout('secret from backend')) as get:
        assert probe_bot(settings) == {'verified': False, 'code': 'network'}
    assert get.call_count == 1 and get.call_args.kwargs['timeout'] == (3, 7)


def test_oauth_probe_has_timeout_and_discarded_request_tokens(settings):
    with patch('toolforge_app.connections.requests.post') as post:
        post.return_value.text = 'oauth_token=request-key&oauth_token_secret=request-secret'
        result = probe_oauth(settings)
    assert result == {'verified': True, 'code': '', 'login_verified': False}
    assert post.call_args.kwargs['timeout'] == (5, 10)
    assert 'request-secret' not in json.dumps(result)


def test_admin_preview_cannot_store_credentials(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    with client.session_transaction() as session:
        session.update(admin_preview=True, csrf='test-csrf')
    assert client.get('/admin').status_code == 200
    with patch('toolforge_app.web.probe_bot') as probe:
        assert client.post('/admin/connections/bot/save', data={'csrf': 'test-csrf',
            'bot_login': 'Bot@tool', 'bot_password': 'password'}).status_code == 302
        probe.assert_not_called()
    assert not store.connection_secret('bot')
