import json
import time
from unittest.mock import patch

import mwoauth
import pytest

from toolforge_app.storage import dump
from toolforge_app.config import Settings
from toolforge_app.web import create_app
from conftest import OPEN


@pytest.fixture
def client(settings, store, wiki):
    store.save_page(wiki.title, month=settings.start_month, revision=10,
        checked_at=time.time(), text=OPEN, issues=dump([{"type": "no_itog", "line": 3,
        "title": '<script>alert("XSS")</script> =Участник'}]))
    return create_app(settings, store).test_client()


def set_session(client, username=None):
    with client.session_transaction() as session:
        session["csrf"] = "csrf-test"
        if username:
            session["username"] = username


@pytest.mark.parametrize("path", ["/processors/obkat", "/processors/obkat?view=nuances", "/processors/obkat?view=logs",
    "/processors/maintenance", "/processors/translations", "/processors/sections", "/reports/obkat.txt",
    "/reports/obkat.json", "/reports/obkat.csv", "/healthz"])
def test_public_reports_accessible_without_login(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("username", [None, "AnotherUser", "Admin"])
def test_only_exact_authenticated_admin_can_enqueue(client, store, username):
    set_session(client, username)
    result = client.post("/admin/obkat/run", data={"csrf": "csrf-test"})
    assert result.status_code == 403
    assert not store.queue()


def test_admin_post_has_csrf_and_get_cannot_mutate(client, store):
    set_session(client, "admin")
    assert client.get("/admin/obkat/run").status_code == 405
    assert client.post("/admin/obkat/run", data={}).status_code == 400
    assert not store.queue()
    assert client.post("/admin/obkat/run", data={"csrf": "csrf-test", "mode": "spacing"}).status_code == 302
    assert store.queue()[0]["spacing"] and store.queue()[0]["requested_by"] == "admin"


@pytest.mark.parametrize("username", [None, "", "admin"])
def test_unconfigured_admin_has_no_management_access(settings, store, username):
    settings.admin_username = Settings().admin_username
    assert settings.admin_username == ""
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session["csrf"] = "csrf-test"
        if username is not None:
            session["username"] = username
    html = client.get("/processors/obkat").get_data(as_text=True)
    assert 'class="admin-panel"' not in html
    assert client.get("/notifications").status_code == 403
    assert client.post("/admin/obkat/run", data={"csrf": "csrf-test"}).status_code == 403
    assert not store.queue()


def test_escaping_search_and_csv_formula_protection(client):
    html = client.get("/processors/obkat?view=nuances").get_data(as_text=True)
    assert '<script>alert("XSS")</script>' not in html
    assert '&lt;script&gt;' not in html  # displayed heading is cleaned to plain text
    assert client.get("/processors/obkat?view=nuances&q=absent").status_code == 200
    csv = client.get("/reports/obkat.csv").get_data(as_text=True)
    assert "'=Участник" not in csv  # the title begins with alert, so it is safe text
    response = client.get("/processors/obkat")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_oauth_secrets_stay_out_of_browser_cookie_and_only_verified_identity_sets_user(client, store):
    request_token = mwoauth.RequestToken("request-key", "private-request-secret")
    with patch("toolforge_app.web.mwoauth.initiate", return_value=("https://meta.wikimedia.org/auth", request_token)):
        result = client.get("/login")
    assert result.status_code == 302
    with client.session_transaction() as session:
        pending_id = session["oauth_pending"]
        assert "username" not in session and "private-request-secret" not in str(dict(session))
    assert store.get_state("oauth:" + pending_id)["secret"] == "private-request-secret"
    with patch("toolforge_app.web.mwoauth.complete", return_value=mwoauth.AccessToken("access", "secret")), \
         patch("toolforge_app.web.mwoauth.identify", return_value={"username": "admin"}):
        assert client.get("/oauth/callback?oauth_token=request-key&oauth_verifier=verified").status_code == 302
    with client.session_transaction() as session:
        assert session["username"] == "admin"
        assert "secret" not in session and "access" not in session
    assert store.get_state("oauth:" + pending_id) is None
    assert client.get("/oauth/callback?oauth_token=request-key&oauth_verifier=verified").status_code == 400


def test_forged_or_expired_oauth_callback_cannot_login(client, store):
    with client.session_transaction() as session:
        session["oauth_pending"] = "expired"
    store.set_state("oauth:expired", {"key": "key", "secret": "secret", "expires": 1})
    assert client.get("/oauth/callback?oauth_token=key").status_code == 400
    with client.session_transaction() as session:
        assert "username" not in session


@pytest.mark.parametrize("configured,verified", [("admin", "OtherUser"), ("admin", "Admin"),
                                                ("admin", None), ("", "admin")])
def test_oauth_rejects_non_admin_identity_and_clears_previous_session(settings, store, configured, verified):
    settings.admin_username = configured
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session["username"] = "admin"
        session["oauth_pending"] = "pending"
    store.set_state("oauth:pending", {"key": "request-key", "secret": "secret", "expires": time.time() + 600})
    with patch("toolforge_app.web.mwoauth.complete", return_value=mwoauth.AccessToken("access", "secret")), \
         patch("toolforge_app.web.mwoauth.identify", return_value={"username": verified}):
        result = client.get("/oauth/callback?oauth_token=request-key&oauth_verifier=verified", follow_redirects=True)
    assert "Вход доступен только администратору" in result.get_data(as_text=True)
    with client.session_transaction() as session:
        assert "username" not in session
    assert client.get("/notifications").status_code == 403
    assert store.get_state("oauth:pending") is None


def test_csv_does_not_execute_heading_as_spreadsheet_formula(client, store, wiki):
    store.save_page(wiki.title, issues=dump([{"type": "no_itog", "line": 3,
        "title": '+HYPERLINK("https://example.org")'}]))
    csv = client.get("/reports/obkat.csv").get_data(as_text=True)
    # The actual heading is a formula; the export prefixes it with an apostrophe.
    assert "'+HYPERLINK" in csv


def test_historical_report_is_immutable_and_full_log_is_public(client, store):
    report = {"items": [], "counts": {}, "problems": 3, "nuances": 4, "pages": 1}
    run_id = store.start_run("full", "admin")
    store.finish_run(run_id, [{"at": 1, "code": "test", "message": "Recorded", "title": ""}], {}, report, "Index")
    assert client.get("/runs/" + run_id).status_code == 200
    assert client.get("/runs/" + run_id + "/log.json").get_json()["events"][0]["message"] == "Recorded"
    assert client.get("/reports/obkat.json?run=" + run_id).get_json()["problems"] == 3
    assert client.get("/runs/" + run_id + "/table.wiki").get_data(as_text=True) == "Index"


def test_public_layout_is_minimal_and_admin_layout_keeps_full_controls(client):
    for path in ("/", "/processors/obkat"):
        html = client.get(path).get_data(as_text=True)
        assert 'class="summary-bar"' not in html
        assert 'class="stat-card"' not in html
        assert 'class="task-controls"' not in html
        assert 'href="/login"' not in html
    set_session(client, "admin")
    for path in ("/", "/processors/obkat"):
        html = client.get(path).get_data(as_text=True)
        assert 'class="summary-bar"' not in html
        assert 'class="task-controls"' in html
        assert 'action="/logout"' in html


def test_public_log_is_continuous_escaped_text_and_can_be_downloaded(client, store):
    run_id = store.start_run("full", "admin")
    store.finish_run(run_id, [{"at": 1, "code": "error", "message": '<script>alert("log")</script>',
        "title": "Категория:Пример", "error": "network", "diff_before": "old\n", "diff_after": "new\n"}], {}, {})
    html = client.get("/runs/" + run_id).get_data(as_text=True)
    assert '<pre class="plain-log">' in html and '<script>alert("log")</script>' not in html
    assert "&lt;script&gt;" in html
    text = client.get("/runs/" + run_id + "/log.txt")
    assert text.status_code == 200 and text.mimetype == "text/plain"
    assert "Ошибка: network" in text.get_data(as_text=True)
    assert "-old" in text.get_data(as_text=True) and "+new" in text.get_data(as_text=True)
    set_session(client, "admin")
    assert '<ol class="event-list">' in client.get("/runs/" + run_id).get_data(as_text=True)


def test_nomination_report_has_log_as_last_tab_and_public_queue(client, store, wiki):
    store.enqueue("page:example", "page", time.time() + 1200, title=wiki.title)
    html = client.get("/processors/obkat").get_data(as_text=True)
    assert "Отчёт по номинациям" in html and "После последней правки" in html and "Через 15 минут" in html
    assert html.index('class="processing-block"') < html.index('id="settings"') < html.index('id="reports"')
    tabs = html.split('<nav class="tabs"')[1].split("</nav>")[0]
    assert tabs.index("Проблемы") < tabs.index("Открытые номинации") < tabs.index("Лог")
    assert '<section id="history"' not in html and "Нюансы" not in html
    logs = client.get("/processors/obkat?view=logs").get_data(as_text=True)
    assert '<section id="history"' in logs and 'class="filters"' not in logs
    assert "ruwiki works" in html and 'content="dark"' in html
    opened = client.get("/processors/obkat?view=open").get_data(as_text=True)
    assert '<ul class="open-nominations"' in opened and "<table" not in opened
    assert client.get("/processors/obkat?view=nuances").status_code == 200


def test_problem_report_explains_the_exact_heading_to_use(client, store, wiki):
    store.save_page(wiki.title, issues=dump([{"type": "wrong_itog_level", "line": 4,
        "title": "Итог", "level": 3, "parent_title": "Категория:Пример"}]))
    html = client.get("/processors/obkat").get_data(as_text=True)
    assert "Как исправить:" in html and "==== Итог ====" in html


def test_all_sections_share_overview_parameters_reports_and_log_navigation(client):
    for slug in ("obkat", "maintenance", "translations", "sections"):
        html = client.get("/processors/" + slug).get_data(as_text=True)
        assert html.index('class="processing-block"') < html.index('id="settings"') < html.index('id="reports"')
        assert 'class="processing-schedule"' in html and 'id="schedule-heading"' in html
        logs = client.get("/processors/" + slug + "?view=logs").get_data(as_text=True)
        assert "История запусков" in logs and '<section id="history"' in logs
