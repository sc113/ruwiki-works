"""Compressed shared snapshots and bounded diagnostic retention.

Run records and safe page outcomes are permanent. Current inventories, skip
marks, issue history, queues and credentials are outside this maintenance.
"""
import hashlib
import json
import time
import zlib

from sqlalchemy import LargeBinary, cast, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from .storage import dump, runs, run_payloads, run_payload_links
from .run_status import normalize_run

SNAPSHOT_VERSION = '_snapshot_version'
EXPIRED = '_details_expired_at'


def payload(conn, raw):
    key = hashlib.sha256(raw).hexdigest()
    if not conn.execute(select(run_payloads.c.key).where(run_payloads.c.key == key).with_for_update()).first():
        try:
            with conn.begin_nested():
                conn.execute(insert(run_payloads).values(key=key, data=zlib.compress(raw), raw_bytes=len(raw)))
        except IntegrityError:
            if not conn.execute(select(run_payloads.c.key).where(run_payloads.c.key == key)).first():
                raise
    return key


def pack_run(conn, run_id, report, table_text):
    """Large report fields are immutable, compressed and shared by content hash."""
    report = dict(report)
    conn.execute(delete(run_payload_links).where(run_payload_links.c.run_id == run_id))
    for field, value in list(report.items()):
        if not isinstance(value, (dict, list)):
            continue
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(raw) < 4096:
            continue
        conn.execute(insert(run_payload_links).values(run_id=run_id, field='report:' + field,
            payload_key=payload(conn, raw)))
        del report[field]
    if table_text:
        conn.execute(insert(run_payload_links).values(run_id=run_id, field='table_text',
            payload_key=payload(conn, table_text.encode('utf-8'))))
        table_text = ''
    report[SNAPSHOT_VERSION] = 1
    return report, table_text


def pack_diagnostics(conn, run):
    """Keep safe results inline; detailed completed logs are loaded on demand."""
    events = json.loads(run['events'])
    if not events:
        return run['events'], False
    conn.execute(insert(run_payload_links).values(run_id=run['id'], field='events',
        payload_key=payload(conn, dump(events).encode('utf-8'))))
    return dump(archive_events(run)), True


def hydrate_run(conn, row, *, reports=True):
    row = dict(row)
    report = json.loads(row['report'])
    if report.pop(SNAPSHOT_VERSION, None) or not reports and report.get('_public_events'):
        statement = select(run_payload_links.c.field, run_payloads.c.data).join(
            run_payloads, run_payloads.c.key == run_payload_links.c.payload_key).where(
                run_payload_links.c.run_id == row['id'])
        if not reports:
            statement = statement.where(run_payload_links.c.field == 'events')
        blobs = conn.execute(statement).all()
        for field, data in blobs:
            value = zlib.decompress(data).decode('utf-8')
            if field == 'events':
                row['events'] = value
                report.pop('_public_events', None)
            elif field == 'table_text':
                row['table_text'] = value
            else:
                report[field.removeprefix('report:')] = json.loads(value)
    if reports:
        report.pop('_public_events', None)
    row['report'] = dump(report)
    return row


def archive_events(run):
    """Keep the exact public page projection, including saved edit identifiers."""
    from .processors.logs import public_run
    decoded = {**run, **{key: json.loads(run[key]) for key in ('events', 'summary', 'report')}}
    return public_run(decoded)['events']


