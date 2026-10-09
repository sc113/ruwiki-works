import json
import time
from datetime import datetime
from unittest.mock import Mock

from sqlalchemy import insert

from toolforge_app.processors.obkat.report import month_range, page_title
from toolforge_app.storage import dump, jobs
from toolforge_app.wiki import Revision, WikiError
from toolforge_app.worker import Worker


def queued(store, title, kind="page", spacing=False):
    store.enqueue("page:test" if kind == "page" else "manual:test", kind, time.time() - 1,
                  title=title, revision=10, spacing=spacing)
    return store.queue()[0]


def test_debounce_tracks_last_edit_and_survives_restart(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    now = time.time()
    worker.observe(wiki.title, 10, now, now)
    worker.observe(wiki.title, 11, now + 300, now + 300)
    worker.observe(wiki.title, 10, now, now + 300)
    from toolforge_app.storage import Store
    restarted = Store(settings.database_url)
    assert restarted.queue()[0]["due_at"] == now + 300 + settings.quiet_minutes * 60
    assert restarted.queue()[0]["revision"] == 11
    restarted.engine.dispose()


def test_own_written_revision_does_not_requeue(settings, store, wiki):
    store.save_page(wiki.title, month=settings.start_month, revision=10, text="", issues="[]", origin="wiki")
    Worker(settings, store, wiki).observe(wiki.title, 10, time.time(), time.time())
    assert store.queue() == []


def test_dry_run_never_edits_and_report_describes_actual_page(settings, store, wiki):
    Worker(settings, store, wiki).execute(queued(store, wiki.title, "full"))
    assert wiki.edits == []
    assert store.page(wiki.title)["text"] == wiki.data[wiki.title].text
    run = store.list_runs()[0]
    assert run["status"] == "success" and run["dry_run"]
    assert json.loads(run["summary"])["proposed"] == 1
    assert any(e["code"] == "would_edit" and e["diff_after"] for e in json.loads(run["events"]))
    assert not store.queue()


def test_live_page_and_index_writes_are_recorded(settings, store, wiki):
    settings.wiki_write = True
    Worker(settings, store, wiki).execute(queued(store, wiki.title, "full"))
    assert len(wiki.edits) == 2
    assert store.page(wiki.title)["revision"] == 11
    assert "<s>" in store.page(wiki.title)["text"]
    assert json.loads(store.list_runs()[0]["summary"])["changed"] == 1
    Worker(settings, store, wiki).observe(wiki.title, 11, time.time(), time.time())
    assert store.queue() == []


def test_conflict_does_not_overwrite_page_or_publish_index_and_retries(settings, store, wiki):
    settings.wiki_write = True
    wiki.conflict = True
    worker = Worker(settings, store, wiki)
    worker.execute(queued(store, wiki.title))
    assert wiki.edits == [] and store.page(wiki.title) is None
    assert store.list_runs()[0]["status"] == "failed"
    assert store.queue()[0]["attempts"] == 1
    assert store.queue()[0]["due_at"] > time.time()


def test_table_failure_does_not_lose_the_page_job(settings, store, wiki):
    wiki.table_error = True
    worker = Worker(settings, store, wiki)
    worker.execute(queued(store, wiki.title))
    assert store.list_runs()[0]["status"] == "failed"
    assert len(store.queue()) == 1
    assert store.queue()[0]["attempts"] == 1


def test_bot_excluded_page_is_analyzed_without_writing_or_endless_retry(settings, store, wiki):
    settings.wiki_write = True
    current = wiki.data[wiki.title]
    wiki.data[wiki.title] = Revision(wiki.title, current.revision, current.edited_at, "{{nobots}}\n" + current.text)
    Worker(settings, store, wiki).execute(queued(store, wiki.title, "full"))
    assert all(base.title != wiki.title for base, _, _ in wiki.edits)
    assert store.page(wiki.title)["revision"] == 10
    assert "{{nobots}}" in store.page(wiki.title)["text"]
    assert not store.queue()
    assert any(e["code"] == "bot_excluded" for e in json.loads(store.list_runs()[0]["events"]))


def test_new_revision_at_fetch_defers_edit_and_preserves_monthly_spacing(settings, store, wiki):
    now = time.time()
    wiki.data[wiki.title] = Revision(wiki.title, 11, now, wiki.data[wiki.title].text)
    worker = Worker(settings, store, wiki)
    store.enqueue("month_end:2026-10", "month_end", now - 1, spacing=True)
    worker.execute(store.queue()[0])
    pending = [j for j in store.queue() if j["kind"] == "page"]
    assert pending[0]["spacing"]
    assert pending[0]["due_at"] == now + settings.quiet_minutes * 60
    assert pending[0]["revision"] == 11
    assert not wiki.edits
    assert len(store.queue()) == 1


def test_duplicate_manual_requests_coalesce(store, wiki):
    assert store.enqueue("manual:normal", "full", 1, title=wiki.title)
    assert not store.enqueue("manual:normal", "full", 2, title=wiki.title)
    assert len(store.queue()) == 1


def test_acknowledgment_preserves_newer_edit(store, wiki):
    store.enqueue("page:test", "page", 1, title=wiki.title, revision=10)
    claimed = store.queue()[0]
    store.enqueue("page:test", "page", 2, title=wiki.title, revision=11)
    store.acknowledge(claimed)
    assert store.queue()[0]["revision"] == 11


def test_distributed_lease_prevents_parallel_worker(store):
    with store.worker_lease() as first:
        assert first is not None
        with store.worker_lease() as second:
            assert second is None
        first()
    with store.worker_lease() as third:
        assert third is not None


def test_month_end_scheduled_once_in_moscow_and_catches_up(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    before = datetime(2026, 10, 31, 23, 29, tzinfo=settings.zone).timestamp()
    after = before + 60
    worker.schedule_month_end(before)
    assert not store.queue()
    worker.schedule_month_end(after)
    worker.schedule_month_end(after + 60)
    assert len(store.queue()) == 1 and store.queue()[0]["spacing"]
    store.set_state("last_month_end", "2026-10")
    worker.schedule_month_end(after + 120)
    assert len(store.queue()) == 1
    worker.schedule_month_end(datetime(2026, 12, 2, tzinfo=settings.zone).timestamp())
    assert {j["key"] for j in store.queue()} == {"month_end:2026-10", "month_end:2026-11"}


def test_network_failure_keeps_last_successful_check_and_jobs(settings, store, wiki):
    last_check = time.time() - settings.poll_seconds - 1
    store.set_state("last_poll", last_check)
    queued(store, wiki.title)
    wiki.poll_error = True
    Worker(settings, store, wiki).tick()
    assert store.get_state("last_poll") == last_check
    assert len(store.queue()) == 1 and not store.list_runs()
    assert store.get_state("worker_error")["code"] == "network"


def test_failed_observer_backs_off_instead_of_polling_every_worker_tick(settings, store, wiki):
    now = time.time()
    wiki.revisions = Mock(side_effect=WikiError('network'))
    Worker(settings, store, wiki).watch(now)
    assert store.get_state('obkat:observer_retry')['due_at'] == now + 60
    restarted = Worker(settings, store, wiki)
    restarted.watch(now + 5)
    wiki.revisions.assert_called_once()
    restarted.watch(now + 60)
    assert wiki.revisions.call_count == 2
    assert store.get_state('obkat:observer_retry')['due_at'] == now + 180
    wiki.revisions = Mock(return_value=[])
    restarted.watch(now + 180)
    assert store.get_state('obkat:observer_retry') is None
    assert store.get_state('worker_error') is None


def test_manual_job_starts_between_wiki_polls(settings, store, wiki):
    now = time.time()
    store.set_state("last_poll", now)
    store.set_state("last_reconcile", now)
    store.set_state("live_sync_initialized", True)
    wiki.revisions = Mock(wraps=wiki.revisions)
    queued(store, wiki.title, "full")
    worker = Worker(settings, store, wiki)
    worker.tick(now + 5)
    assert store.list_runs()[0]["status"] == "success"
    wiki.revisions.assert_not_called()
    worker.tick(now + settings.poll_seconds + 1)
    wiki.revisions.assert_called_once()


def test_bootstrap_covers_missing_months(settings, store, wiki):
    now = datetime.now(settings.zone)
    if now.month == 1:
        settings.start_month = f"{now.year - 1}-12"
    else:
        settings.start_month = f"{now.year}-{now.month - 1:02d}"
    # The fake provides only the current page; the previous month is missing.
    Worker(settings, store, wiki).tick()
    assert len(store.all_pages()) == 2
    assert any(p["missing"] for p in store.all_pages())
    assert store.get_state("live_sync_initialized")
    assert not store.queue()


def test_poll_recovers_a_change_after_downtime_and_keeps_its_deadline(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    now = time.time()
    edited = now - 180
    store.save_page(wiki.title, month=settings.start_month, revision=10, origin='wiki', text='', issues='[]')
    wiki.data[wiki.title] = Revision(wiki.title, 11, edited, wiki.data[wiki.title].text)
    store.set_state('last_poll', now - 60 * 86400)
    worker.poll(now)
    job = store.queue()[0]
    assert job['revision'] == 11 and job['due_at'] == edited + 15 * 60
    # Clearing an observed job must not permanently lose its revision.
    store.acknowledge(job)
    worker.poll(now + 300)
    assert store.queue()[0]['due_at'] == edited + 15 * 60
    worker.poll(now + 600)
    assert store.queue()[0]['due_at'] == edited + 15 * 60
    assert not wiki.edits


def test_failed_revision_check_does_not_advance_successful_check_time(settings, store, wiki):
    import pytest
    now = time.time()
    store.set_state('last_poll', now - 300)
    store.set_state('last_reconcile', now - 300)
    wiki.revisions = Mock(side_effect=WikiError('network'))
    with pytest.raises(WikiError):
        Worker(settings, store, wiki).poll(now)
    assert store.get_state('last_poll') == now - 300
    assert store.get_state('last_reconcile') == now - 300


def test_unchanged_poll_uses_one_metadata_read_and_no_cached_text(settings, store, wiki, monkeypatch):
    settings.start_month = '2025-01'
    now = datetime(2026, 10, 9, tzinfo=settings.zone).timestamp()
    for month in month_range(settings.start_month, datetime.fromtimestamp(now, settings.zone)):
        title = page_title(month)
        store.save_page(title, month=month, revision=10, origin='wiki')
        wiki.data[title] = Revision(title, 10, now - 3600, 'Text that observation must not load')
    headers = Mock(wraps=store.page_headers)
    monkeypatch.setattr(store, 'page_headers', headers)
    monkeypatch.setattr(store, 'all_pages', Mock(side_effect=AssertionError('Loaded all cached text')))
    monkeypatch.setattr(store, 'page', Mock(side_effect=AssertionError('Loaded cached page text')))
    wiki.revisions = Mock(wraps=wiki.revisions)
    Worker(settings, store, wiki).poll(now)
    headers.assert_called_once_with()
    assert wiki.revisions.call_count == 2
    assert all(len(call.args[0]) <= 20 and not call.kwargs.get('content') for call in wiki.revisions.call_args_list)
    assert not store.queue() and not wiki.edits


def test_idle_observer_does_not_load_or_build_reports(settings, store, wiki, monkeypatch):
    now = time.time()
    store.set_state('last_poll', now)
    store.set_state('last_reconcile', now)
    monkeypatch.setattr(store, 'all_pages', Mock(side_effect=AssertionError('Loaded report text while idle')))
    wiki.revisions = Mock(side_effect=AssertionError('Polled before search interval elapsed'))
    worker = Worker(settings, store, wiki)
    assert worker.watch(now + 5)
    assert worker.watch(now + 10)
    assert store.get_state('last_poll') == now


def test_revision_saved_during_poll_is_not_requeued(settings, store, wiki):
    now = time.time()
    store.save_page(wiki.title, month=settings.start_month, revision=10, origin='wiki')

    def revisions(titles):
        store.save_page(wiki.title, month=settings.start_month, revision=11, origin='wiki')
        return [Revision(wiki.title, 11, now, 'Saved by executor')]

    wiki.revisions = revisions
    Worker(settings, store, wiki).poll(now)
    assert not store.queue()


def test_repeated_poll_preserves_retry_backoff_and_monthly_spacing(settings, store, wiki):
    now = time.time()
    worker = Worker(settings, store, wiki)
    worker.observe(wiki.title, 10, now - 1000, now)
    job = store.queue()[0]
    store.enqueue(job['key'], 'page', job['due_at'], title=wiki.title, revision=10, spacing=True)
    store.retry(store.queue()[0], now)
    retry = store.queue()[0]
    worker.poll(now + 5)
    assert store.queue()[0] == retry


def test_missing_page_retry_is_not_reset_by_poll(settings, store, wiki):
    now = time.time()
    store.save_page(wiki.title, month=settings.start_month, revision=10, origin='wiki')
    wiki.data.pop(wiki.title)
    worker = Worker(settings, store, wiki)
    worker.poll(now)
    assert store.queue()[0]['due_at'] == now
    store.retry(store.queue()[0], now)
    retry = store.queue()[0]
    worker.poll(now + 5)
    assert store.queue()[0] == retry


def test_new_edit_before_due_run_extends_quiet_period_without_publishing_new_text(settings, store, wiki):
    settings.wiki_write = True
    now = time.time()
    worker = Worker(settings, store, wiki)
    worker.observe(wiki.title, 10, now - 1000, now - 1000)
    wiki.data[wiki.title] = Revision(wiki.title, 11, now - 60, wiki.data[wiki.title].text)
    worker.execute(store.queue()[0])
    assert not wiki.edits
    assert store.queue()[0]['revision'] == 11 and store.queue()[0]['due_at'] == now - 60 + 15 * 60
    assert json.loads(store.list_runs()[0]['summary'])['deferred'] == 1


def test_obkat_edit_comments_use_action_names_without_abbreviation(settings, store, wiki):
    settings.wiki_write = True
    Worker(settings, store, wiki).execute(queued(store, wiki.title, 'full', spacing=True))
    assert [comment for _, _, comment in wiki.edits] == [
        'Автоформатирование и зачёркивание завершённых', 'Обновление таблицы открытых номинаций']
