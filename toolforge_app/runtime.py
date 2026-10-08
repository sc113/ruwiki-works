"""Shared administrator switches; process liveness is independent of permission."""
from dataclasses import replace


class ObservationPaused(Exception):
    pass


def service_enabled(store, name):
    return bool((store.get_state('service:' + name, {}) or {}).get('enabled', True))


def job_enabled(store, job):
    """An explicit request may run while automatic execution is off.

    A later global disable also interrupts that request at its next checkpoint.
    """
    switch = store.get_state('service:executor', {}) or {}
    if switch.get('enabled', True):
        return True
    receipt = store.run_request(job_key=job['key']) if job else None
    return bool(receipt and receipt['requested_at'] > (switch.get('at') or 0))


def writes_enabled(settings, store=None):
    return settings.wiki_write and (store is None or service_enabled(store, 'writes'))


def execution_settings(settings, store):
    return replace(settings, wiki_write=writes_enabled(settings, store))
