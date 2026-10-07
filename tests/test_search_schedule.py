import time
from datetime import datetime

from toolforge_app.execution import ready_jobs
from toolforge_app.processors.sections import TASK_SLUGS as SECTIONS
from toolforge_app.processors.sections.service import SectionWorker, SectionMonitor
from toolforge_app.processors.translations import TASK_SLUGS as TRANSLATIONS
from toolforge_app.processors.translations.service import TranslationWorker, TranslationMonitor
from toolforge_app.schedules import get_schedule, save_schedule
from toolforge_app.web import create_app
from test_sections import SectionWiki
from test_translations import TranslationWiki
from test_web import set_session


def test_search_frequency_is_editable_and_monitor_uses_it_without_restart(settings, store):
    slug = TRANSLATIONS[0]
    wiki = TranslationWiki()
    monitor = TranslationMonitor(settings, store, wiki)
    signals = []
    monitor.outer_renew = lambda: signals.append(1)
    monitor.tick(1000)
    assert len(signals) >= 5
    save_schedule(settings, store, slug, {'search_minutes': '10'}, 'admin', partial=True)
    calls = len(wiki.calls)
    monitor.tick(1599)
    assert len(wiki.calls) == calls
    monitor.tick(1600)
    assert len(wiki.calls) > calls and store.get_state(slug + ':inventory')['checked_at'] == 1600


def test_regular_section_search_fetches_new_titles_despite_long_article_cache(settings, store):
    slug = SECTIONS[0]
    store.patch_state(slug + ':config', {'articles_cache_days': 30})
    wiki = SectionWiki()
    monitor = SectionMonitor(settings, store, wiki)
    monitor.tick(1000)
    original = len([call for call in wiki.calls if call[0] == 'inventory'])
    monitor.tick(22599)
    assert len([call for call in wiki.calls if call[0] == 'inventory']) == original
    monitor.tick(22600)
    assert len([call for call in wiki.calls if call[0] == 'inventory']) > original
    assert store.get_state(slug + ':inventory')['checked_at'] == 22600


def test_four_oclock_actions_share_deadline_and_follow_website_order(settings, store):
    now = datetime(2026, 10, 7, 3, 59, tzinfo=settings.zone).timestamp()
    for slug in (*TRANSLATIONS, *SECTIONS):
        worker = TranslationWorker if slug in TRANSLATIONS else SectionWorker
        worker(settings, store, slug).schedule(now)
    assert {job['due_at'] for job in store.queue(None)} == {now + 60}
    assert [job['processor'] for job in ready_jobs(store, now + 61)] == [*TRANSLATIONS, *SECTIONS]


def test_search_timer_round_trips_through_shared_form_and_index(settings, store):
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    response = client.post('/admin/tasks/obkat/schedule', data={
        'csrf': 'csrf-test', 'search_minutes': '5', 'quiet_minutes': '15', 'month_end_time': '23:30'})
    assert response.status_code == 302
    assert get_schedule(settings, store, 'obkat')['search_minutes'] == 5
    for url in ('/', '/processors/obkat'):
        html = client.get(url).get_data(as_text=True)
        assert 'Поиск' in html and '15' in html
    assert 'name="search_minutes" value="5"' in client.get('/processors/obkat').get_data(as_text=True)
