import io
import json
from unittest.mock import patch

import pytest

from toolforge_app import recovery
from toolforge_app.config import Settings
from toolforge_app.web import create_app


def test_backup_restores_login_and_database_but_environment_wins(monkeypatch):
    values = {'TOOLFORGE_DATABASE_URL': 'mysql+pymysql://localhost/database',
              'TOOLFORGE_PUBLIC_URL': 'https://example.toolforge.org',
              'TOOLFORGE_SECRET_KEY': 'stable-key', 'TOOLFORGE_ADMIN_USERNAME': 'admin',
              'TOOLFORGE_OAUTH_KEY': 'consumer', 'TOOLFORGE_OAUTH_SECRET': 'secret'}
    with patch.dict('os.environ', {}, clear=True), \
            patch('toolforge_app.config.load_dotenv'), \
            patch('toolforge_app.config.tool_directory', return_value='/data/project/example'), \
            patch('toolforge_app.config.private_configuration', return_value={'environment': values}):
        result = Settings.from_env()
        assert result.secret_key == 'stable-key' and result.admin_username == 'admin'
        assert result.oauth_key == 'consumer' and result.database_url.startswith('mysql')
        assert result.configuration_source == 'recovery'
        monkeypatch.setenv('TOOLFORGE_OAUTH_KEY', '')
        assert Settings.from_env().oauth_key == ''  # deliberate removals remain authoritative


def test_missing_production_configuration_never_creates_an_empty_database():
    with patch.dict('os.environ', {}, clear=True), \
            patch('toolforge_app.config.load_dotenv'), \
            patch('toolforge_app.config.tool_directory', return_value='/data/project/example'), \
            patch('toolforge_app.config.private_configuration', return_value=None), \
            patch('toolforge_app.web.Store') as database:
        client = create_app().test_client()
        for path in ('/', '/login', '/admin', '/healthz'):
            response = client.get(path)
            assert response.status_code == 503
            assert 'не нужно создавать заново' in response.get_data(as_text=True)
        database.assert_not_called()


def test_private_backup_rejects_public_permissions_and_symlinks(tmp_path, monkeypatch):
    folder = tmp_path / '.config/ruwiki-works'
    folder.mkdir(parents=True)
    file = folder / 'recovery.json'
    file.write_text(json.dumps({'version': 1, 'environment': {}}))
    monkeypatch.setattr(recovery, 'tool_directory', lambda: tmp_path)
    # Use explicit POSIX stat metadata, also on Windows test hosts.
    from types import SimpleNamespace
    monkeypatch.setattr(recovery.os, 'getuid', lambda: 100, raising=False)
    with patch.object(type(file), 'lstat', return_value=SimpleNamespace(st_mode=0o100644, st_uid=100)):
        with pytest.raises(recovery.RecoveryError, match='permissions'):
            recovery.private_configuration()
    with patch.object(type(file), 'lstat', return_value=SimpleNamespace(st_mode=0o120600, st_uid=100)):
        with pytest.raises(recovery.RecoveryError, match='permissions'):
            recovery.private_configuration()


def admin_client(settings, store):
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session.update(username='admin', csrf='csrf')
    return client


def test_process_restart_requires_existing_admin_and_csrf(settings, store):
    client = create_app(settings, store).test_client()
    with patch('toolforge_app.web.restart_process') as restart:
        assert client.get('/admin/processes/executor/restart').status_code == 405
        assert client.post('/admin/processes/executor/restart').status_code == 400
        with client.session_transaction() as session:
            session.update(username='other', csrf='csrf')
        assert client.post('/admin/processes/executor/restart', data={'csrf': 'csrf'}).status_code == 403
        restart.assert_not_called()


def test_restart_preserves_runtime_and_waits_for_execution_slot(settings, store):
    client = admin_client(settings, store)
    store.set_state('service:executor', {'enabled': False})
    with patch('toolforge_app.web.restart_available', return_value=True), \
            patch('toolforge_app.web.restart_process', return_value='receipt') as restart:
        with store.worker_lease(processor='execution'):
            assert client.post('/admin/processes/executor/restart', data={'csrf': 'csrf'}).status_code == 409
        restart.assert_not_called()
        assert client.post('/admin/processes/executor/restart', data={'csrf': 'csrf'}).status_code == 302
        assert store.get_state('service:executor') == {'enabled': False}
        assert store.get_state('process:restart')['status'] == 'accepted'
        assert client.post('/admin/processes/executor/restart', data={'csrf': 'csrf'}).status_code == 409
        assert restart.call_count == 1


def test_uncertain_restart_is_not_retried_and_reserves_short_safety_window(settings, store):
    client = admin_client(settings, store)
    with patch('toolforge_app.web.restart_available', return_value=True), \
            patch('toolforge_app.web.restart_process', side_effect=recovery.RecoveryError('Не подтверждено')) as restart:
        assert client.post('/admin/processes/executor/restart', data={'csrf': 'csrf'}).status_code == 302
        assert store.get_state('process:restart')['status'] == 'unconfirmed'
        assert store.lease_active('execution') and store.lease_active('dispatcher')
        assert restart.call_count == 1


def test_process_control_cannot_target_arbitrary_deployments():
    with pytest.raises(recovery.RecoveryError, match='Unknown'):
        recovery.restart_process('anything-else')


def test_restart_uses_verified_tls_fixed_target_and_waits_for_old_pods(tmp_path):
    root = tmp_path / 'example'
    data = {'control': {'server': 'https://k8s.tools.eqiad1.wikimedia.cloud:6443',
                       'certificate_authority': 'trusted-ca'}}
    calls = []
    pods = iter([{'items': [{'metadata': {'uid': 'old'}}]},
                 {'items': [{'metadata': {'uid': 'old'}}]}, {'items': []}])
    class Response(io.BytesIO):
        status = 200
    def open_request(request, **kwargs):
        calls.append(request)
        if isinstance(request, str):
            payload = (next(pods) if '/pods?' in request else
                       {'spec': {'selector': {'matchLabels': {'app': 'executor'}}}})
        else:
            assert request.method == 'PATCH'
            assert request.full_url.endswith('/namespaces/tool-example/deployments/ruwiki-executor')
            payload = {}
        return Response(json.dumps(payload).encode())
    with patch.object(recovery, 'tool_directory', return_value=root), \
            patch.object(recovery, 'private_configuration', return_value=data), \
            patch.object(recovery.ssl, 'SSLContext') as tls, \
            patch.object(recovery, 'urlopen', side_effect=open_request), patch.object(recovery.time, 'sleep'):
        assert len(recovery.restart_process('executor')) == 32
        tls.assert_called_once_with(recovery.ssl.PROTOCOL_TLS_CLIENT)
        tls.return_value.load_verify_locations.assert_called_once_with(cadata='trusted-ca')
    assert len([c for c in calls if not isinstance(c, str)]) == 1


def test_restart_buttons_are_admin_only(settings, store):
    with patch('toolforge_app.web.restart_available', return_value=True):
        assert '/admin/processes/' not in create_app(settings, store).test_client().get('/').get_data(as_text=True)
        html = admin_client(settings, store).get('/admin').get_data(as_text=True)
        assert '/admin/processes/web/restart' in html
        assert '/admin/processes/executor/restart' in html
        assert '/admin/processes/monitor/restart' in html
