"""Shared lifecycle for serial daily actions, controls and live progress."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..execution import execution_slot, ready_jobs
from ..schedules import get_schedule, next_daily_time, next_weekly_time, next_month_end_time
from . import get_task
from ..wiki import WikiClient
from ..runtime import execution_settings, job_enabled

MOSCOW = ZoneInfo("Europe/Moscow")


class Controlled(Exception):
    def __init__(self, mode):
        self.mode = mode


class DailyWorker:
    def __init__(self, settings, store, slug, wiki=None):
        self.settings, self.store, self.slug = settings, store, slug
        self.base_settings = settings
        self.wiki = wiki or WikiClient(settings, store)
        self.generation = None
        self.renew = lambda: None
        self.outer_renew = lambda: None
        self.events, self.run_id = [], None
        self.active_job = None
        self.wiki.request_guard = self.checkpoint

    def checkpoint(self):
        self.renew()
        if self.generation is not None and not job_enabled(self.store, self.active_job):
            raise Controlled('paused')
        control = self.store.control(self.slug)
        if self.generation is not None and (control["mode"] != "active" or control["generation"] != self.generation):
            raise Controlled(control["mode"])


    def event(self, code, message, title="", **details):
        self.events.append(dict(at=time.time(), code=code, message=message, title=title, **details))
        if self.run_id:
            self.store.update_progress(self.run_id, self.events)


    def schedule(self, now):
        if self.store.control(self.slug)["mode"] == "stopped":
            return
        if not self.store.get_state(self.slug + ':schedule_started_at'):
            self.store.set_state(self.slug + ':schedule_started_at', now)
        schedule = get_schedule(self.settings, self.store, self.slug)
        kinds = get_task(self.slug).schedules
        deadlines = {}
        if 'daily' in kinds:
            deadlines['daily'] = next_daily_time(schedule['run_time'], now)
        if 'weekly' in kinds:
            deadlines['weekly'] = next_weekly_time(schedule['run_time'], schedule['weekday'], now)
        if 'month_end' in kinds:
            deadlines['month_end'] = next_month_end_time(schedule['month_end_time'], now)
        if deadlines.get('weekly') == deadlines.get('month_end') and 'weekly' in deadlines:
            del deadlines['weekly']
        for kind, due in deadlines.items():
            if any(job['kind'] == kind for job in self.store.queue(self.slug)):
                continue
            date = datetime.fromtimestamp(due, MOSCOW).date().isoformat()
            self.store.enqueue(kind + ':' + date, kind, due, processor=self.slug)


    def execute(self, job):
        if not job_enabled(self.store, job):
            return
        self.settings = execution_settings(self.base_settings, self.store)
        with execution_slot(self, self.slug, job) as acquired:
            if acquired:
                self.active_job = job
                try:
                    self._execute(job)
                finally:
                    self.active_job = None


    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor=self.slug) as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
                self.store.set_state(self.slug + ":worker_heartbeat", time.time())
            self.renew = heartbeat
            recovered = self.store.recover_runs(self.slug)
            for run in recovered:
                # A crashed pass is reported as interrupted; retry only at the
                # next daily deadline. Later explicit requests remain queued.
                for job in self.store.queue(self.slug):
                    if job["kind"] == run["kind"] and job["due_at"] <= run["started_at"]:
                        if job['kind'] == 'article':
                            self.store.move_job(job, next_daily_time(get_schedule(self.settings, self.store, self.slug)['run_time'], now))
                        else:
                            self.store.acknowledge(job)

            # A selected article that failed retains its override until the next
            # scheduled pass, rather than disappearing behind its old skip mark.
            heartbeat()
            self.schedule(now)
            if self.store.control(self.slug)["mode"] == "active":
                due = [job for job in ready_jobs(self.store, now) if job['processor'] == self.slug]
                if due:
                    self.execute(due[0])
            heartbeat()
            self.schedule(time.time())
            return True


    def forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                self.store.set_state(self.slug + ":worker_error", {"code": "internal", "at": time.time()})
                import traceback
                traceback.print_exc()
            time.sleep(5)
