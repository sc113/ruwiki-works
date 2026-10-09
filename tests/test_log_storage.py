import json
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, insert, select, update

from toolforge_app.log_storage import (EXPIRED, SNAPSHOT_VERSION, maintain_storage,
                                      maintenance_tick, storage_usage)
from toolforge_app.processors.logs import public_run
from toolforge_app.statistics import statistics
from toolforge_app.storage import dump, runs, run_payloads, run_payload_links
from toolforge_app.web import create_app
from test_web import set_session


def decoded(store, run_id):
    row = store.run(run_id)
    return {**row, **{key: json.loads(row[key]) for key in ('events', 'summary', 'report')}}


def recorded(store, *, report=None, events=None, at=1000, status='success', dry=False, processor='maintenance-dates'):
    rid = store.start_run('daily', processor=processor, dry_run=dry)
    store.finish_run(rid, events or [], dict(checked=2, changed=1, errors=int(status == 'failed'), skipped=1),
                     report or {}, 'wiki snapshot', status=status)
    with store.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == rid).values(started_at=at, finished_at=at + 60))
    return rid


def snapshots(store):
    with store.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(run_payloads)).scalar_one()


def test_compressed_fields_are_shared_and_round_trip_exactly(store):
    rows = [dict(title='Статья ' + str(i), reason='Без изменений') for i in range(300)]
    report = dict(skipped_articles=rows, skipped=300, latest_check=1)
    first = recorded(store, report=report)
    second = recorded(store, report={**report, 'latest_check': 2})
    assert snapshots(store) == 2  # One shared list and one shared wiki text.
    assert decoded(store, first)['report'] == report
    assert decoded(store, second)['report'] == {**report, 'latest_check': 2}
    assert store.run(first)['table_text'] == 'wiki snapshot'
    with store.engine.connect() as conn:
        raw = conn.execute(select(runs.c.report).where(runs.c.id == first)).scalar_one()
        assert len(raw) < 200 and 'Статья' not in raw
        total = conn.execute(select(func.sum(func.length(run_payloads.c.data)))).scalar_one()
    assert total < len(dump(rows).encode('utf-8')) / 10


def test_expiry_preserves_public_results_statistics_and_other_state(settings, store):
    old = datetime(2026, 9, 30, 23, 59, 59, tzinfo=settings.zone).timestamp()
    edited = old + 2
    now = datetime(2027, 2, 1, tzinfo=settings.zone).timestamp()
    events = [dict(at=old, code='article', message='PRIVATE', title='A'),
        dict(at=edited, code='edited', title='A', message='PRIVATE', revision=123,
             changes=[dict(template='X', date='2026-10-01'), dict(template='Y')], diff_before='SECRET'),
        dict(at=edited + 1, code='article', message='PRIVATE', title='Unfinished')]
    rid = recorded(store, at=old, events=events, status='failed')
    other = recorded(store, at=old, events=[dict(at=edited, code='edited', title='A', revision=124, message='PRIVATE')],
                     processor='maintenance-rq')
    recorded(store, at=old, events=events, dry=True)
    before = statistics(store, settings.zone, '2026-10', now=now)
    public = public_run(decoded(store, rid))
    store.record_article_checks('translations-talk', [dict(title='A', checked_at=old, outcome='skipped', reason='none')])
    checks = store.checked_articles('translations-talk')
    store.set_state('sections-empty-to-fill:inventory', {'articles': ['A']})
    store.enqueue('page:sample', 'page', now + 900, title='Nomination', revision=2)
    queue = store.queue()
    result = maintain_storage(store, now=now)
    assert result == dict(expired=3, compressed=0, pending=0)
    after = decoded(store, rid)
    assert 'SECRET' not in dump(after) and 'PRIVATE' not in dump(after)
    assert after['events'] == public['events'] and after['status'] == 'failed'
    assert after['report'][EXPIRED] == now and after['table_text'] == ''
    assert public_run(after)['events'] == public['events']
    assert after['events'][0]['revision'] == 123
    assert after['events'][1]['code'] == 'unfinished'
    assert statistics(store, settings.zone, '2026-10', now=now) == before
    assert store.checked_articles('translations-talk') == checks
    assert store.get_state('sections-empty-to-fill:inventory') == {'articles': ['A']}
    assert store.queue() == queue
    assert maintain_storage(store, now=now) == dict(expired=0, compressed=0, pending=0)
    assert snapshots(store) == 0
    assert store.run(other)


