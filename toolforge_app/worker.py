import calendar
import json
import time
from datetime import datetime

from .processors.obkat.analyzer import analyze_text
from .processors.obkat.formatter import format_text
from .processors.obkat.report import (TABLE_TITLE, build_report, month_range,
                                     page_title, title_month)
from .processors.obkat.table import generate_wiki_table
from .storage import dump
from .execution import execution_slot
from .schedules import after_edit_due, get_schedule, month_end_deadline
from .run_statistics import report_changes, report_snapshot
from .wiki import RUN_ERRORS, WikiClient, WikiError, bot_may_edit, epoch
from .runtime import execution_settings, service_enabled


class RunControlled(Exception):
    def __init__(self, mode):
        self.mode = mode


class Worker:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.base_settings = settings
        self.wiki = wiki or WikiClient(settings, store)
        self.events = []
        self.completed_pages = []
        self.run_id = None
        self.execution_generation = None
        if isinstance(self.wiki, WikiClient):
            self.wiki.request_guard = self.check_control
        self.renew = lambda: None
        self.outer_renew = lambda: None

    def event(self, code, message, title="", **details):
        self.events.append(dict(at=time.time(), code=code, message=message, title=title, **details))
        if self.run_id:
            self.store.update_progress(self.run_id, self.events)

    def check_control(self):
        self.renew()
        if self.execution_generation is not None and not service_enabled(self.store, 'executor'):
            raise RunControlled('paused')
        control = self.store.control()
        if self.execution_generation is None:
            if control["mode"] == "stopped":
                raise RunControlled("stopped")
            return
        if control["mode"] != "active" or control["generation"] != self.execution_generation:
            raise RunControlled(control["mode"])

    def observe(self, title, revision, edited_at, now):
        month = title_month(title)
        if not month or month < self.settings.start_month:
            return
        cached = self.store.page(title)
        if cached and cached["revision"] == revision and cached["origin"] == "wiki":
            return
        # Identical events are replayed at the polling boundary. Keep a retry's
        # backoff instead of resetting it on every poll.
        pending = next((j for j in self.store.queue() if j["key"] == "page:" + month), None)
        if pending and pending["revision"] >= revision:
            return
        delay = get_schedule(self.settings, self.store, "obkat")["quiet_minutes"]
        self.store.enqueue("page:" + month, "page", after_edit_due(delay, edited_at, now),
            title=title, revision=revision)

    def poll(self, now):
        cursor = self.store.get_state("rc_cursor", now - 60)
        # Reconciliation catches older changes if RecentChanges has expired.
        start = max(cursor - 2, now - 29 * 86400)
        for change in self.wiki.changes(start, now):
            self.renew()
            self.observe(change["title"], change["revid"], epoch(change["timestamp"]), now)
        self.store.set_state("rc_cursor", now)
        self.store.set_state("last_poll", now)

    def reconcile(self, now):
        titles = [page_title(m) for m in month_range(self.settings.start_month,
                  datetime.fromtimestamp(now, self.settings.zone))]
        for offset in range(0, len(titles), 20):
            self.renew()
            for revision in self.wiki.revisions(titles[offset:offset + 20]):
                cached = self.store.page(revision.title)
                if revision.missing:
                    if cached and not cached["missing"]:
                        self.store.enqueue("page:" + cached["month"], "page", now, title=revision.title)
                else:
                    self.observe(revision.title, revision.revision, revision.edited_at, now)
        self.store.set_state("last_reconcile", now)

    def schedule_month_end(self, now):
        local = datetime.fromtimestamp(now, self.settings.zone)
        clock = get_schedule(self.settings, self.store, "obkat")["month_end_time"]
        hour, minute = map(int, clock.split(":"))
        current = local.strftime("%Y-%m")
        last = self.store.get_state("last_month_end")
        last_day = calendar.monthrange(local.year, local.month)[1]
        due = local.day == last_day and (local.hour, local.minute) >= (hour, minute)
        # Finish the previous month if the daemon was offline at month end.
        previous = f"{local.year if local.month > 1 else local.year - 1}-{local.month - 1 if local.month > 1 else 12:02d}"
        target = current if due else previous if last and last < previous else None
        if target and (not last or last < target):
            self.store.enqueue("month_end:" + target, "month_end", now, spacing=True)
        for job in self.store.queue():
            if job["kind"] == "month_end" and job["key"] == "month_end:" + current and not job["attempts"]:
                deadline = month_end_deadline(current, clock, self.settings.zone)
                if job["due_at"] > now or deadline > now:
                    self.store.move_job(job, max(now, deadline))

    def retime_pages(self, now):
        request = self.store.get_state("obkat:retime_requested")
        if not request:
            return
        pending = {job["title"]: job for job in self.store.queue() if job["kind"] == "page" and not job["attempts"]}
        delay = get_schedule(self.settings, self.store, "obkat")["quiet_minutes"]
        titles = list(pending)
        for offset in range(0, len(titles), 20):
            self.renew()
            for base in self.wiki.revisions(titles[offset:offset + 20]):
                job = pending[base.title]
                if base.missing or base.revision == job["revision"]:
                    self.store.move_job(job, now if base.missing else after_edit_due(delay, base.edited_at, now))
                elif base.revision > job["revision"]:
                    self.observe(base.title, base.revision, base.edited_at, now)
        self.store.clear_state_if("obkat:retime_requested", request)

    def process_page(self, title, spacing=False, respect_quiet=False):
        self.check_control()
        self.renew()
        base = self.wiki.fetch(title)
        self.check_control()
        month = title_month(title)
        if base.missing:
            self.store.save_page(title, month=month, missing=True, text="", issues="[]",
                revision=0, edited_at=0, checked_at=time.time(), origin="wiki")
            self.completed_pages.append((title, 2 ** 31 - 1, True))
            self.event("missing", "Страница отсутствует; исключена из отчёта", title)
            return
        if base.text.lstrip().lower().startswith(("#redirect", "#перенаправление")):
            raise WikiError("redirect")
        delay = get_schedule(self.settings, self.store, "obkat")["quiet_minutes"]
        if respect_quiet and time.time() < base.edited_at + delay * 60:
            self.store.enqueue("page:" + month, "page", base.edited_at + delay * 60,
                title=title, revision=base.revision, spacing=spacing)
            self.event("deferred", f"Новая правка: обработка отложена до окончания {delay}-минутной паузы", title)
            return
        proposed = format_text(base.text, spacing=spacing)
        actual, revision = base.text, base.revision
        if proposed == actual:
            self.event("unchanged", "Изменения не требуются", title, revision=revision)
        elif not bot_may_edit(base.text, self.settings.bot_username):
            self.event("bot_excluded", "Изменения пропущены: страница запрещает работу этого бота", title, revision=revision)
        elif not self.settings.wiki_write:
            self.event("would_edit", "Проверка: подготовлены изменения", title,
                revision=revision, diff_before=actual, diff_after=proposed)
        else:
            self.renew()
            self.check_control()
            revision = self.wiki.edit(base, proposed,
                "ОБКАТ: зачёркивание завершённых номинаций" + (" и ежемесячное форматирование" if spacing else ""))
            actual = proposed
            self.event("edited", "Страница обновлена", title, revision=revision,
                diff_before=base.text, diff_after=actual)
        self.store.save_page(title, month=month, revision=revision, edited_at=base.edited_at,
            checked_at=time.time(), text=actual, issues=dump(analyze_text(actual)),
            missing=False, origin="wiki")
        self.completed_pages.append((title, base.revision, spacing))

    def refresh_table(self):
        self.check_control()
        # Only a complete, live snapshot may replace the on-wiki full index.
        rows = self.store.all_pages()
        table = generate_wiki_table({p["month"]: p["text"] for p in rows if not p["missing"]})
        expected = set(month_range(self.settings.start_month,
            datetime.now(self.settings.zone)))
        if not expected.issubset({p["month"] for p in rows if p["origin"] == "wiki"}):
            self.event("table_incomplete", "Таблица не опубликована: требуется полная синхронизация страниц")
            return table
        self.renew()
        base = self.wiki.fetch(TABLE_TITLE)
        self.check_control()
        if base.missing:
            self.event("table_missing", "Страница таблицы отсутствует; автоматическое создание отключено", TABLE_TITLE)
        elif base.text.strip() == table.strip():
            self.event("table_unchanged", "Таблица открытых номинаций уже актуальна", TABLE_TITLE)
        elif not bot_may_edit(base.text, self.settings.bot_username):
            self.event("table_excluded", "Таблица не изменена: на странице запрещена работа этого бота", TABLE_TITLE)
        elif self.settings.wiki_write:
            self.check_control()
            revision = self.wiki.edit(base, table, "ОБКАТ: обновление таблицы открытых номинаций")
            self.event("table_edited", "Таблица открытых номинаций обновлена", TABLE_TITLE, revision=revision)
        else:
            self.event("table_would_edit", "Проверка: подготовлено обновление таблицы", TABLE_TITLE)
        return table

    def execute(self, job):
        if not service_enabled(self.store, 'executor'):
            return
        self.settings = execution_settings(self.base_settings, self.store)
        with execution_slot(self, "obkat", job) as acquired:
            if acquired:
                self._execute(job)

    def _execute(self, job):
        control = self.store.control()
        if control["mode"] != "active":
            return
        queued_job = next((pending for pending in self.store.queue() if pending["key"] == job["key"]), None)
        if not queued_job or any(queued_job[key] != job[key] for key in ("revision", "due_at", "spacing")):
            return
        self.execution_generation = control["generation"]
        self.events = []
        self.completed_pages = []
        before_report = report_snapshot(build_report(self.store.all_pages()))
        self.run_id = self.store.start_run(job["kind"], job["requested_by"],
            dry_run=not self.settings.wiki_write, spacing=job["spacing"])
        failed, table, controlled = False, "", None
        try:
            self.event("started", "Начало обработки", spacing=job["spacing"],
                job={key: job[key] for key in ("key", "revision", "due_at", "spacing")})
            titles = [job["title"]] if job["kind"] == "page" else [page_title(m) for m in
                month_range(self.settings.start_month, datetime.now(self.settings.zone))]
            for title in titles:
                try:
                    self.process_page(title, spacing=job["spacing"],
                        respect_quiet=job["kind"] in {"page", "month_end", "bootstrap"})
                except WikiError as exc:
                    if exc.code in RUN_ERRORS:
                        raise
                    failed = True
                    self.event("error", "Не удалось обработать страницу", title, error=exc.code)
            if not failed:
                table = self.refresh_table()
            else:
                self.event("table_skipped", "Публикация таблицы отложена из-за ошибки обработки")
        except RunControlled as exc:
            controlled = {"paused": "paused", "stopped": "stopped"}.get(exc.mode, "interrupted")
            self.event("control", {"paused": "Обработка приостановлена: задание сохранено в очереди",
                "stopped": "Обработка остановлена по команде администратора",
                "interrupted": "Текущий проход прерван новой командой управления"}[controlled])
        except WikiError as exc:
            failed = True
            self.event("error", "Ошибка MediaWiki API", error=exc.code)
        except Exception:
            failed = True
            self.event("error", "Внутренняя ошибка обработчика; подробности доступны в служебном выводе", error="internal")
            # No tokens, cookies, passwords or full request parameters in public logs.
            import traceback
            traceback.print_exc()
        report = build_report(self.store.all_pages())
        summary = {"checked": sum(e["code"] in {"edited", "would_edit", "unchanged", "missing", "bot_excluded"} for e in self.events),
            "changed": sum(e["code"] == "edited" for e in self.events),
            "proposed": sum(e["code"] == "would_edit" for e in self.events),
            "deferred": sum(e["code"] == "deferred" for e in self.events),
            "problems": report["problems"], "nuances": report["nuances"],
            "skipped": sum(e["code"] in {"missing", "bot_excluded"} for e in self.events),
            "errors": sum(e["code"] == "error" for e in self.events)}
        summary.update(report_changes(before_report, report))
        self.event("finished", "Обработка завершена с ошибками" if failed else
                   "Обработка прервана командой управления" if controlled else "Обработка завершена")
        self.store.finish_run(self.run_id, self.events, summary, report, table,
                              controlled or ("failed" if failed else "success"))
        self.run_id = None
        self.execution_generation = None
        if controlled:
            return
        if failed:
            self.store.retry(job, time.time())
        else:
            for title, revision, spacing in self.completed_pages:
                self.store.resolve_page_job(title, revision, spacing)
            self.store.acknowledge(job)
            if job["kind"] == "month_end":
                self.store.set_state("last_month_end", job["key"].split(":", 1)[1])
            if job["kind"] != "page":
                self.store.set_state("live_sync_initialized", True)

    def watch(self, now):
        """Observe edits even while another action occupies the execution slot."""
        previous_renew = self.renew
        with self.store.worker_lease(processor="obkat-observer") as lease_renew:
            if not lease_renew:
                return False
            def renew():
                lease_renew()
                self.outer_renew()
                previous_renew()
                self.store.set_state("observer_heartbeat", time.time())
            self.renew = renew
            try:
                if self.store.control()["mode"] == "stopped":
                    return True
                retry = self.store.get_state("obkat:observer_retry") or {}
                if now < retry.get("due_at", 0):
                    return False
                from .schedules import search_interval
                if now - self.store.get_state("last_poll", 0) >= search_interval(self.settings, self.store, 'obkat'):
                    self.poll(now)
                if now - self.store.get_state("last_reconcile", 0) >= 86400:
                    self.reconcile(now)
                self.retime_pages(now)
                from .issues import annotate_report
                annotate_report(self.store, 'obkat', build_report(self.store.all_pages()))
                self.store.set_state("worker_error", None)
                self.store.set_state("obkat:observer_retry", None)
                self.schedule_month_end(now)
                return True
            except WikiError as exc:
                self.store.set_state("worker_error", {"at": now, "code": exc.code})
                attempts = min(7, (self.store.get_state("obkat:observer_retry") or {}).get("attempts", 0) + 1)
                self.store.set_state("obkat:observer_retry", dict(attempts=attempts,
                                     due_at=now + min(3600, 60 * 2 ** (attempts - 1))))
                return False
            except RunControlled:
                return True
            finally:
                self.renew = previous_renew

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store.worker_lease() as lease_renew:
            if not lease_renew:
                return False
            def renew():
                lease_renew()
                self.outer_renew()
                self.store.set_state("worker_heartbeat", time.time())
            self.renew = renew
            self.store.recover_runs()
            self.store.set_state("worker_heartbeat", now)
            if self.store.control()["mode"] == "stopped":
                return True
            # Check jobs frequently for prompt manual starts. Poll the wiki at
            # its own interval; every page still fetches a fresh base before edit.
            # A failed due poll blocks execution until connectivity is restored.
            if service_enabled(self.store, 'monitor') and not self.watch(now):
                return False
            if not service_enabled(self.store, 'executor'):
                return True
            self.schedule_month_end(now)
            if self.store.control()["mode"] != "active":
                return True
            if not self.store.get_state("live_sync_initialized"):
                self.store.enqueue("bootstrap", "bootstrap", now)
            due = [j for j in self.store.queue() if j["due_at"] <= now]
            if due:
                priority = {"bootstrap": 0, "full": 1, "month_end": 2, "page": 3}
                due.sort(key=lambda j: (priority.get(j["kind"], 4), j["due_at"]))
                self.execute(due[0])
            self.store.set_state("worker_heartbeat", time.time())
            return True

    def forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                self.store.set_state("worker_error", {"at": time.time(), "code": "internal"})
                import traceback
                traceback.print_exc()
            time.sleep(self.settings.poll_seconds if self.store.get_state("worker_error") else min(5, self.settings.poll_seconds))
