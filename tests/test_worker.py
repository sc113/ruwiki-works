import json
import time
from datetime import datetime
from unittest.mock import Mock

from sqlalchemy import insert

from toolforge_app.processors.obkat.report import build_report, page_title
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


def test_network_failure_keeps_cursor_and_jobs(settings, store, wiki):
    store.set_state("rc_cursor", time.time() - 100)
    cursor = store.get_state("rc_cursor")
    queued(store, wiki.title)
    wiki.poll_error = True
    Worker(settings, store, wiki).tick()
    assert store.get_state("rc_cursor") == cursor
    assert len(store.queue()) == 1 and not store.list_runs()
    assert store.get_state("worker_error")["code"] == "network"


def test_failed_observer_backs_off_instead_of_polling_every_worker_tick(settings, store, wiki):
    now = time.time()
    wiki.changes = Mock(side_effect=WikiError('network'))
    Worker(settings, store, wiki).watch(now)
    assert store.get_state('obkat:observer_retry')['due_at'] == now + 60
    restarted = Worker(settings, store, wiki)
    restarted.watch(now + 5)
    wiki.changes.assert_called_once()
    restarted.watch(now + 60)
    assert wiki.changes.call_count == 2
    assert store.get_state('obkat:observer_retry')['due_at'] == now + 180
    wiki.changes = Mock(return_value=[])
    restarted.watch(now + 180)
    assert store.get_state('obkat:observer_retry') is None
    assert store.get_state('worker_error') is None


def test_manual_job_starts_between_wiki_polls(settings, store, wiki):
    now = time.time()
    store.set_state("last_poll", now)
    store.set_state("last_reconcile", now)
    store.set_state("live_sync_initialized", True)
    wiki.changes = Mock(return_value=[])
    queued(store, wiki.title, "full")
    worker = Worker(settings, store, wiki)
    worker.tick(now + 5)
    assert store.list_runs()[0]["status"] == "success"
    wiki.changes.assert_not_called()
    worker.tick(now + settings.poll_seconds + 1)
    wiki.changes.assert_called_once()


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


def test_poll_recovers_a_change_missing_from_recentchanges_and_keeps_its_deadline(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    now = time.time()
    edited = now - 180
    store.save_page(wiki.title, month=settings.start_month, revision=10, origin='wiki', text='', issues='[]')
    wiki.data[wiki.title] = Revision(wiki.title, 11, edited, wiki.data[wiki.title].text)
    wiki.changes = Mock(return_value=[])
    store.set_state('rc_cursor', now - 300)
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


def test_poll_overlaps_feed_and_does_not_advance_cursor_when_revision_check_fails(settings, store, wiki):
    import pytest
    now = time.time()
    store.set_state('rc_cursor', now - 300)
    wiki.changes = Mock(return_value=[])
    wiki.revisions = Mock(side_effect=WikiError('network'))
    with pytest.raises(WikiError):
        Worker(settings, store, wiki).poll(now)
    assert wiki.changes.call_args.args == (now - 900, now)
    assert store.get_state('rc_cursor') == now - 300 and store.get_state('last_poll') is None


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
