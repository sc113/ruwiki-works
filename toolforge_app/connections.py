"""Admin-only connection settings and bounded, read-only authentication checks."""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import replace

import mwoauth
import requests
from cryptography.fernet import Fernet, InvalidToken
from requests_oauthlib import OAuth1

from .wiki import WikiClient, WikiError

FIELDS = {'bot': ('bot_login', 'bot_password'), 'oauth': ('oauth_key', 'oauth_secret')}
RIGHTS = {'edit': 'Редактирование страниц', 'bot': 'Флаг бота', 'minoredit': 'Малые правки'}
ERRORS = {
    'credentials-missing': 'Учётные данные ещё не заданы.',
    'credentials-unreadable': 'Не удалось расшифровать настройки. Восстановите ключ сервера или введите данные заново.',
    'login-failed': 'Википедия отклонила логин или BotPassword. Проверьте пароль приложения.',
    'unexpected-bot-identity': 'Вход выполнен под другой учётной записью. Нужен BotPassword настроенной учётки бота.',
    'insufficient-bot-grants': 'Вход выполнен, но не хватает разрешений BotPassword.',
    'blocked': 'Учётная запись заблокирована в Википедии.',
    'network': 'Википедия не ответила. Повторите проверку позднее.',
    'maxlag': 'Википедия временно просит отложить запросы.',
    'ratelimited': 'Википедия ограничила частоту запросов. Повторите проверку позднее.',
    'oauth-failed': 'Не удалось начать OAuth-вход. Проверьте consumer token, secret token и callback.',
    'api': 'Не удалось проверить подключение через API Википедии.',
}


def cipher(settings):
    key = hashlib.sha256(('ruwiki-works:connections:v1:' + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def effective_settings(settings, store, names=None):
    """Shared DB overrides take precedence; never change the wiki write gate."""
    values = {}
    for name, fields in FIELDS.items():
        if names is not None and name not in names:
            continue
        encrypted = store.connection_secret(name)
        if encrypted:
            try:
                decoded = json.loads(cipher(settings).decrypt(encrypted.encode()))
                if not isinstance(decoded, dict) or set(decoded) != set(fields) or not all(
                        isinstance(decoded[field], str) for field in fields):
                    raise ValueError()
                values.update(decoded)
            except (InvalidToken, ValueError, TypeError, UnicodeError, KeyError):
                # Never fall back to obsolete environment credentials after key loss.
                raise WikiError('credentials-unreadable') from None
    return replace(settings, **values)


def save_credentials(settings, store, name, values):
    fields = FIELDS[name]
    data = {key: values[key] for key in fields}
    encrypted = cipher(settings).encrypt(json.dumps(data).encode()).decode()
    store.save_connection_secret(name, encrypted)


def fingerprint(settings, name):
    data = json.dumps([getattr(settings, field) for field in FIELDS[name]])
    if name == 'bot':
        data += settings.bot_username + settings.api_url
    else:
        data += settings.oauth_url + settings.public_url
    return hmac.new(settings.secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()


def record_check(settings, store, name, result):
    # Receipts contain a fixed schema, never API responses, tokens or passwords.
    safe = {key: result[key] for key in ('verified', 'code', 'checks', 'missing', 'login_verified') if key in result}
    safe.update(at=time.time(), fingerprint=fingerprint(settings, name))
    store.set_state('connection:' + name, safe)
    return safe


def last_check(settings, store, name):
    result = store.get_state('connection:' + name, {})
    return result if result.get('fingerprint') == fingerprint(settings, name) else {}


class AuthOnlyClient(WikiClient):
    def request(self, params, post=False):
        if params.get('action') not in {'query', 'login'}:
            raise WikiError('api')
        params = {'format': 'json', 'formatversion': 2, 'maxlag': 5, **params}
        try:
            response = (self.session.post(self.settings.api_url, data=params, timeout=(3, 7)) if post else
                        self.session.get(self.settings.api_url, params=params, timeout=(3, 7)))
            if response.status_code == 429:
                raise WikiError('ratelimited')
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            raise WikiError('network') from None
        if 'error' in data:
            code = data['error'].get('code')
            raise WikiError(code if code in ERRORS else 'api')
        return data


def probe_bot(settings):
    if not all((settings.bot_username, settings.bot_login, settings.bot_password)):
        return dict(verified=False, code='credentials-missing')
    # login()'s gate is relaxed only in this isolated client. Its request allowlist
    # rejects every write action, even if a caller accidentally invokes edit().
    client = AuthOnlyClient(replace(settings, wiki_write=True))
    try:
        client.login()
        identity = client.request({'action': 'query', 'meta': 'userinfo', 'uiprop': 'rights|blockinfo'})['query']['userinfo']
        checks = {key: key in identity.get('rights', []) for key in RIGHTS}
        missing = [key for key in RIGHTS if not checks[key]]
        if identity.get('blockid') or identity.get('blockedby'):
            return dict(verified=False, code='blocked', checks=checks)
        # writeapi was removed from MediaWiki; edit, bot and minoredit are the
        # current permissions needed for this application's existing-page edits.
        return dict(verified=not missing, code='insufficient-bot-grants' if missing else '',
                    checks=checks, missing=missing)
    except (WikiError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, WikiError) and error.code in ERRORS else 'api'
        return dict(verified=False, code=code)
    finally:
        client.session.close()


def probe_oauth(settings):
    if not settings.oauth_key or not settings.oauth_secret:
        return dict(verified=False, code='credentials-missing')
    try:
        response = requests.post(settings.oauth_url, params={'title': 'Special:OAuth/initiate'},
            auth=OAuth1(settings.oauth_key, client_secret=settings.oauth_secret,
                        callback_uri=settings.public_url.rstrip('/') + '/oauth/callback'),
            headers={'User-Agent': settings.user_agent}, timeout=(5, 10))
        response.raise_for_status()
        mwoauth.functions.process_request_token(response.text)
        return dict(verified=True, code='', login_verified=False)
    except requests.RequestException:
        return dict(verified=False, code='network')
    except Exception:
        return dict(verified=False, code='oauth-failed')


def connection_summary(settings, store):
    result = {}
    for name, fields in FIELDS.items():
        check = last_check(settings, store, name)
        configured = all(getattr(settings, field) for field in fields)
        status = 'success' if check.get('verified') else 'error' if check else 'pending'
        if not configured:
            status = 'pending'
        result[name] = dict(configured=configured, status=status, check=check,
                            message=ERRORS.get(check.get('code'), ''))
    return result
