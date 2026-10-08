"""Shared administrator switches; process liveness is independent of permission."""
from dataclasses import replace


class ObservationPaused(Exception):
    pass


def service_enabled(store, name):
    return bool((store.get_state('service:' + name, {}) or {}).get('enabled', True))


def writes_enabled(settings, store=None):
    return settings.wiki_write and (store is None or service_enabled(store, 'writes'))


def execution_settings(settings, store):
    return replace(settings, wiki_write=writes_enabled(settings, store))
