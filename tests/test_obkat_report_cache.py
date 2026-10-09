from unittest.mock import Mock

import pytest
from sqlalchemy import event

from toolforge_app.processors.obkat import service
from toolforge_app.storage import Store, dump


def save_problem(store, title, month, at=100):
    store.save_page(title, month=month, revision=10, checked_at=at,
        text='== День ==\n=== Итог ===\n',
        issues=dump([dict(type='wrong_itog_level', title='Итог', line=2, level=3)]))


def test_cached_report_avoids_reloading_text_and_cannot_be_mutated_by_a_view(settings, store, wiki, monkeypatch):
    save_problem(store, wiki.title, settings.start_month)
    build = Mock(wraps=service.build_report)
    reads = Mock(wraps=store.all_pages)
    monkeypatch.setattr(service, 'build_report', build)
    monkeypatch.setattr(store, 'all_pages', reads)
    first = service.report(store)
    assert first['problems'] == 1
    assert first['items'][0]['problem_history'] == dict(first_seen=100, last_seen=100, episodes=1)
    first['items'].clear()
    second = service.report(store)
    assert len(second['items']) == 1
    assert build.call_count == reads.call_count == 1


def test_another_process_invalidates_report_even_if_revision_and_time_are_identical(settings, store, wiki):
    save_problem(store, wiki.title, settings.start_month)
    assert service.report(store)['problems'] == 1
    another = Store(settings.database_url)
    try:
        another.save_page(wiki.title, text='No headings', issues='[]')
        assert service.report(store)['problems'] == 0
        assert store.page(wiki.title)['checked_at'] == 100
    finally:
        another.engine.dispose()


def test_write_during_report_build_invalidates_the_next_request(settings, store, wiki, monkeypatch):
    save_problem(store, wiki.title, settings.start_month)
    another = Store(settings.database_url)
    original = service.build_report

    def build(rows):
        another.save_page(wiki.title, text='No headings', issues='[]', checked_at=200)
        monkeypatch.setattr(service, 'build_report', original)
        return original(rows)

    monkeypatch.setattr(service, 'build_report', build)
    try:
        assert service.report(store)['problems'] == 1
        assert service.report(store)['problems'] == 0
    finally:
        another.engine.dispose()


def test_failed_invalidation_rolls_back_page_and_keeps_cached_report(settings, store, wiki):
    save_problem(store, wiki.title, settings.start_month)
    previous = service.report(store)
    version = store.get_state('obkat:pages_version')

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE state'):
            raise RuntimeError('Database write failed')

    event.listen(store.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError):
            store.save_page(wiki.title, text='No headings', issues='[]')
    finally:
        event.remove(store.engine, 'before_cursor_execute', fail)
    assert store.get_state('obkat:pages_version') == version
    assert store.page(wiki.title)['issues'] != '[]'
    assert service.report(store) == previous
