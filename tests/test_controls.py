import time
from unittest.mock import Mock

import pytest

from toolforge_app.storage import Store
from toolforge_app.worker import Worker
from test_web import client, set_session
from test_worker import queued


@pytest.mark.parametrize("username", [None, "AnotherUser", "Admin"])
def test_public_users_cannot_control_tasks(client, store, username):
    set_session(client, username)
    for action in ("pause", "resume", "stop", "restart"):
        assert client.post("/admin/tasks/obkat/control",
            data={"csrf": "csrf-test", "action": action}).status_code == 403
    assert store.control()["mode"] == "active" and not store.queue()
    assert 'name="action"' not in client.get("/").get_data(as_text=True)


def test_admin_controls_require_csrf_and_reject_unknown_or_unconnected_tasks(client, store):
    set_session(client, "admin")
    assert client.get("/admin/tasks/obkat/control").status_code == 405
    assert client.post("/admin/tasks/obkat/control", data={"action": "stop"}).status_code == 400
    for slug, action, expected in (("obkat", "invalid", 400), ("missing", "pause", 404),
                                  ("sections", "restart", 404)):
        assert client.post("/admin/tasks/" + slug + "/control",
            data={"csrf": "csrf-test", "action": action}).status_code == expected
    assert store.control()["mode"] == "active" and not store.queue()
    assert 'name="action"' in client.get("/").get_data(as_text=True)
    html = client.get("/processors/obkat").get_data(as_text=True)
    assert 'Пауза' in html and 'Перезапустить' not in html


def test_pause_keeps_queue_across_restart_and_stop_clears_it(client, settings, store, wiki):
    set_session(client, "admin")
    job = queued(store, wiki.title, spacing=True)
    assert client.post("/admin/tasks/obkat/control", data={"csrf": "csrf-test", "action": "pause"}).status_code == 302
    reopened = Store(settings.database_url)
    try:
        assert reopened.control()["mode"] == "paused"
        assert reopened.queue()[0] == job
    finally:
        reopened.engine.dispose()
    assert client.post("/admin/obkat/run", data={"csrf": "csrf-test"}).status_code == 409
    response = client.post("/admin/tasks/obkat/control",
        data={"csrf": "csrf-test", "action": "stop"}, follow_redirects=True)
    assert response.status_code == 200
    assert "Отключён" in response.get_data(as_text=True)
    assert store.control()["mode"] == "stopped" and not store.queue()
    assert not store.enqueue("page:new", "page", time.time(), title=wiki.title)


def test_restart_enqueues_one_full_run_and_resume_keeps_existing_jobs(client, store, wiki):
    set_session(client, "admin")
    job = queued(store, wiki.title)
    store.change_control("obkat", "pause", "admin")
    client.post("/admin/tasks/obkat/control", data={"csrf": "csrf-test", "action": "resume"})
    assert store.control()["mode"] == "active" and store.queue()[0] == job
    store.change_control("obkat", "stop", "admin")
    client.post("/admin/tasks/obkat/control", data={"csrf": "csrf-test", "action": "restart"})
    assert store.control()["mode"] == "active"
    assert len(store.queue()) == 1 and store.queue()[0]["kind"] == "full"
    assert store.queue()[0]["requested_by"] == "admin"


@pytest.mark.parametrize("action,status,queue_count", [("pause", "paused", 1), ("stop", "stopped", 0),
                                                       ("restart", "interrupted", 1)])
def test_command_during_fetch_prevents_edits_and_records_the_run(settings, store, wiki, action, status, queue_count):
    settings.wiki_write = True
    job = queued(store, wiki.title, "full")
    original_fetch = wiki.fetch

    def fetch_then_control(title):
        base = original_fetch(title)
        store.change_control("obkat", action, "admin")
        return base

    wiki.fetch = fetch_then_control
    Worker(settings, store, wiki).execute(job)
    assert wiki.edits == []
    assert store.list_runs()[0]["status"] == status
    assert len(store.queue()) == queue_count
    if action == "restart":
        assert store.run_request(job_key=store.queue()[0]["key"])


def test_pause_after_a_write_preserves_the_actual_edit_and_skips_index(settings, store, wiki):
    settings.wiki_write = True
    job = queued(store, wiki.title, "full")
    original_edit = wiki.edit

    def edit_then_pause(base, text, summary):
        revision = original_edit(base, text, summary)
        store.change_control("obkat", "pause", "admin")
        return revision

    wiki.edit = edit_then_pause
    Worker(settings, store, wiki).execute(job)
    assert len(wiki.edits) == 1
    assert store.page(wiki.title)["revision"] == 11
    assert store.list_runs()[0]["status"] == "paused"
    assert len(store.queue()) == 1


def test_fast_pause_and_resume_still_interrupts_the_old_pass(settings, store, wiki):
    settings.wiki_write = True
    job = queued(store, wiki.title)
    original_fetch = wiki.fetch

    def fetch_then_resume(title):
        base = original_fetch(title)
        store.change_control("obkat", "pause", "admin")
        store.change_control("obkat", "resume", "admin")
        return base

    wiki.fetch = fetch_then_resume
    Worker(settings, store, wiki).execute(job)
    assert wiki.edits == [] and store.list_runs()[0]["status"] == "interrupted"
    assert len(store.queue()) == 1


def test_stopped_worker_skips_network_and_paused_worker_observes_without_executing(settings, store, wiki):
    worker = Worker(settings, store, wiki)
    store.change_control("obkat", "stop", "admin")
    wiki.changes = Mock(return_value=[])
    worker.tick()
    wiki.changes.assert_not_called()
    store.change_control("obkat", "pause", "admin")
    worker.tick()
    wiki.changes.assert_called_once()
    assert store.queue() and not store.list_runs() and not wiki.edits


def test_restart_before_execution_cannot_run_an_obsolete_job(settings, store, wiki):
    old_job = queued(store, wiki.title, spacing=True)
    store.change_control("obkat", "restart", "admin")
    Worker(settings, store, wiki).execute(old_job)
    assert not store.list_runs() and not wiki.edits
    assert len(store.queue()) == 1 and store.run_request(job_key=store.queue()[0]["key"])


def test_real_api_checks_stop_between_token_fetch_and_edit(settings, store):
    from toolforge_app.processors.obkat.report import page_title
    from toolforge_app.wiki import Revision, WikiClient
    from toolforge_app.worker import RunControlled
    settings.wiki_write = True
    settings.bot_username = "ExampleBot"
    client = WikiClient(settings)
    client.login = Mock()
    worker = Worker(settings, store, client)
    worker.execution_generation = store.control()["generation"]
    response = Mock(status_code=200)

    def token_then_stop():
        store.change_control("obkat", "stop", "admin")
        return {"query": {"tokens": {"csrftoken": "TOKEN"}}}

    response.json.side_effect = token_then_stop
    client.session.get = Mock(return_value=response)
    client.session.post = Mock()
    with pytest.raises(RunControlled):
        client.edit(Revision(page_title(settings.start_month), 1, 1, "text"), "updated", "summary")
    client.session.post.assert_not_called()
