import time
from unittest.mock import patch

import pytest

from toolforge_app.processors.maintenance.config import defaults
from toolforge_app.web import create_app
from toolforge_app.overview import build_overview
from test_web import set_session


def test_discussions_share_group_but_keep_distinct_pages_and_admin_controls(settings, store):
    overview = build_overview(settings, store)
    group = overview['groups'][0]
    assert group['spec'].slug == 'discussions'
    assert group['spec'].title == 'Обсуждения и номинации'
    assert [card['task'].slug for card in group['cards']] == ['obkat', 'projects']
    assert [card['display_title'] for card in group['cards']] == ['Категории', 'Проекты']
    all_tasks = [card['task'].slug for group in overview['groups'] for card in group['cards']]
    assert len(all_tasks) == len(set(all_tasks)) == 11
    client = create_app(settings, store).test_client()
    home = client.get('/').get_data(as_text=True)
    section = home.split('aria-labelledby="group-discussions"', 1)[1].split('</section>', 1)[0]
    assert '/tasks/obkat' in section and '/tasks/projects' in section
    assert '2 задачи' in section and 'Поиск каждые 12 ч' in section
    assert '/processors/discussions' not in home
    for slug, title in [('obkat', 'Категории'), ('projects', 'Проекты')]:
        html = client.get('/processors/' + slug).get_data(as_text=True)
        navigation = html.split('class="processor-group-links"', 1)[1].split('</nav>', 1)[0]
        assert f'href="/processors/{slug}" class="processor-link selected"' in navigation
        assert f'<span>{title}</span>' in navigation
        mobile_group = html.split('<optgroup label="Обсуждения и номинации">', 1)[1].split('</optgroup>', 1)[0]
        assert 'value="/processors/obkat"' in mobile_group and 'value="/processors/projects"' in mobile_group
    set_session(client, settings.admin_username)
    admin = client.get('/').get_data(as_text=True)
    section = admin.split('aria-labelledby="group-discussions"', 1)[1].split('</section>', 1)[0]
    assert '/admin/tasks/obkat/control' in section and '/admin/tasks/projects/control' in section


def populate(store, slug, count=45):
    config = defaults(slug)
    key = {"maintenance-dates": "meta_category", "maintenance-rq": "rq_category", "maintenance-rq-unwrap": "target_category"}[slug]
    source = config[key]
    titles = [f"Статья {number:02}" for number in range(count)]
    category = "Категория:Непустая"
    store.set_state(slug + ":inventory", {"source": source, "checked_at": time.time(), "total": count,
        "articles": {title: [category] for title in titles}, "categories": [
            {"title": category, "count": count, "articles": titles},
            {"title": "Категория:Пустая", "count": 0, "articles": []}]})
    store.set_state(slug + ":result", {"source": source, "at": time.time(), "manual": [
        {"title": titles[0], "reason": "Нужна ручная проверка"}]})
    return titles


@pytest.mark.parametrize("slug", ["maintenance-dates", "maintenance-rq", "maintenance-rq-unwrap"])
def test_pending_metric_opens_articles_not_manual_errors_and_pagination_keeps_view(settings, store, slug):
    titles = populate(store, slug)
    client = create_app(settings, store).test_client()
    home = client.get("/").get_data(as_text=True)
    assert f'task={slug}&amp;view=pending#reports' in home
    first = client.get(f"/processors/maintenance?task={slug}&view=pending").get_data(as_text=True)
    assert "Статьи к обработке" in first and titles[39] in first and titles[40] not in first
    assert "Нужна ручная проверка" not in first
    assert f'task={slug}&amp;view=pending&amp;page=2#reports' in first
    second = client.get(f"/processors/maintenance?task={slug}&view=pending&page=2").get_data(as_text=True)
    assert titles[40] in second and titles[0] not in second
    problems = client.get(f"/processors/maintenance?task={slug}&view=problems").get_data(as_text=True)
    assert "Нужна ручная проверка" in problems and titles[1] not in problems


def test_log_tab_is_scoped_to_current_action_and_run_backlink_opens_log(settings, store):
    first = store.start_run("daily", processor="maintenance-dates")
    store.finish_run(first, [], {}, {})
    other = store.start_run("daily", processor="maintenance-rq")
    store.finish_run(other, [], {}, {})
    client = create_app(settings, store).test_client()
    log = client.get("/processors/maintenance?task=maintenance-dates&view=logs").get_data(as_text=True)
    assert f'/runs/{first}' in log and f'/runs/{other}' not in log
    assert 'class="pending-articles"' not in log and '<section id="history"' in log
    detail = client.get('/runs/' + first).get_data(as_text=True)
    assert '/runs?task=maintenance-dates' in detail


def test_countdown_uses_schedule_and_disappears_when_action_is_paused(settings, store):
    client = create_app(settings, store).test_client()
    with patch('toolforge_app.web.time.time', return_value=1000):
        app = client.application
        assert app.jinja_env.filters['countdown'](4660) == 'через 1 ч 1 мин'
        assert app.jinja_env.filters['countdown'](1030) == 'менее минуты'
        assert app.jinja_env.filters['countdown'](999) == 'Запуск ожидается'
    live = client.get('/processors/maintenance?task=maintenance-rq').get_data(as_text=True)
    assert 'data-countdown=' in live and 'data-server-now=' in live
    store.change_control('maintenance-rq', 'pause', 'admin')
    paused = client.get('/processors/maintenance?task=maintenance-rq').get_data(as_text=True)
    assert 'data-countdown=' not in paused and 'На паузе' in paused
