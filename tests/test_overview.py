from datetime import datetime

from toolforge_app.overview import build_overview, next_month_end
from toolforge_app.storage import dump
from toolforge_app.web import create_app


def test_index_is_overview_and_import_is_not_presented_as_a_bot_run(settings, store, wiki):
    store.save_page(wiki.title, month=settings.start_month, checked_at=1, text="",
        issues=dump([{"type": "wrong_itog_level", "title": "Итог", "line": 1},
                     {"type": "no_itog", "title": "Пример", "line": 2}]), origin="import")
    run_id = store.start_run("import", "local")
    store.finish_run(run_id, [], {}, {})
    overview = build_overview(settings, store)
    assert overview["total_problems"] == 1
    assert len(overview["cards"]) == 10 and overview["enabled_count"] == 10
    assert overview["cards"][0]["last_run"] is None
    assert overview["cards"][0]["snapshot"]["id"] == run_id
    response = create_app(settings, store).test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Обзор" in html
    assert "Не запускалась" in html
    assert "Даты в шаблонах о проблемах" in html and "Замена параметров RQ" in html
    assert '/tasks/maintenance-dates' in html and '/tasks/maintenance-rq' in html


def test_task_links_lead_to_distinct_task_pages(settings, store):
    client = create_app(settings, store).test_client()
    for slug, title in [("maintenance-dates", "Даты в шаблонах о проблемах"),
                        ("maintenance-rq", "Замена параметров RQ"),
                        ("maintenance-rq-unwrap", "Разворачивание одиночного RQ")]:
        response = client.get("/tasks/" + slug, follow_redirects=True)
        assert response.status_code == 200
        assert title in response.get_data(as_text=True)
    assert client.get("/tasks/nonexistent").status_code == 404


def test_overview_distinguishes_success_error_and_control_states(settings, store):
    run_id = store.start_run("full")
    store.finish_run(run_id, [], {}, {})
    assert build_overview(settings, store)["cards"][0]["status"] == "success"
    store.set_state("worker_error", {"code": "network", "at": 1})
    assert build_overview(settings, store)["cards"][0]["status"] == "error"
    store.change_control("obkat", "pause", "admin")
    assert build_overview(settings, store)["cards"][0]["status"] == "paused"
    store.change_control("obkat", "stop", "admin")
    assert build_overview(settings, store)["cards"][0]["status"] == "stopped"


def test_overview_uses_actual_last_run_and_earliest_pending_job(settings, store, wiki):
    run_id = store.start_run("full", "admin")
    store.finish_run(run_id, [], {}, {})
    store.enqueue("manual:normal", "full", 2000)
    store.enqueue("page:example", "page", 1000, title=wiki.title)
    card = build_overview(settings, store, now=500)["cards"][0]
    assert card["last_run"]["id"] == run_id
    assert card["next_job"]["due_at"] == 1000
    assert card["queued"] == 2


def test_empty_event_queue_does_not_invent_a_next_edit_time(settings, store):
    card = build_overview(settings, store)["cards"][0]
    assert card["next_job"] is None
    assert card["status"] == "offline"
    assert card["month_end"] > 0


def test_running_job_is_not_shown_as_next_but_a_newer_edit_is(settings, store, wiki):
    from toolforge_app.worker import Worker
    store.enqueue("page:example", "page", 1000, title=wiki.title, revision=10)
    worker = Worker(settings, store, wiki)
    original_refresh = worker.refresh_table

    def check_overview_during_run():
        card = build_overview(settings, store, now=1000)["cards"][0]
        assert card["status"] == "running"
        assert card["next_job"] is None
        assert card["queued"] == 0
        store.enqueue("page:example", "page", 2200, title=wiki.title, revision=11)
        card = build_overview(settings, store, now=1000)["cards"][0]
        assert card["next_job"]["revision"] == 11
        assert card["queued"] == 1
        return original_refresh()

    worker.refresh_table = check_overview_during_run
    worker.execute(store.queue()[0])
    assert store.list_runs()[0]["status"] == "success"


def test_month_end_predicts_moscow_time_and_handles_year_rollover(settings):
    before = datetime(2026, 10, 31, 23, 29, tzinfo=settings.zone).timestamp()
    expected = datetime(2026, 10, 31, 23, 30, tzinfo=settings.zone).timestamp()
    assert next_month_end(settings, before) == expected
    december = datetime(2026, 12, 31, 23, 31, tzinfo=settings.zone).timestamp()
    assert next_month_end(settings, december) == datetime(2027, 1, 31, 23, 30, tzinfo=settings.zone).timestamp()


def test_overview_separates_live_search_from_disabled_automatic_execution(settings, store, wiki):
    import time
    now = time.time()
    store.set_state('service:executor', {'enabled': False})
    store.set_state('monitor_heartbeat', now)
    store.set_state('last_poll', now - 100)
    store.enqueue('page:example', 'page', now - 1000, title=wiki.title)
    card = build_overview(settings, store, now)['cards'][0]
    assert card['search_online'] and card['search_status'] == 'Поиск работает'
    assert card['next_search'] == now + 200
    assert card['status_label'] == 'Обработка выключена' and not card['automatic_enabled']
    client = create_app(settings, store).test_client()
    for path in ('/', '/processors/obkat'):
        html = client.get(path).get_data(as_text=True)
        assert 'Готова к обработке' in html and 'Автозапуск выключен' in html
        assert 'Поиск работает' in html
        assert ('Следующая проверка:' if path == '/' else 'следующая в') in html


def test_overdue_enabled_run_shows_queue_instead_of_yesterdays_date(settings, store):
    import time
    store.enqueue('page:example', 'page', time.time() - 86400)
    html = create_app(settings, store).test_client().get('/').get_data(as_text=True)
    assert 'В очереди' in html and 'Автозапуск выключен' not in html