def test_running_and_recent_runs_are_untouched_and_shared_snapshots_survive(store):
    now = 10000000
    rid = recorded(store, at=now - 90 * 86400 - 60)  # Exact retention boundary.
    old = recorded(store, at=1000)
    running = store.start_run('full')
    store.update_progress(running, [dict(at=1000, code='page', title='A', message='live')])
    active = store.run(running)
    full = store.run(rid)
    maintain_storage(store, now=now)
    assert store.run(rid) == full and store.run(running) == active
    assert decoded(store, old)['report'][EXPIRED] == now
    assert snapshots(store) == 1 and store.run(rid)['table_text'] == 'wiki snapshot'


def test_legacy_backfill_is_bounded_restartable_and_handles_unicode(store):
    ids = [recorded(store, report=dict(items=['Номинация'] * 1000), at=10000 + i) for i in range(3)]
    for rid in ids:
        with store.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == rid).values(report=dump({'items': ['Номинация'] * 1000}), table_text='legacy'))
    before = [store.run(rid) for rid in ids]
    assert maintain_storage(store, now=20000, batch_size=2) == dict(expired=0, compressed=2, pending=1)
    assert maintain_storage(store, now=20000, batch_size=2) == dict(expired=0, compressed=1, pending=0)
    assert [store.run(rid) for rid in ids] == before
    assert snapshots(store) == 2


def test_failed_conversion_rolls_back_and_retries_without_touching_runs(store):
    rid = recorded(store)
    original = store.run(rid)
    def fail(connection, cursor, statement, params, context, many):
        if statement.startswith('UPDATE runs'):
            raise RuntimeError('simulated storage error')
    event.listen(store.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError):
            maintain_storage(store, now=10000000)
    finally:
        event.remove(store.engine, 'before_cursor_execute', fail)
    assert store.run(rid) == original and snapshots(store) == 1
    assert maintain_storage(store, now=10000000)['expired'] == 1


def test_light_history_never_fetches_diagnostics_or_snapshots_but_live_counts_remain(store):
    rid = recorded(store, report=dict(items=['PRIVATE'] * 3000, problems=2),
                   events=[dict(at=1000, code='error', title='A', message='PRIVATE')])
    active = store.start_run('daily', processor='maintenance-dates')
    store.update_progress(active, [dict(at=2000, code='unchanged', message='live', title='A')])
    statements = []
    def collect(connection, cursor, statement, params, context, many):
        statements.append(statement)
    event.listen(store.engine, 'before_cursor_execute', collect)
    try:
        rows = store.list_runs(processor=None, details=False)
    finally:
        event.remove(store.engine, 'before_cursor_execute', collect)
    archived = next(row for row in rows if row['id'] == rid)
    assert json.loads(archived['report']) == {'problems': 2, '_public_events': True}
    assert archived['events'] == '[]' and archived['table_text'] == ''
    assert next(row for row in rows if row['id'] == active)['events'] != '[]'
    assert all('run_payloads' not in sql and 'runs.table_text' not in sql and ', runs.report,' not in sql for sql in statements)
    assert 'PRIVATE' in store.list_runs(processor=None, details=False, include_events=True)[1]['events']


def test_expired_exports_are_clear_and_public_archive_is_still_accessible(settings, store):
    rid = recorded(store, processor='obkat', events=[dict(at=1000, code='edited', title='A', revision=123, message='private')])
    maintain_storage(store, now=10000000)
    client = create_app(settings, store).test_client()
    assert client.get('/runs/' + rid).status_code == 200
    text = client.get('/runs/' + rid).get_data(as_text=True)
    assert 'diff=123' in text and 'Краткий архив' in text
    assert client.get('/reports/obkat.json?run=' + rid).status_code == 410
    assert client.get('/runs/' + rid + '/table.wiki').status_code == 410
    set_session(client, 'admin')
    assert 'удалены по сроку хранения' in client.get('/runs/' + rid).get_data(as_text=True)
    assert client.get('/runs/' + rid + '/log.txt').status_code == 200


def test_background_maintenance_is_rate_limited_and_recovers_errors(settings, store):
    with patch('toolforge_app.log_storage.maintain_storage', side_effect=RuntimeError('PRIVATE')) as failure:
        maintenance_tick(settings, store, now=10000)
        maintenance_tick(settings, store, now=10001)
        assert failure.call_count == 1 and store.get_state('storage:maintenance_error')['code'] == 'internal'
    maintenance_tick(settings, store, now=13601)
    assert not store.get_state('storage:maintenance_error')
    assert store.get_state('storage:maintenance')['retention_days'] == 90
    assert storage_usage(store)['runs'] == 0


@pytest.mark.parametrize('days,batch', [(0, 100), (90, 0), (3651, 100), (90, 1001)])
def test_invalid_retention_limits_are_rejected(store, days, batch):
    with pytest.raises(ValueError):
        maintain_storage(store, retention_days=days, batch_size=batch)
