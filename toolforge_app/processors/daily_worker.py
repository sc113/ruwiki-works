"""Shared lifecycle for serial daily actions, controls and live progress."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..execution import execution_slot
from ..schedules import get_schedule, next_daily_time
from ..wiki import WikiClient

MOSCOW = ZoneInfo("Europe/Moscow")


class Controlled(Exception):
    def __init__(self, mode):
        self.mode = mode


class DailyWorker:
    def __init__(self, settings, store, slug, wiki=None):
        self.settings, self.store, self.slug = settings, store, slug
        self.wiki = wiki or WikiClient(settings, store)
        self.generation = None
        self.renew = lambda: None
        self.outer_renew = lambda: None
        self.events, self.run_id = [], None
        self.wiki.request_guard = self.checkpoint

    def checkpoint(self):
        self.renew()
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
        if not any(job["kind"] == "daily" for job in self.store.queue(self.slug)):
            due = next_daily_time(get_schedule(self.settings, self.store, self.slug)["run_time"], now)
            date = datetime.fromtimestamp(due, MOSCOW).date().isoformat()
            self.store.enqueue("daily:" + date, "daily", due, processor=self.slug)


    def execute(self, job):
        with execution_slot(self, self.slug, job) as acquired:
            if acquired:
                self._execute(job)


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
                due = [job for job in self.store.queue(self.slug) if job["due_at"] <= now]
                if due:
                    self.execute(min(due, key=lambda job: (job["kind"] == "daily", job["due_at"])))
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
