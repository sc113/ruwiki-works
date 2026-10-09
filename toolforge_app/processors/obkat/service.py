"""Reuse the current report until a committed page observation changes."""
from copy import deepcopy

from ...issues import annotate_report
from .report import build_report


def report(store):
    with store.report_cache_lock:
        version = store.get_state("obkat:pages_version")
        cached = store.report_cache.get("obkat")
        if cached is None or cached[0] != version:
            # Read the version first: a concurrent write makes this cache expire
            # on the next request, even if it lands while the report is built.
            rows = store.all_pages()
            value = annotate_report(store, "obkat", build_report(rows),
                                    scopes={row["title"]: row["checked_at"] for row in rows})
            cached = (version, value)
            store.report_cache["obkat"] = cached
        # Page filtering and historical annotations must not mutate the cache.
        return deepcopy(cached[1])
