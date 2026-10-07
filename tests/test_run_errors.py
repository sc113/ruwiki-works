"""A session/service failure stops a pass without marking articles as skipped."""
import json
from unittest.mock import Mock

import pytest

from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.processors.sections.service import SectionWorker
from toolforge_app.processors.translations.service import TranslationWorker
from toolforge_app.wiki import Revision, WikiError
from toolforge_app.worker import Worker
from test_maintenance import MaintenanceWiki, configure, queued
from test_sections import SectionWiki, queue
from test_translations import TranslationWiki, job
from test_worker import queued as obkat_job


@pytest.mark.parametrize('slug', ['maintenance-dates', 'translations-categories', 'translations-talk',
                                'sections-empty-to-fill', 'sections-fill-to-empty'])
@pytest.mark.parametrize('code', ['network', 'badtoken'])
def test_global_error_stops_after_first_article(settings, store, slug, code):
    if slug.startswith('maintenance'):
        wiki = MaintenanceWiki()
        configure(store, slug)
        worker, pending = MaintenanceWorker(settings, store, slug, wiki), queued(store, slug)
    elif slug.startswith('translations'):
        wiki = TranslationWiki()
        worker, pending = TranslationWorker(settings, store, slug, wiki), job(store, slug)
    else:
        wiki = SectionWiki('== A ==\n{{Пустой раздел}}{{Дополнить раздел}}')
        wiki.data['Я'] = Revision('Я', 1, 1, wiki.data['Пример'].text)
        worker, pending = SectionWorker(settings, store, slug, wiki), queue(store, slug)
    if hasattr(wiki, 'category_members'):
        original_members = wiki.category_members
        def members(*args, **kwargs):
            result = original_members(*args, **kwargs)
            if any(row['title'] == 'Пример' for row in result):
                return [{'title': 'Пример'}, {'title': 'Я'}]
            return result
        wiki.category_members = members
    original_fetch = wiki.fetch
    fetched = []
    def fetch(title):
        if title in {'Пример', 'Я'}:
            fetched.append(title)
            raise WikiError(code)
        return original_fetch(title)
    wiki.fetch = fetch
    worker.execute(pending)
    run = store.list_runs(processor=slug)[0]
    assert run['status'] == 'failed'
    assert fetched == ['Пример']
    assert json.loads(run['summary'])['errors'] == 1
    assert not wiki.edits and not store.checked_articles(slug)
    assert not store.queue(slug)  # Next attempt is scheduled for the next night.


def test_obkat_global_failure_stops_monthly_pass_and_preserves_retry(settings, store, wiki, monkeypatch):
    monkeypatch.setattr('toolforge_app.worker.month_range', lambda *args: ['2026-01', '2026-02'])
    wiki.fetch = Mock(side_effect=WikiError('network'))
    Worker(settings, store, wiki).execute(obkat_job(store, wiki.title, 'full'))
    wiki.fetch.assert_called_once()
    assert store.list_runs()[0]['status'] == 'failed'
    assert store.queue()[0]['attempts'] == 1


def test_server_cooldown_preserves_due_jobs_instead_of_failing_every_task(settings, store):
    import time
    from toolforge_app.execution import ready_jobs
    now = time.time()
    queued(store, 'maintenance-dates')
    store.set_state('wiki:api_backoff', dict(code='ratelimited', until=now + 300))
    assert ready_jobs(store, now) == []
    assert len(store.queue('maintenance-dates')) == 1
    assert len(ready_jobs(store, now + 300)) == 1


def test_obkat_observer_failure_does_not_block_other_actions(settings, store, wiki):
    import time
    from toolforge_app.execution import ready_jobs
    now = time.time()
    obkat_job(store, wiki.title, 'full')
    queued(store, 'maintenance-dates')
    store.set_state('obkat:observer_retry', dict(attempts=1, due_at=now + 60))
    assert [job['processor'] for job in ready_jobs(store, now)] == ['maintenance-dates']
    assert [job['processor'] for job in ready_jobs(store, now + 60)] == ['obkat', 'maintenance-dates']
