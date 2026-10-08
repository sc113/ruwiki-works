"""Durable pages, jobs and role-filtered logs. SQLite locally; MariaDB on Toolforge."""
import json
import hashlib
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (Boolean, Column, Float, Integer, MetaData, String, Table, or_,
                        Text, create_engine, delete, insert, select, update)
from sqlalchemy.dialects.mysql import DOUBLE, LONGTEXT
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
large_text = Text().with_variant(LONGTEXT(), "mysql")
epoch_time = Float().with_variant(DOUBLE(asdecimal=False), "mysql")
pages = Table("pages", metadata,
    Column("title", String(255), primary_key=True),
    Column("processor", String(32), nullable=False),
    Column("month", String(7), nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("edited_at", epoch_time, nullable=False, default=0),
    Column("checked_at", epoch_time, nullable=False, default=0),
    Column("text", large_text, nullable=False, default=""),
    Column("issues", large_text, nullable=False, default="[]"),
    Column("missing", Boolean, nullable=False, default=False),
    Column("origin", String(32), nullable=False, default="wiki"))
jobs = Table("jobs", metadata,
    Column("key", String(255), primary_key=True),
    Column("processor", String(32), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("title", String(255), nullable=False, default=""),
    Column("revision", Integer, nullable=False, default=0),
    Column("due_at", epoch_time, nullable=False),
    Column("spacing", Boolean, nullable=False, default=False),
    Column("requested_by", String(255), nullable=False, default="worker"),
    Column("attempts", Integer, nullable=False, default=0))
runs = Table("runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("processor", String(32), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("requested_by", String(255), nullable=False),
    Column("started_at", epoch_time, nullable=False),
    Column("finished_at", epoch_time),
    Column("status", String(32), nullable=False),
    Column("dry_run", Boolean, nullable=False),
    Column("spacing", Boolean, nullable=False),
    Column("events", large_text, nullable=False, default="[]"),
    Column("summary", large_text, nullable=False, default="{}"),
    Column("report", large_text, nullable=False, default="{}"),
    Column("table_text", large_text, nullable=False, default=""))
state = Table("state", metadata,
    Column("key", String(80), primary_key=True),
    Column("value", large_text, nullable=False))
connection_secrets = Table("connection_secrets", metadata,
    Column("name", String(32), primary_key=True),
    Column("encrypted", Text, nullable=False))
run_requests = Table("run_requests", metadata,
    Column("id", String(32), primary_key=True),
    Column("processor", String(32), nullable=False),
    Column("job_key", String(255), nullable=False, unique=True),
    Column("requested_at", epoch_time, nullable=False),
    Column("run_id", String(32)))
article_checks = Table("article_checks", metadata,
    Column("processor", String(32), primary_key=True),
    # Hash titles so ToolsDB's default case-insensitive collation cannot merge them.
    Column("title_key", String(64), primary_key=True),
    Column("title", String(255), nullable=False),
    Column("checked_at", epoch_time, nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("reason", large_text, nullable=False, default=""),
    Column("run_id", String(32), nullable=False, default=""))
leases = Table("leases", metadata,
    Column("key", String(80), primary_key=True),
    Column("owner", String(32), nullable=False),
    Column("expires_at", epoch_time, nullable=False))
problem_history = Table("problem_history", metadata,
    Column("processor", String(32), primary_key=True),
    Column("issue_key", String(64), primary_key=True),
    Column("scope", String(255), nullable=False),
    Column("first_seen", epoch_time, nullable=False),
    Column("last_seen", epoch_time, nullable=False),
    Column("resolved_at", epoch_time),
    Column("episodes", Integer, nullable=False, default=1))
notifications = Table("notifications", metadata,
    Column("id", String(32), primary_key=True),
    Column("active_key", String(80), unique=True),
    Column("title", Text, nullable=False),
    Column("message", large_text, nullable=False),
    Column("target", String(255), nullable=False),
    Column("opened_at", epoch_time, nullable=False),
    Column("resolved_at", epoch_time),
    Column("read_at", epoch_time))


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, url):
        if url.startswith("sqlite:///var/"):
            from .config import ROOT
            (ROOT / "var").mkdir(exist_ok=True)
            url = "sqlite:///" + (ROOT / url.removeprefix("sqlite:///")).as_posix()
        self.engine = create_engine(url, pool_pre_ping=True,
            connect_args={"timeout": 30} if url.startswith("sqlite") else {})
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if not conn.execute(select(state.c.key).where(state.c.key == 'wiki:api_backoff')).first():
                try:
                    with conn.begin_nested():
                        conn.execute(insert(state).values(key='wiki:api_backoff', value=dump({'until': 0})))
                except IntegrityError:
                    pass
            if not conn.execute(select(leases).where(leases.c.key == "obkat")).first():
                try:
                    with conn.begin_nested():
                        conn.execute(insert(leases).values(key="obkat", owner="", expires_at=0))
                except IntegrityError:
                    pass
            if not conn.execute(select(state.c.key).where(state.c.key == "control:obkat")).first():
                try:
                    with conn.begin_nested():
                        conn.execute(insert(state).values(key="control:obkat", value=dump(
                            {"mode": "active", "generation": 0, "at": None, "by": ""})))
                except IntegrityError:
                    pass

    def get_state(self, key, default=None):
        with self.engine.connect() as conn:
            value = conn.execute(select(state.c.value).where(state.c.key == key)).scalar()
            return json.loads(value) if value is not None else default

    def defer_api(self, code, until):
        """A monitor's short delay must never shorten the executor's Retry-After."""
        with self.observation_transaction() as conn:
            raw = conn.execute(select(state.c.value).where(state.c.key == 'wiki:api_backoff').with_for_update()).scalar_one()
            previous = json.loads(raw)
            if previous.get('until', 0) >= until:
                return previous
            value = dict(code=code, until=until)
            conn.execute(update(state).where(state.c.key == 'wiki:api_backoff').values(value=dump(value)))
            return value

    @contextmanager
    def observation_transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def observe_problems(self, processor, entries, evidence):
        """Cached views never advance confirmation dates or reopen older findings."""
        with self.observation_transaction() as conn:
            rows = {r['issue_key']: dict(r) for r in conn.execute(select(problem_history).where(
                problem_history.c.processor == processor).with_for_update()).mappings()}
            current = {entry['issue_key'] for entry in entries}
            for entry in entries:
                key, at = entry['issue_key'], entry['at']
                if not at:
                    continue
                old = rows.get(key)
                where = (problem_history.c.processor == processor, problem_history.c.issue_key == key)
                if old is None:
                    try:
                        with conn.begin_nested():
                            conn.execute(insert(problem_history).values(processor=processor, issue_key=key,
                                scope=entry['scope'], first_seen=at, last_seen=at, episodes=1))
                    except IntegrityError:
                        old = dict(conn.execute(select(problem_history).where(*where).with_for_update()).mappings().one())
                if old and at > max(old['last_seen'], old['resolved_at'] or 0):
                    conn.execute(update(problem_history).where(*where).values(last_seen=at, resolved_at=None,
                        episodes=old['episodes'] + bool(old['resolved_at'])))
            for key, old in rows.items():
                at = evidence.get(old['scope'], 0)
                if key not in current and not old['resolved_at'] and at >= old['last_seen']:
                    conn.execute(update(problem_history).where(problem_history.c.processor == processor,
                        problem_history.c.issue_key == key).values(resolved_at=at))
            return {r['issue_key']: dict(r) for r in conn.execute(select(problem_history).where(
                problem_history.c.processor == processor)).mappings()}

    def sync_notifications(self, conditions, now):
        """One notification per incident; recurrence creates a new unread entry."""
        with self.observation_transaction() as conn:
            active = {r['active_key']: dict(r) for r in conn.execute(select(notifications).where(
                notifications.c.active_key.is_not(None)).with_for_update()).mappings()}
            for key, value in conditions.items():
                if key in active:
                    if any(active[key][field] != value[field] for field in ('title', 'message', 'target')):
                        conn.execute(update(notifications).where(notifications.c.id == active[key]['id']).values(**value))
                    continue
                try:
                    with conn.begin_nested():
                        conn.execute(insert(notifications).values(id=uuid.uuid4().hex, active_key=key,
                            opened_at=now, **value))
                except IntegrityError:
                    if not conn.execute(select(notifications.c.id).where(notifications.c.active_key == key)).first():
                        raise
            for key, row in active.items():
                if key not in conditions:
                    conn.execute(update(notifications).where(notifications.c.id == row['id']).values(
                        active_key=None, resolved_at=now))

    def list_notifications(self, limit=50, offset=0):
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(notifications).order_by(
                notifications.c.opened_at.desc(), notifications.c.id).limit(limit).offset(offset)).mappings()]

    def unread_notifications(self):
        from sqlalchemy import func
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(notifications).where(
                notifications.c.read_at.is_(None))).scalar()

    def read_notification(self, notification_id, now):
        with self.engine.begin() as conn:
            return conn.execute(update(notifications).where(notifications.c.id == notification_id,
                notifications.c.read_at.is_(None)).values(read_at=now)).rowcount

    def read_all_notifications(self, now):
        with self.engine.begin() as conn:
            return conn.execute(update(notifications).where(
                notifications.c.read_at.is_(None)).values(read_at=now)).rowcount

    def set_state(self, key, value):
        with self.engine.begin() as conn:
            if conn.execute(select(state.c.key).where(state.c.key == key)).first():
                conn.execute(update(state).where(state.c.key == key).values(value=dump(value)))
            else:
                try:
                    with conn.begin_nested():
                        conn.execute(insert(state).values(key=key, value=dump(value)))
                except IntegrityError:
                    # The observer and executor can initialize the same state.
                    conn.execute(update(state).where(state.c.key == key).values(value=dump(value)))

    def connection_secret(self, name):
        with self.engine.connect() as conn:
            return conn.execute(select(connection_secrets.c.encrypted).where(
                connection_secrets.c.name == name)).scalar()

    def save_connection_secret(self, name, encrypted):
        with self.engine.begin() as conn:
            if conn.execute(select(connection_secrets.c.name).where(connection_secrets.c.name == name)).first():
                conn.execute(update(connection_secrets).where(connection_secrets.c.name == name).values(encrypted=encrypted))
            else:
                conn.execute(insert(connection_secrets).values(name=name, encrypted=encrypted))

    def clear_connection_secret(self, name):
        with self.engine.begin() as conn:
            conn.execute(delete(connection_secrets).where(connection_secrets.c.name == name))

    def page(self, title):
        with self.engine.connect() as conn:
            row = conn.execute(select(pages).where(pages.c.title == title)).mappings().first()
            return dict(row) if row else None

    def checked_articles(self, processor):
        with self.engine.connect() as conn:
            return {row['title']: dict(row) for row in conn.execute(
                select(article_checks).where(article_checks.c.processor == processor)).mappings()}

    def record_article_checks(self, processor, entries, *, overwrite=True):
        """Persist each completed article independently of the pass's final log."""
        with self.engine.begin() as conn:
            if self.engine.dialect.name == 'sqlite':
                # SQLite's deferred BEGIN would otherwise commit each released savepoint
                # separately. Keep bulk record imports atomic, with one disk commit.
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            for entry in entries:
                title_key = hashlib.sha256(entry['title'].encode('utf-8')).hexdigest()
                where = (article_checks.c.processor == processor, article_checks.c.title_key == title_key)
                values = dict(entry)
                if conn.execute(select(article_checks.c.title_key).where(*where)).first():
                    if overwrite:
                        conn.execute(update(article_checks).where(*where).values(**values))
                else:
                    try:
                        with conn.begin_nested():
                            conn.execute(insert(article_checks).values(processor=processor, title_key=title_key, **values))
                    except IntegrityError:
                        # Only a concurrent insertion of this key is recoverable.
                        # Validation/NOT NULL failures must roll back the batch.
                        if not conn.execute(select(article_checks.c.title_key).where(*where)).first():
                            raise
                        if overwrite:
                            conn.execute(update(article_checks).where(*where).values(**values))

    def reset_article_checks(self, processor, job):
        """Reset once when a full recheck starts; pause/resume keeps its progress."""
        key = processor + ':recheck_started'
        token = {name: job[name] for name in ('key', 'due_at', 'revision', 'spacing')}
        with self.engine.begin() as conn:
            raw = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
            if raw and json.loads(raw) == token:
                return False
            conn.execute(delete(article_checks).where(article_checks.c.processor == processor))
            conn.execute(delete(state).where(state.c.key == processor + ':result'))
            if raw is not None:
                conn.execute(update(state).where(state.c.key == key).values(value=dump(token)))
            else:
                conn.execute(insert(state).values(key=key, value=dump(token)))
            return True

    def patch_state(self, key, values):
        """Merge independent settings without overwriting another form's fields."""
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
            try:
                raw = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
                if raw is None:
                    try:
                        with conn.begin_nested():
                            conn.execute(insert(state).values(key=key, value=dump(values)))
                    except IntegrityError:
                        raw = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
                if raw is not None:
                    merged = json.loads(raw)
                    merged.update(values)
                    conn.execute(update(state).where(state.c.key == key).values(value=dump(merged)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def clear_state_if(self, key, value):
        with self.engine.begin() as conn:
            conn.execute(delete(state).where(state.c.key == key, state.c.value == dump(value)))

    def move_job(self, job, due_at):
        """Retime an unclaimed job, preserving newer edits and API backoff."""
        with self.engine.begin() as conn:
            active = conn.execute(select(runs.c.events).where(runs.c.processor == job["processor"],
                                  runs.c.status == "running")).scalars()
            if any(event.get("job") and all(job.get(key) == value for key, value in event["job"].items())
                   for events in active for event in json.loads(events) if event["code"] == "started"):
                return
            conn.execute(update(jobs).where(jobs.c.key == job["key"], jobs.c.revision == job["revision"],
                jobs.c.due_at == job["due_at"], jobs.c.spacing == job["spacing"], jobs.c.attempts == 0).values(due_at=due_at))

    def all_pages(self, processor="obkat"):
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(pages).where(
                pages.c.processor == processor).order_by(pages.c.month.desc())).mappings()]

    def save_page(self, title, **values):
        with self.engine.begin() as conn:
            if conn.execute(select(pages.c.title).where(pages.c.title == title)).first():
                conn.execute(update(pages).where(pages.c.title == title).values(**values))
            else:
                conn.execute(insert(pages).values(title=title, processor="obkat", **values))

    def enqueue(self, key, kind, due_at, title="", revision=0, spacing=False, requested_by="worker", *, processor="obkat"):
        """Page edits debounce; manual requests coalesce. Preserve pending spacing."""
        if processor != "obkat" and not key.startswith(processor + ":"):
            key = processor + ":" + key
        values = dict(processor=processor, kind=kind, title=title, revision=revision,
                      due_at=due_at, spacing=spacing, requested_by=requested_by)
        with self.engine.begin() as conn:
            control = conn.execute(select(state.c.value).where(state.c.key == "control:" + processor).with_for_update()).scalar()
            if control and json.loads(control)["mode"] == "stopped":
                return False
            try:
                with conn.begin_nested():
                    conn.execute(insert(jobs).values(key=key, **values))
                return True
            except IntegrityError:
                if kind == "page":
                    # Never let replayed older events move the quiet period back.
                    conn.execute(update(jobs).where(jobs.c.key == key,
                        jobs.c.revision <= revision).values(
                        revision=revision, due_at=due_at, attempts=0))
                    if spacing:
                        conn.execute(update(jobs).where(jobs.c.key == key).values(spacing=True))
                return False

    def queue(self, processor="obkat"):
        with self.engine.connect() as conn:
            statement = select(jobs)
            if processor is not None:
                statement = statement.where(jobs.c.processor == processor)
            return [dict(r) for r in conn.execute(statement.order_by(jobs.c.due_at)).mappings()]

    def request_run(self, processor, requested_by, *, kind='full', spacing=False):
        """Deduplicate repeated clicks and retain a stable link before execution."""
        with self.observation_transaction() as conn:
            conn.execute(select(state.c.key).where(state.c.key == 'control:obkat').with_for_update()).first()
            return self._request_run(conn, processor, requested_by, kind=kind, spacing=spacing)

    def request_runs(self, processors, requested_by):
        """Queue one normal pass per active task in a single transaction."""
        receipts, skipped = [], []
        with self.observation_transaction() as conn:
            conn.execute(select(state.c.key).where(state.c.key == 'control:obkat').with_for_update()).first()
            for processor in dict.fromkeys(processors):
                try:
                    receipts.append(self._request_run(conn, processor, requested_by))
                except ValueError:
                    skipped.append(processor)
            if not receipts:
                raise ValueError('No active tasks')
        return receipts, skipped

    def _request_run(self, conn, processor, requested_by, *, kind='full', spacing=False):
        key = 'control:' + processor
        raw = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
        if not raw:
            conn.execute(insert(state).values(key=key, value=dump(
                {'mode': 'active', 'generation': 0, 'at': None, 'by': ''})))
        elif json.loads(raw)['mode'] != 'active':
            raise ValueError('Task is paused or stopped')
        existing = conn.execute(select(run_requests).join(jobs, jobs.c.key == run_requests.c.job_key).where(
            jobs.c.processor == processor, jobs.c.kind == kind, jobs.c.spacing == spacing).order_by(
            run_requests.c.requested_at)).mappings().first()
        if existing:
            status = conn.execute(select(runs.c.status).where(runs.c.id == existing['run_id'])).scalar() if existing['run_id'] else None
            if status in {None, 'running'}:
                return dict(existing)
            # A new click after a failed or paused pass creates a new live
            # journal and replaces only that request's pending retry.
            conn.execute(delete(jobs).where(jobs.c.key == existing['job_key']))
        request_id, at = uuid.uuid4().hex, time.time()
        job_key = processor + ':manual:' + request_id
        conn.execute(insert(jobs).values(key=job_key, processor=processor, kind=kind, due_at=at,
            requested_by=requested_by, spacing=spacing))
        receipt = dict(id=request_id, processor=processor, job_key=job_key, requested_at=at, run_id=None)
        conn.execute(insert(run_requests).values(**receipt))
        return receipt

    def run_request(self, request_id=None, *, job_key=None):
        with self.engine.connect() as conn:
            row = conn.execute(select(run_requests).where(run_requests.c.job_key == job_key if job_key
                else run_requests.c.id == request_id)).mappings().first()
            return dict(row) if row else None

    def pending_run_requests(self):
        with self.engine.connect() as conn:
            return {row['job_key']: dict(row) for row in conn.execute(select(run_requests).join(
                jobs, jobs.c.key == run_requests.c.job_key)).mappings()}

    def control(self, processor="obkat"):
        return self.get_state("control:" + processor,
                              {"mode": "active", "generation": 0, "at": None, "by": ""})

    def change_control(self, processor, action, requested_by):
        """Persist a command; a generation change also cancels a running pass."""
        modes = {"pause": "paused", "resume": "active", "stop": "stopped", "restart": "active"}
        if action not in modes:
            raise ValueError("Unknown control action")
        key, now = "control:" + processor, time.time()
        with self.engine.begin() as conn:
            old = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
            previous = json.loads(old) if old else {"generation": 0}
            value = {"mode": modes[action], "generation": previous["generation"] + 1,
                     "at": now, "by": requested_by, "action": action}
            if old:
                conn.execute(update(state).where(state.c.key == key).values(value=dump(value)))
            else:
                conn.execute(insert(state).values(key=key, value=dump(value)))
            if action in {"stop", "restart"}:
                conn.execute(delete(jobs).where(jobs.c.processor == processor))
            if action == "restart":
                conn.execute(insert(jobs).values(key="manual:restart" if processor == "obkat" else processor + ":manual:restart", processor=processor,
                    kind="full", due_at=now, requested_by=requested_by))
                keys = ("last_reconcile", "worker_error") if processor == "obkat" else (processor + ":worker_error",)
                conn.execute(delete(state).where(state.c.key.in_(keys)))
        return value

    def acknowledge(self, job):
        # A later event can update the same row while a page is being processed.
        with self.engine.begin() as conn:
            conn.execute(delete(jobs).where(jobs.c.key == job["key"],
                jobs.c.revision == job["revision"], jobs.c.due_at == job["due_at"],
                jobs.c.spacing == job["spacing"]))

    def cancel_schedule(self, processor):
        with self.engine.begin() as conn:
            conn.execute(delete(jobs).where(jobs.c.processor == processor,
                jobs.c.kind == "daily", jobs.c.due_at > time.time()))

    def resolve_page_job(self, title, revision, spacing):
        with self.engine.begin() as conn:
            statement = delete(jobs).where(jobs.c.title == title, jobs.c.kind == "page", jobs.c.revision <= revision)
            if not spacing:
                statement = statement.where(jobs.c.spacing == False)
            conn.execute(statement)

    def retry(self, job, now):
        attempts = job["attempts"] + 1
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.key == job["key"],
                jobs.c.revision == job["revision"], jobs.c.due_at == job["due_at"]).values(
                attempts=attempts, due_at=now + min(3600, 60 * 2 ** min(attempts, 6))))

    def start_run(self, kind, requested_by="worker", dry_run=True, spacing=False, *, processor="obkat", job=None):
        run_id = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(runs).values(id=run_id, processor=processor, kind=kind,
                requested_by=requested_by, started_at=time.time(), status="running",
                dry_run=dry_run, spacing=spacing))
            if job:
                conn.execute(update(run_requests).where(run_requests.c.job_key == job['key'],
                    run_requests.c.run_id.is_(None)).values(run_id=run_id))
        return run_id

    def finish_run(self, run_id, events, summary, report, table_text="", status="success"):
        from .issues import annotate_report
        run = self.run(run_id)
        if run:
            report = annotate_report(self, run['processor'], report)
        with self.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(
                finished_at=time.time(), status=status, events=dump(events),
                summary=dump(summary), report=dump(report), table_text=table_text))

    def update_progress(self, run_id, events):
        with self.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(events=dump(events)))

    def pop_state(self, key):
        with self.engine.begin() as conn:
            value = conn.execute(select(state.c.value).where(state.c.key == key).with_for_update()).scalar()
            conn.execute(delete(state).where(state.c.key == key))
            return json.loads(value) if value else None

    def prune_oauth_requests(self, now):
        with self.engine.begin() as conn:
            rows = conn.execute(select(state.c.key, state.c.value).where(state.c.key.like("oauth:%"))).all()
            expired = [key for key, value in rows if json.loads(value)["expires"] < now]
            if expired:
                conn.execute(delete(state).where(state.c.key.in_(expired)))

    def list_runs(self, limit=100, offset=0, processor="obkat", exclude_import=False, *, started_from=None, started_until=None, status=None, overlap=False):
        with self.engine.connect() as conn:
            statement = select(runs)
            if processor is not None:
                statement = statement.where(runs.c.processor == processor)
            if exclude_import:
                statement = statement.where(runs.c.kind != "import")
            if started_from is not None:
                statement = statement.where(or_(runs.c.finished_at >= started_from, runs.c.status == 'running')
                    if overlap else runs.c.started_at >= started_from)
            if started_until is not None:
                statement = statement.where(runs.c.started_at < started_until)
            if status:
                statement = statement.where(runs.c.status == status)
            return [dict(r) for r in conn.execute(statement.order_by(
                runs.c.started_at.desc()).offset(offset).limit(limit)).mappings()]

    def run(self, run_id):
        with self.engine.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
            return dict(row) if row else None

    def lease_active(self, processor, now=None):
        with self.engine.connect() as conn:
            expires = conn.execute(select(leases.c.expires_at).where(leases.c.key == processor)).scalar()
            return bool(expires and expires > (time.time() if now is None else now))

    def recover_runs(self, processor="obkat"):
        with self.engine.begin() as conn:
            recovered = [dict(row) for row in conn.execute(select(runs).where(
                runs.c.status == "running", runs.c.processor == processor)).mappings()]
            conn.execute(update(runs).where(runs.c.status == "running", runs.c.processor == processor).values(
                status="interrupted", finished_at=time.time()))
        return recovered

    @contextmanager
    def worker_lease(self, now=None, *, processor="obkat"):
        owner = uuid.uuid4().hex
        now = time.time() if now is None else now
        with self.engine.begin() as conn:
            if not conn.execute(select(leases.c.key).where(leases.c.key == processor)).first():
                try:
                    with conn.begin_nested():
                        conn.execute(insert(leases).values(key=processor, owner="", expires_at=0))
                except IntegrityError:
                    pass
            acquired = conn.execute(update(leases).where(leases.c.key == processor,
                leases.c.expires_at < now).values(owner=owner, expires_at=now + 600)).rowcount == 1
        def renew():
            with self.engine.begin() as conn:
                if conn.execute(update(leases).where(leases.c.key == processor, leases.c.owner == owner,
                    leases.c.expires_at > time.time()).values(expires_at=time.time() + 600)).rowcount != 1:
                    raise RuntimeError("Worker lease expired")
        try:
            yield renew if acquired else None
        finally:
            if acquired:
                with self.engine.begin() as conn:
                    conn.execute(update(leases).where(leases.c.key == processor, leases.c.owner == owner).values(
                        expires_at=0, owner=""))
