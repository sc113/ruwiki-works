"""A single public projection used by HTML, TXT and JSON exports."""
import copy

from .daily import TASK_SLUGS
from ..run_statistics import public_summary


def public_run(run):
    if run["processor"] not in TASK_SLUGS:
        return run
    result = {key: copy.deepcopy(run[key]) for key in
              ("id", "processor", "kind", "started_at", "finished_at", "status", "dry_run")}
    result.update(summary=public_summary(run["summary"]), report={}, table_text="", spacing=False)
    result["summary"].setdefault("changed", 0)
    for field in ("problems", "skipped"):
        if type(run["report"].get(field)) is int:
            result["summary"].setdefault("report_" + field, run["report"][field])
    events = []
    for event in run["events"]:
        if event["code"] != "edited":
            continue
        keys = (("template", "replacement", "section", "reason", "action") if run["processor"].startswith("sections-") else
                ("template", "language", "original", "action") if run["processor"].startswith("translations-") else ("template", "date", "action", "parameter"))
        changes = [{key: change.get(key) for key in keys}
                   for change in event.get("changes", [])]
        messages = []
        for change in changes:
            if change["action"] == "section_switch":
                message = "{{" + change["template"] + "}} → {{" + change["replacement"] + "}} · «" + change["section"] + "» · " + change["reason"]
            elif change["action"] == "translation_source":
                message = "{{" + change["template"] + "}}: язык=" + change["language"] + "; оригинал=" + change["original"]
            elif change["action"] == "removed_parameter":
                message = "RQ: удалён параметр «" + change["parameter"] + "»; шаблон «" + change["template"] + "» уже присутствовал"
            elif change["action"] == "unwrapped":
                message = "Убрана обёртка RQ → {{" + change["template"] + "}}" + ("; дата сохранена: " + change["date"] if change["date"] else "")
            else:
                message = "{{" + change["template"] + "}}" + (": дата " + change["date"] if change["date"] else "")
            messages.append(message)
        events.append({"at": event["at"], "code": "edited", "title": event["title"],
                       "message": "; ".join(messages), "changes": changes})
    result["events"] = events
    return result