def maintain_storage(store, *, now=None, retention_days=90, batch_size=100):
    """Convert a bounded batch; per-run transactions make interruption harmless."""
    if not 1 <= retention_days <= 3650 or not 1 <= batch_size <= 1000:
        raise ValueError('Invalid storage maintenance limits')
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    closed = (runs.c.status != 'running') & runs.c.finished_at.is_not(None)
    expired = closed & (runs.c.finished_at < cutoff) & func.json_extract(runs.c.report, '$.' + EXPIRED).is_(None)
    legacy = closed & func.json_extract(runs.c.report, '$.' + SNAPSHOT_VERSION).is_(None) & func.json_extract(runs.c.report, '$.' + EXPIRED).is_(None)
    # Backfill recent snapshots after processing the oldest expired diagnostics.
    with store.engine.connect() as conn:
        ids = list(conn.execute(select(runs.c.id).where(expired | legacy).order_by(
            runs.c.finished_at, runs.c.id).limit(batch_size)).scalars())
    result = dict(expired=0, compressed=0)
    for run_id in ids:
        with store.engine.begin() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id).with_for_update()).mappings().first()
            if not row or row['status'] == 'running' or row['finished_at'] is None:
                continue
            report = json.loads(row['report'])
            if EXPIRED in report:
                continue
            if row['finished_at'] < cutoff:
                run = normalize_run(row)
                # Freeze classification and metrics before dropping the snapshot.
                summary = json.loads(run['summary'])
                for field in ('problems', 'skipped'):
                    if type(report.get(field)) is int:
                        summary.setdefault('report_' + field, report[field])
                short_report = {key: value for key, value in report.items()
                                if type(value) in (int, float, bool) and not key.startswith('_')}
                short_report[EXPIRED] = now
                conn.execute(update(runs).where(runs.c.id == run_id).values(
                    status=run['status'], summary=dump(summary), events=dump(archive_events(run)),
                    report=dump(short_report), table_text=''))
                conn.execute(delete(run_payload_links).where(run_payload_links.c.run_id == run_id))
                result['expired'] += 1
            elif not report.get(SNAPSHOT_VERSION):
                run = normalize_run(row)
                packed, table = pack_run(conn, run_id, report, row['table_text'])
                events, packed['_public_events'] = pack_diagnostics(conn, run)
                conn.execute(update(runs).where(runs.c.id == run_id).values(
                    report=dump(packed), table_text=table, events=events, status=run['status']))
                result['compressed'] += 1
    with store.engine.begin() as conn:
        # Lock candidates and recheck current references: a finishing run can
        # reuse a snapshot that an older run has just released.
        unused = list(conn.execute(select(run_payloads.c.key).where(
            run_payloads.c.key.not_in(select(run_payload_links.c.payload_key))).limit(1000).with_for_update()).scalars())
        for key in unused:
            if not conn.execute(select(run_payload_links.c.run_id).where(
                    run_payload_links.c.payload_key == key).with_for_update()).first():
                conn.execute(delete(run_payloads).where(run_payloads.c.key == key))
        result['pending'] = conn.execute(select(func.count()).select_from(runs).where(expired | legacy)).scalar_one()
    return result


def storage_usage(store):
    """Payload sizes are measured separately from allocated database space."""
    from sqlalchemy import text
    with store.engine.connect() as conn:
        size = func.octet_length if store.engine.dialect.name == 'mysql' else lambda column: func.length(cast(column, LargeBinary))
        row = conn.execute(select(func.count().label('runs'),
            func.coalesce(func.sum(size(runs.c.events)), 0).label('events_bytes'),
            func.coalesce(func.sum(size(runs.c.report) + size(runs.c.table_text)), 0).label('report_bytes'))).mappings().one()
        result = dict(row)
        result['snapshot_bytes'] = conn.execute(select(func.coalesce(func.sum(size(run_payloads.c.data)), 0))).scalar_one()
        if store.engine.dialect.name == 'mysql':
            result['database_bytes'] = int(conn.execute(text('SELECT COALESCE(SUM(data_length + index_length), 0) '
                'FROM information_schema.tables WHERE table_schema = DATABASE()')).scalar_one())
        else:
            result['database_bytes'] = conn.exec_driver_sql('PRAGMA page_count').scalar_one() * conn.exec_driver_sql('PRAGMA page_size').scalar_one()
        return {key: int(value) for key, value in result.items()}


def maintenance_tick(settings, store, now=None):
    """One background process, hourly checks and bounded catch-up batches."""
    now = time.time() if now is None else now
    previous = store.get_state('storage:maintenance', {})
    interval = 30 if previous.get('pending') else 3600
    if previous.get('checked_at') and now - previous['checked_at'] < interval:
        return
    with store.worker_lease(processor='storage-maintenance') as renew:
        if not renew:
            return
        try:
            result = maintain_storage(store, now=now, retention_days=settings.log_retention_days)
            renew()
            result.update(storage_usage(store), checked_at=now, retention_days=settings.log_retention_days)
            store.set_state('storage:maintenance', result)
            store.set_state('storage:maintenance_error', None)
        except Exception as exc:
            # Diagnostic upkeep must not stop bot execution or disclose SQL credentials.
            store.set_state('storage:maintenance', {**previous, 'checked_at': now})
            store.set_state('storage:maintenance_error', dict(at=now, code='internal'))
            import sys
            print('Storage maintenance failed: ' + type(exc).__name__, file=sys.stderr)
