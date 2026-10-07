"""Serial executor and independent read-only observation of deadlines."""
import time

from .worker import Worker
from .processors.maintenance import TASK_SLUGS
from .processors.maintenance.service import InventoryMonitor, MaintenanceWorker
from .processors.translations import TASK_SLUGS as TRANSLATION_TASKS
from .processors.translations.service import TranslationWorker, TranslationMonitor
from .processors.sections import TASK_SLUGS as SECTION_TASKS
from .processors.sections.service import SectionWorker, SectionMonitor
from .health import system_status


class Dispatcher:
    def __init__(self, settings, store, *, obkat=None, maintenance=None, translations=None, sections=None):
        self.settings, self.store = settings, store
        self.obkat = obkat or Worker(settings, store)
        self.maintenance = maintenance if maintenance is not None else [MaintenanceWorker(settings, store, slug) for slug in TASK_SLUGS]
        self.translations = translations if translations is not None else [TranslationWorker(settings, store, slug) for slug in TRANSLATION_TASKS]
        self.sections = sections if sections is not None else [SectionWorker(settings, store, slug) for slug in SECTION_TASKS]

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor="dispatcher") as renew:
            if not renew:
                return False
            self.store.set_state("executor_heartbeat", now)
            def heartbeat():
                renew()
                self.store.set_state('executor_heartbeat', time.time())
            # Queue every daily action before beginning the first one.
            for worker in [*self.maintenance, *self.translations, *self.sections]:
                worker.schedule(now)
            for worker in [self.obkat, *self.maintenance, *self.translations, *self.sections]:
                renew()
                previous_renew = worker.outer_renew
                worker.outer_renew = heartbeat
                try:
                    worker.tick()
                except Exception:
                    key = "worker_error" if worker is self.obkat else worker.slug + ":worker_error"
                    self.store.set_state(key, {"code": "internal", "at": time.time()})
                    import traceback
                    traceback.print_exc()
                finally:
                    worker.outer_renew = previous_renew
            self.store.set_state("executor_heartbeat", time.time())
            system_status(self.settings, self.store)
            return True

    def forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                import traceback
                traceback.print_exc()
            time.sleep(5)


class Monitor:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.obkat = Worker(settings, store)
        self.inventory = InventoryMonitor(settings, store)
        self.translations = TranslationMonitor(settings, store)
        self.sections = SectionMonitor(settings, store)
        self.schedulers = ([MaintenanceWorker(settings, store, slug) for slug in TASK_SLUGS]
                           + [TranslationWorker(settings, store, slug) for slug in TRANSLATION_TASKS]
                           + [SectionWorker(settings, store, slug) for slug in SECTION_TASKS])

    def tick(self):
        if not self.store.get_state('monitor_started_at'):
            self.store.set_state('monitor_started_at', time.time())
        def heartbeat():
            self.store.set_state('monitor_heartbeat', time.time())
        heartbeat()
        for monitor in (self.inventory, self.translations, self.sections):
            monitor.outer_renew = heartbeat
        previous_renew = self.obkat.outer_renew
        self.obkat.outer_renew = heartbeat
        try:
            self.obkat.watch(time.time())
        finally:
            self.obkat.outer_renew = previous_renew
        for monitor, slugs in ((self.inventory, TASK_SLUGS), (self.translations, TRANSLATION_TASKS),
                               (self.sections, SECTION_TASKS)):
            try:
                monitor.tick()
            except Exception:
                for slug in slugs:
                    self.store.set_state(slug + ':monitor_error', dict(code='internal', at=time.time()))
                import traceback
                traceback.print_exc()
        for worker in self.schedulers:
            worker.schedule(time.time())
        self.store.set_state("monitor_heartbeat", time.time())
        system_status(self.settings, self.store)

    def forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                import traceback
                traceback.print_exc()
            time.sleep(5)
