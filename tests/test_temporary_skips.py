import json

import pytest

from toolforge_app.processors.maintenance.service import MaintenanceWorker
from toolforge_app.processors.maintenance.inventory import report
from toolforge_app.processors.sections.service import SectionWorker
from toolforge_app.processors.sections.inventory import report as section_report
from toolforge_app.processors.translations.service import TranslationWorker
from toolforge_app.processors.translations.inventory import report as translation_report
from toolforge_app.processors.temporary import split_report
from toolforge_app.processors.logs import public_run
from toolforge_app.console import decode_run, run_metrics
from toolforge_app.web import create_app
from test_maintenance import MaintenanceWiki, configure, queued, DATES
from test_sections import SectionWiki, queue
from test_translations import TranslationWiki, job
from toolforge_app.wiki import WikiError


@pytest.mark.parametrize('slug', ['maintenance-dates', 'maintenance-rq', 'maintenance-rq-unwrap'])
def test_in_use_maintenance_is_temporary_and_retried_next_run(settings, store, slug):
    settings.wiki_write = True
    configure(store, slug)
    wiki = MaintenanceWiki(['{{Редактирую}}\n{{Факты}}'])
    worker = MaintenanceWorker(settings, store, slug, wiki)
    worker.execute(queued(store, slug))
    run = decode_run(store.list_runs(processor=slug)[0])
    assert run['status'] == 'success' and run['summary']['errors'] == 0
    assert run['summary']['temporary_skipped'] == 1
    current = report(store, slug)
    assert current['problems'] == 0 and current['temporary'] == 1 and current['to_process'] == 0
    assert not current['manual'] and not current['pending_articles'] and not wiki.edits
    assert not store.checked_articles(slug)
    projected = public_run(run)
    assert len(projected['events']) == 1 and projected['events'][0]['code'] == 'temporary_skip'
    assert 'Временный пропуск' in projected['events'][0]['message'] and projected['events'][0]['tone'] == 'neutral'
    metrics = run_metrics(run)
    assert next(m['value'] for m in metrics if m['label'] == 'временно пропущено') == 1
    assert next(m['value'] for m in metrics if m['label'] == 'пропущено') == 0
    if slug == DATES:
        wiki.base = type(wiki.base)(wiki.base.title, wiki.base.revision, wiki.base.edited_at, '{{Факты}}')
        worker.execute(queued(store, slug))
        assert wiki.edits and report(store, slug)['temporary'] == 0


@pytest.mark.parametrize('slug', ['translations-categories', 'translations-talk', 'sections-empty-to-fill'])
def test_other_daily_tasks_do_not_record_temporary_obstacles_in_skip_ledger(settings, store, slug):
    settings.wiki_write = True
    if slug.startswith('translations'):
        wiki = TranslationWiki()
        worker, pending, get_report = TranslationWorker(settings, store, slug, wiki), job(store, slug), translation_report
    else:
        wiki = SectionWiki()
        worker, pending, get_report = SectionWorker(settings, store, slug, wiki), queue(store, slug), section_report
    original = wiki.fetch
    def fetch(title):
        if title == 'Пример':
            raise WikiError('page-in-use')
        return original(title)
    wiki.fetch = fetch
    worker.execute(pending)
    run = decode_run(store.list_runs(processor=slug)[0])
    assert run['status'] == 'success' and not run['summary']['errors']
    current = get_report(store, slug)
    assert current['temporary'] == 1 and current['problems'] == 0
    assert not store.checked_articles(slug)
    wiki.fetch = original
    worker.execute(job(store, slug) if slug.startswith('translations') else queue(store, slug))
    assert wiki.edits and get_report(store, slug)['temporary'] == 0


def test_legacy_temporary_issue_is_shown_separately_without_changing_state(settings, store):
    config = configure(store, DATES)
    wiki = MaintenanceWiki()
    from toolforge_app.processors.maintenance.inventory import refresh_inventory, source_category
    refresh_inventory(wiki, store, DATES)
    original = {'source': source_category(DATES, config), 'at': 1000, 'manual': [
        {'title': 'Пример', 'reason': 'Статья сейчас редактируется участником; проверка будет повторена в следующий запуск'}]}
    store.set_state(DATES+':result', original)
    client = create_app(settings, store).test_client()
    html = client.get('/processors/maintenance?task=maintenance-dates&view=temporary').get_data(as_text=True)
    assert 'Временные пропуски' in html and 'вручную исправлять не нужно' in html
    assert 'Исправить 1' not in html and 'Требует исправления' not in html
    assert 'Временные пропуски: 1' in client.get('/').get_data(as_text=True)
    assert report(store, DATES)['problems'] == 0
    assert store.get_state(DATES+':result') == original
