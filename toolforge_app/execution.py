"""One execution slot shared by every processor, ordered like the website."""
import json
import time
from contextlib import contextmanager

from .processors import TASKS

ORDER = {task.slug: index for index, task in enumerate(TASKS) if task.enabled}
KIND_ORDER = {"bootstrap": 0, "full": 1, "recheck": 1, "article": 1, "month_end": 2, "daily": 3, "page": 4}


def ready_jobs(store, now=None, *, exclude_running=False):
    now = time.time() if now is None else now
    if now < (store.get_state('wiki:api_backoff') or {}).get('until', 0):
        return []
    observer_wait = now < (store.get_state('obkat:observer_retry') or {}).get('due_at', 0)
    requests = store.pending_run_requests()
    switch = store.get_state('service:executor', {}) or {}
    active = []
    if exclude_running:
        for run in store.list_runs(20, processor=None, exclude_import=True):
            if run["status"] == "running":
                active.extend((run["processor"], event["job"]) for event in json.loads(run["events"])
                              if event["code"] == "started" and event.get("job"))
    jobs = [job for job in store.queue(None) if job["processor"] in ORDER and job["due_at"] <= now
            and (switch.get('enabled', True) or requests.get(job['key'], {}).get('requested_at', 0) > (switch.get('at') or 0))
            and not (job['processor'] == 'obkat' and observer_wait and job['key'] not in requests)
            and store.control(job["processor"])["mode"] == "active"
            and not any(slug == job["processor"] and all(job.get(key) == value for key, value in claimed.items())
                        for slug, claimed in active)]
    return sorted(jobs, key=lambda job: (job['key'] not in requests,
        ORDER[job["processor"]], KIND_ORDER.get(job["kind"], 5), job["due_at"], job["key"]))


@contextmanager
def execution_slot(worker, slug, job):
    """Protect per-task commands from overlapping the dispatcher."""
    previous_renew = worker.renew
    with worker.store.worker_lease(processor="execution") as renew:
        first = ready_jobs(worker.store)
        if not renew or not first or first[0] != job or job["processor"] != slug:
            yield False
            return
        def heartbeat():
            renew()
            previous_renew()
            worker.store.set_state("executor_heartbeat", time.time())
        worker.renew = heartbeat
        try:
            heartbeat()
            yield True
        finally:
            worker.renew = previous_renew
