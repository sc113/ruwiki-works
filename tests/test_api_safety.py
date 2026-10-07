"""Exercise retries and write limits without contacting Wikimedia."""
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

from toolforge_app.wiki import Revision, WikiClient, WikiError


class Clock:
    def __init__(self, monkeypatch):
        self.now, self.waits = 1791200000.0, []
        monkeypatch.setattr('toolforge_app.wiki.time.time', lambda: self.now)
        monkeypatch.setattr('toolforge_app.wiki.time.sleep', self.sleep)

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def response(data=None, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data or {'query': {}}
    return result


@pytest.mark.parametrize('header', ['12', 'date'])
def test_maxlag_honors_retry_after_in_seconds_or_http_date(settings, monkeypatch, header):
    clock = Clock(monkeypatch)
    if header == 'date':
        header = formatdate(clock.now + 12, usegmt=True)
    client = WikiClient(settings)
    called = []
    answers = iter([response({'error': {'code': 'maxlag'}}, headers={'Retry-After': header}), response()])
    def get(*args, **kwargs):
        called.append(clock.now)
        return next(answers)
    client.session.get = Mock(side_effect=get)
    client.request({'action': 'query'})
    assert called[1] - called[0] == 12
    assert max(clock.waits) <= 5
    assert client.session.get.call_args.kwargs['params']['maxlag'] == 5


def test_read_retries_are_bounded_and_preserve_api_error(settings, monkeypatch):
    clock = Clock(monkeypatch)
    client = WikiClient(settings)
    client.session.get = Mock(return_value=response({'error': {'code': 'maxlag'}}))
    with pytest.raises(WikiError, match='maxlag'):
        client.request({'action': 'query'})
    assert client.session.get.call_count == 3
    assert sum(clock.waits) == 15


def test_long_server_pause_is_shared_across_clients_and_restarts(settings, store, monkeypatch):
    clock = Clock(monkeypatch)
    first = WikiClient(settings, store)
    first.session.get = Mock(return_value=response(status=429, headers={'Retry-After': '300'}))
    with pytest.raises(WikiError, match='ratelimited'):
        first.request({'action': 'query'})
    second = WikiClient(settings, store)
    second.session.get = Mock(return_value=response())
    with pytest.raises(WikiError, match='ratelimited'):
        second.request({'action': 'query'})
    second.session.get.assert_not_called()
    assert not clock.waits
    clock.now += 300
    second.request({'action': 'query'})
    second.session.get.assert_called_once()


def test_concurrent_delays_cannot_shorten_server_requested_pause(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda delay: store.defer_api('ratelimited', delay), [300, 5, 20, 600, 10]))
    assert store.get_state('wiki:api_backoff')['until'] == 600


def test_write_limit_applies_between_actions_and_to_rejected_attempts(settings, store, monkeypatch):
    clock = Clock(monkeypatch)
    calls = []
    def save(*args, **kwargs):
        calls.append(clock.now)
        return response({'error': {'code': 'protectedpage'}}) if len(calls) == 1 else response({'edit': {'result': 'Success'}})
    first, second = WikiClient(settings, store), WikiClient(settings, store)
    first.session.post = second.session.post = Mock(side_effect=save)
    with pytest.raises(WikiError, match='protectedpage'):
        first.request({'action': 'edit'}, post=True)
    second.request({'action': 'edit'}, post=True)
    assert calls[1] - calls[0] == settings.edit_interval


@pytest.mark.parametrize('failure', ['timeout', 'gateway', 'invalid-json'])
def test_uncertain_edit_result_is_not_resubmitted(settings, monkeypatch, failure):
    clock = Clock(monkeypatch)
    client = WikiClient(settings)
    result = response(status=502 if failure == 'gateway' else 200)
    if failure == 'invalid-json':
        result.json.side_effect = ValueError('broken response')
    client.session.post = Mock(side_effect=requests.Timeout() if failure == 'timeout' else None,
                               return_value=result)
    with pytest.raises(WikiError, match='edit-outcome-unknown'):
        client.request({'action': 'edit'}, post=True)
    client.session.post.assert_called_once()
    assert not clock.waits


def test_pause_can_interrupt_a_retry_wait(settings, monkeypatch):
    clock = Clock(monkeypatch)
    client = WikiClient(settings)
    client.session.get = Mock(return_value=response({'error': {'code': 'maxlag'}}))
    def guard():
        if clock.now > 1791200000:
            raise RuntimeError('paused')
    client.request_guard = guard
    with pytest.raises(RuntimeError, match='paused'):
        client.request({'action': 'query'})
    client.session.get.assert_called_once()


def test_captcha_is_reported_without_retry_or_attempt_to_solve(settings):
    client = WikiClient(settings)
    client.login = Mock()
    client.request = Mock(side_effect=[{'query': {'tokens': {'csrftoken': 'TOKEN'}}},
                                     {'edit': {'result': 'Failure', 'captcha': {'id': '1'}}}])
    with pytest.raises(WikiError, match='captcha'):
        client.edit_article(Revision('Пример', 1, 1, 'text'), 'new', 'summary', {'Пример'})
    assert client.request.call_count == 2
