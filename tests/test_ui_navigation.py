import time
from unittest.mock import patch

import pytest

from toolforge_app.processors.maintenance.config import defaults
from toolforge_app.web import create_app
from toolforge_app.overview import build_overview
from test_web import set_session


def test_discussions_use_one_menu_entry_and_two_action_tabs(settings, store):
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
    assert '2 действия' in section and 'Поиск 00:00 и 12:00 МСК' in section
    assert 'href="/processors/discussions"' in section
    for slug, title in [('obkat', 'Категории'), ('projects', 'Проекты')]:
        html = client.get('/processors/discussions?task=' + slug).get_data(as_text=True)
        assert '<h1>Обсуждения и номинации</h1>' in html
        navigation = html.split('<nav aria-label="Задачи бота"', 1)[1].split('</nav>', 1)[0]
        assert 'href="/processors/discussions" class="processor-link selected"' in navigation
        assert '/processors/obkat' not in navigation and '/processors/projects' not in navigation
        assert 'processor-group' not in navigation
        tabs = html.split('<nav class="action-tabs"', 1)[1].split('</nav>', 1)[0]
        assert f'class="active" href="/processors/discussions?task={slug}"' in tabs
        assert '>Категории</a>' in tabs and '>Проекты</a>' in tabs
        mobile = html.split('<select data-section-nav', 1)[1].split('</select>', 1)[0]
        assert 'value="/processors/discussions" selected' in mobile and '<optgroup' not in mobile
        assert f'<input type="hidden" name="task" value="{slug}">' in html
        assert f'data-refresh-url="/tasks/{slug}/status-fragment"' in html
        assert client.get('/processors/' + slug).status_code == 200
    set_session(client, settings.admin_username)
    admin = client.get('/').get_data(as_text=True)
    section = admin.split('aria-labelledby="group-discussions"', 1)[1].split('</section>', 1)[0]
    assert '/admin/tasks/obkat/control' in section and '/admin/tasks/projects/control' in section


def test_discussion_tabs_keep_logs_settings_and_control_separate(settings, store):
    runs = {}
    for slug in ['obkat', 'projects']:
        runs[slug] = store.start_run('full', processor=slug)
        store.finish_run(runs[slug], [], {}, {})
    client = create_app(settings, store).test_client()
    assert client.get('/processors/discussions').status_code == 200
    for slug, other in [('obkat', 'projects'), ('projects', 'obkat')]:
        html = client.get('/processors/discussions?task=' + slug + '&view=logs').get_data(as_text=True)
        assert '/runs/' + runs[slug] in html and '/runs/' + runs[other] not in html
        redirect = client.get('/tasks/' + slug)
        assert redirect.location.endswith('/processors/discussions?task=' + slug)
    assert client.get('/processors/discussions?task=maintenance-rq').status_code == 404
    set_session(client, settings.admin_username)
    for slug, other in [('obkat', 'projects'), ('projects', 'obkat')]:
        html = client.get('/processors/discussions?task=' + slug).get_data(as_text=True)
        assert f'/admin/tasks/{slug}/schedule' in html and f'/admin/tasks/{other}/schedule' not in html
    response = client.post('/admin/tasks/projects/control', data={
        'csrf': 'csrf-test', 'action': 'pause', 'return_to': 'processor'})
    assert response.location.endswith('/processors/discussions?task=projects')
    assert store.control('projects')['mode'] == 'paused' and store.control('obkat')['mode'] == 'active'


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
