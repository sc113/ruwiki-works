"""Comparable report snapshots and public numeric results of a run."""
from collections import Counter


SUMMARY_FIELDS = (
    "checked", "changed", "proposed", "skipped", "previously_checked", "errors",
    "remaining", "problems", "nuances", "deferred", "report_problems", "report_skipped",
    "problems_added", "problems_resolved", "skipped_added", "skipped_resolved",
)


def public_summary(summary):
    return {key: value for key, value in summary.items() if key in SUMMARY_FIELDS
            and (value is None or type(value) in (int, float))}


def report_snapshot(report):
    if "items" in report:
        problems = Counter((item["page"], item["type"], item.get("display_title", ""),
                            item.get("parent_title", "")) for item in report["items"]
                           if item["section"] == "problems")
    else:
        problems = Counter(item["title"] for item in report.get("manual", []))
    skipped = Counter(item["title"] for item in report.get("skipped_articles", []))
    return dict(problems=problems, skipped=skipped)


def report_changes(before, report):
    after = report_snapshot(report)
    result = dict(report_problems=report.get("problems", 0), report_skipped=report.get("skipped", 0))
    for kind in ("problems", "skipped"):
        result[kind + "_added"] = sum((after[kind] - before[kind]).values())
        result[kind + "_resolved"] = sum((before[kind] - after[kind]).values())
    return result
