import time
from datetime import datetime

import pytest
from sqlalchemy import update

from toolforge_app.console import console_data, run_history
from toolforge_app.run_statistics import report_changes, report_snapshot
from toolforge_app.storage import runs
from toolforge_app.web import create_app
from test_maintenance import recorded
from test_web import set_session


def timed_run(store, at, slug="maintenance-dates", kind="daily", **summary):
    run_id = store.start_run(kind, processor=slug, dry_run=False)
    store.finish_run(run_id, [], summary or dict(checked=20, changed=12, skipped=3), {})
    with store.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(started_at=at, finished_at=at + 65))
    return run_id


def test_public_console_uses_the_same_saved_changes_projection_as_logs(settings, store):
    run_id = recorded(store)
    client = create_app(settings, store).test_client()
    for path in ("/console", "/console/fragment", "/runs/recent-fragment"):
        text = client.get(path).get_data(as_text=True)
        assert run_id in text and "PRIVATE_" not in text and "SECRET_ERROR" not in text
        if "console" in path:
            assert "Обработанная статья" in text and "2020-01-02" in text
            assert "Failed article" in text and "Unchanged article" not in text
    set_session(client, "admin")
    text = client.get("/console").get_data(as_text=True)
    assert "PRIVATE_DIAGNOSTIC" in text
    assert "PRIVATE_OLD_TEXT" in client.get("/runs/" + run_id + "/events/3").get_data(as_text=True)
    assert "Подробности" in text and "Полный журнал" in text


def test_history_filters_use_moscow_calendar_days_and_keep_duration(settings, store):
    early = datetime(2026, 10, 6, 0, 5, tzinfo=settings.zone).timestamp()
    included = timed_run(store, early, checked=10, changed=4, report_problems=3, problems_added=2, skipped=1)
    excluded = timed_run(store, early - 600, slug="translations-talk")
    client = create_app(settings, store).test_client()
    for endpoint in ("/runs", "/runs/history-fragment"):
        text = client.get(endpoint + "?day=2026-10-06").get_data(as_text=True)
        assert included in text and excluded not in text
        assert "1 мин 5 с" in text and "+2 новые" in text
        assert "06.10.2026" in text and "00:05:00" in text
    assert excluded in client.get("/runs?task=translations-talk").get_data(as_text=True)
    assert included not in client.get("/runs?task=translations-talk").get_data(as_text=True)


def test_history_paginates_all_runs_and_index_shows_only_eight(settings, store):
    run_ids = [timed_run(store, 1000 + index, slug="obkat") for index in range(30)]
    imported = timed_run(store, 9000, kind="import")
    history = run_history(store)
    assert [entry['run']['id'] for entry in history['entries']] == list(reversed(run_ids[5:]))
    assert history['has_next']
    client = create_app(settings, store).test_client()
    second = client.get("/runs?page=2").get_data(as_text=True)
    # The live console may contain newer events; the table is strictly paginated.
    table = second.split('<table class="runs-table"')[1].split('</table>')[0]
    assert all(run_id in table for run_id in run_ids[:5])
    assert all(run_id not in table for run_id in run_ids[5:])
    assert imported not in second
    index = client.get("/").get_data(as_text=True).split('class="recent-runs"')[1]
    assert all(run_id in index for run_id in run_ids[-8:])
    assert all(run_id not in index for run_id in run_ids[:-8])
    assert '/runs' in index


@pytest.mark.parametrize("query", ["day=invalid", "day=2026-02-30", "status=missing", "page=0", "page=abc"])
def test_invalid_history_filters_are_rejected(settings, store, query):
    assert create_app(settings, store).test_client().get("/console?" + query).status_code == 400


def test_report_comparison_counts_new_issues_even_when_total_is_unchanged():
    def issue(kind, line):
        return dict(page="Обсуждение", type=kind, display_title="Номинация", line=line, section="problems")
    before = report_snapshot(dict(items=[issue("wrong_itog_level", 20), issue("not_struck", 30)], problems=2))
    after = dict(items=[issue("wrong_itog_level", 25), issue("level_skip", 40)], problems=2)
    changes = report_changes(before, after)
    assert changes['report_problems'] == 2
    assert changes['problems_added'] == 1 and changes['problems_resolved'] == 1


def test_running_history_uses_live_events_and_keeps_a_link_to_the_run(settings, store):
    run_id = store.start_run("daily", processor="maintenance-dates")
    store.update_progress(run_id, [dict(at=time.time(), code="unchanged", message="checked", title="Пример")])
    feed = console_data(store, full=False)
    assert feed['running'][0]['run']['id'] == run_id
    assert feed['history']['entries'][0]['metrics'][0]['value'] == 1
    assert all(event['message'] != 'checked' for event in feed['events'])
