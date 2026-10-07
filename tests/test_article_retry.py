import hashlib
import json
import time

import pytest

from toolforge_app.processors.daily import report
from toolforge_app.processors.sections.inventory import refresh_inventory as section_inventory
from toolforge_app.processors.sections.service import SectionWorker
from toolforge_app.processors.translations.inventory import refresh_inventory as translation_inventory
from toolforge_app.processors.translations.service import TranslationWorker
from toolforge_app.schedules import next_daily_time
from toolforge_app.web import create_app
from toolforge_app.wiki import Revision
from test_sections import SectionWiki
from test_translations import TranslationWiki
from test_web import set_session

SLUGS = ('translations-categories', 'translations-talk', 'sections-empty-to-fill', 'sections-fill-to-empty')


class MultipleTranslations(TranslationWiki):
    def category_members(self, category, **kwargs):
        self.request_guard()
        return [{'title': title} for title, base in self.data.items()
                if not title.startswith('Обсуждение:') and '|язык=en|' not in base.text]


def prepare(store, slug):
    if slug.startswith('sections-'):
        text = '== A ==\n{{Пустой раздел}}\nТекст' if slug.endswith('empty-to-fill') else '== A ==\n{{Дополнить раздел}}'
        wiki = SectionWiki(text)
        worker, refresh = SectionWorker, section_inventory
        outcome = 'unchanged'
    else:
        wiki = MultipleTranslations()
        worker, refresh = TranslationWorker, translation_inventory
        outcome = 'comment-source-missing'
    base = wiki.data['Пример']
    for title in ('Другая', 'Новая'):
        wiki.data[title] = Revision(title, 1, time.time() - 1000, base.text)
    store.record_article_checks(slug, [dict(title=title, checked_at=10, outcome=outcome,
        reason='Ранее пропущено', run_id='old') for title in ('Пример', 'Другая')])
    refresh(wiki, store, slug)
    return wiki, worker


def enqueue(store, slug):
    store.enqueue('article:' + hashlib.sha256('Пример'.encode()).hexdigest(), 'article',
                  time.time() - 1, title='Пример', processor=slug, requested_by='admin')
    return next(job for job in store.queue(slug) if job['kind'] == 'article')


@pytest.mark.parametrize('slug', SLUGS)
def test_selected_retry_only_processes_that_article_and_keeps_other_marks(settings, store, slug):
    settings.wiki_write = True
    wiki, worker = prepare(store, slug)
    old = store.checked_articles(slug)['Другая']
    job = enqueue(store, slug)
    assert any(row['title'] == 'Пример' for row in report(store, slug)['pending_articles'])
    worker(settings, store, slug, wiki).execute(job)
    run = store.list_runs(processor=slug)[0]
    events = json.loads(run['events'])
    assert json.loads(run['summary'])['checked'] == 1 and run['status'] == 'success'
    assert {event['title'] for event in events if event['code'] == 'edited'} == {'Пример'}
    assert store.checked_articles(slug)['Другая'] == old
    assert 'Новая' not in store.checked_articles(slug)
    assert not store.queue(slug)


@pytest.mark.parametrize('slug', SLUGS)
def test_selected_dry_run_preserves_every_mark(settings, store, slug):
    wiki, worker = prepare(store, slug)
    before = store.checked_articles(slug)
    worker(settings, store, slug, wiki).execute(enqueue(store, slug))
    assert store.checked_articles(slug) == before and not wiki.edits


@pytest.mark.parametrize('slug', ['translations-categories', 'sections-empty-to-fill'])
def test_failed_selected_retry_keeps_override_until_the_next_daily_deadline(settings, store, slug):
    settings.wiki_write = True
    wiki, worker = prepare(store, slug)
    if slug.startswith('translations'):
        wiki.network_error = True
        # The base implementation injects a transient category API failure.
        wiki.category_members = TranslationWiki.category_members.__get__(wiki)
    else:
        wiki.error = 'network'
    before = store.checked_articles(slug)
    job = enqueue(store, slug)
    worker(settings, store, slug, wiki).execute(job)
    assert store.list_runs(processor=slug)[0]['status'] == 'failed'
    pending = store.queue(slug)
    assert len(pending) == 1 and pending[0]['kind'] == 'article'
    clock = '04:00'
    assert abs(pending[0]['due_at'] - next_daily_time(clock, time.time())) < 1
    assert store.checked_articles(slug) == before


@pytest.mark.parametrize('username', [None, 'Other', 'Admin'])
def test_retry_endpoint_rejects_other_users(settings, store, username):
    prepare(store, 'translations-categories')
    client = create_app(settings, store).test_client()
    set_session(client, username)
    assert client.post('/admin/tasks/translations-categories/retry-article',
                       data={'csrf': 'csrf-test', 'title': 'Пример'}).status_code == 403
    assert not store.queue('translations-categories')


def test_retry_endpoint_requires_csrf_coalesces_requests_and_does_not_erase_marks(settings, store):
    slug = 'sections-empty-to-fill'
    prepare(store, slug)
    before = store.checked_articles(slug)
    client = create_app(settings, store, admin_preview=True).test_client()
    set_session(client, 'admin')
    url = '/admin/tasks/' + slug + '/retry-article'
    assert client.post(url, data={'title': 'Пример'}).status_code == 400
    data = {'csrf': 'csrf-test', 'title': 'Пример'}
    with client.session_transaction() as session:
        session.pop('username')
        session['admin_preview'] = True
    assert client.post(url, data=data).status_code == 302 and not store.queue(slug)
    with client.session_transaction() as session:
        session.pop('admin_preview')
        session['username'] = 'admin'
    for _ in range(2):
        assert client.post(url, data=data).status_code == 302
    assert len(store.queue(slug)) == 1 and store.checked_articles(slug) == before
    assert client.post(url, data=dict(data, title='Новая')).status_code == 409
    store.change_control(slug, 'pause', 'admin')
    assert client.post(url, data=data).status_code == 409
