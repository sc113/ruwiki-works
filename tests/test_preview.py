import re

import pytest

from toolforge_app.web import create_app


def switch(client, mode="admin", target="/processors/obkat?view=open"):
    html = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    return client.post("/preview", data={"csrf": token, "mode": mode, "next": target}, follow_redirects=True)


def test_preview_shows_admin_views_without_authentication_or_mutations(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    html = switch(client).get_data(as_text=True)
    assert "Предпросмотр admin" in html and "Только просмотр" in html
    assert 'class="admin-panel"' in html and "С форматированием" in html
    with client.session_transaction() as session:
        assert "username" not in session
        csrf = session["csrf"]
    before = store.control()
    for path, data in (("/admin/obkat/run", {"mode": "spacing"}),
                       ("/admin/tasks/obkat/control", {"action": "restart"}),
                       ("/admin/tasks/obkat/control", {"action": "stop"})):
        response = client.post(path, data={"csrf": csrf, **data}, follow_redirects=True)
        assert "Команды управления не выполняются" in response.get_data(as_text=True)
        assert store.control() == before and store.queue() == [] and store.list_runs() == []
    admin_overview = client.get("/tasks/overview-fragment").get_data(as_text=True)
    assert 'class="task-controls"' in admin_overview and 'class="summary-bar"' not in admin_overview
    assert 'class="preview-banner"' not in html
    public = switch(client, "public").get_data(as_text=True)
    assert 'class="admin-panel"' not in public and "Предпросмотр admin" not in public
    assert 'class="summary-bar"' not in client.get("/tasks/overview-fragment").get_data(as_text=True)
    assert 'class="task-controls"' not in client.get("/tasks/overview-fragment").get_data(as_text=True)
    assert client.post("/admin/obkat/run", data={"csrf": csrf}).status_code == 403


def test_preview_is_absent_in_the_default_application(settings, store):
    client = create_app(settings, store).test_client()
    with client.session_transaction() as session:
        session["csrf"] = "csrf-test"
        session["admin_preview"] = True
    html = client.get("/processors/obkat").get_data(as_text=True)
    assert "Режим admin" not in html and 'class="admin-panel"' not in html
    assert client.post("/preview", data={"csrf": "csrf-test", "mode": "admin"}).status_code == 404


@pytest.mark.parametrize("username", ["", "Private configured identity"])
def test_preview_uses_a_generic_role_label(settings, store, username):
    settings.admin_username = username
    client = create_app(settings, store, admin_preview=True).test_client()
    html = switch(client).get_data(as_text=True)
    assert "Предпросмотр admin" in html
    assert 'class="avatar">a</span>' in html
    assert "Private configured identity" not in html


@pytest.mark.parametrize("setting,value", [("public_url", "https://ruwiki-works.toolforge.org"),
                                         ("public_url", "http://example.org"), ("wiki_write", True)])
def test_preview_cannot_start_with_external_url_or_wiki_writes(settings, store, setting, value):
    setattr(settings, setting, value)
    with pytest.raises(ValueError, match="local HTTP URL and disabled wiki writes"):
        create_app(settings, store, admin_preview=True)


def test_preview_requires_loopback_csrf_and_local_redirect(settings, store):
    client = create_app(settings, store, admin_preview=True).test_client()
    html = client.get("/").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    assert client.post("/preview", data={"mode": "admin"}).status_code == 400
    assert client.post("/preview", data={"csrf": csrf, "mode": "admin", "next": "//example.org"}).status_code == 400
    remote = {"REMOTE_ADDR": "192.0.2.10"}
    assert "Режим admin" not in client.get("/", environ_overrides=remote).get_data(as_text=True)
    assert client.post("/preview", data={"csrf": csrf, "mode": "admin"}, environ_overrides=remote).status_code == 404
