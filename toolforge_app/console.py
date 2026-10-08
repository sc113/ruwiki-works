"""Shared run history and a public or detailed feed of processing events."""
import json
import time

from .processors import TASKS, Processor, get_processor, get_task
from .processors.logs import public_run
from .run_statistics import public_summary
from .execution import ready_jobs


ACTIVITY = Processor("activity", "Работы бота", "Работы бота", "История всех работ", True)


def decode_run(run):
    return {**run, **{key: json.loads(run[key]) for key in ("events", "summary", "report")}}


def count_label(value, one, few, many):
    number = int(value or 0) % 100
    return many if 11 <= number <= 14 else one if number % 10 == 1 else few if 2 <= number % 10 <= 4 else many


def run_metrics(run):
    summary = public_summary(run["summary"])
    if run["status"] == "running":
        terminal = {"edited", "would_edit", "unchanged", "missing", "bot_excluded"}
        summary.setdefault("checked", sum(event["code"] in terminal for event in run["events"]))
        summary.setdefault("changed", sum(event["code"] == "edited" for event in run["events"]))
        summary.setdefault("proposed", sum(event["code"] == "would_edit" for event in run["events"]))
    report = run.get("report", {})
    for field in ("problems", "skipped"):
        if type(report.get(field)) is int:
            summary.setdefault("report_" + field, report[field])
    metrics = [dict(label="проверено", value=summary.get("checked")),
               dict(label="изменено", value=summary.get("changed", 0))]
    if summary.get("proposed"):
        metrics.append(dict(label="подготовлено", value=summary["proposed"], tone="muted"))
    if "report_problems" in summary or "problems" in summary:
        value = summary.get("report_problems", summary.get("problems"))
        label = count_label(value, "проблема", "проблемы", "проблем")
        if "report_problems" not in summary:
            label = "найдено проблем"
        metrics.append(dict(label=label, value=value,
                            added=summary.get("problems_added"), resolved=summary.get("problems_resolved"), tone="warning"))
    if "skipped" in summary or "skipped_added" in summary:
        metrics.append(dict(label="пропущено", value=summary.get("skipped", 0),
                            added=summary.get("skipped_added"), tone="muted"))
    if summary.get("previously_checked"):
        metrics.append(dict(label="ранее проверено", value=summary["previously_checked"], tone="muted"))
    if summary.get("errors"):
        metrics.append(dict(label=count_label(summary["errors"], "ошибка", "ошибки", "ошибок"), value=summary["errors"], tone="error"))
    for metric in metrics:
        if metric.get("added"):
            metric["added_label"] = count_label(metric["added"], "новая", "новые", "новых")
    return metrics


def run_history(store, selected="", *, page=1, limit=25, started_from=None, started_until=None, status=None):
    rows = store.list_runs(limit + 1, offset=(page - 1) * limit, processor=selected or None,
                          exclude_import=True, started_from=started_from, started_until=started_until, status=status)
    entries = []
    for row in rows[:limit]:
        run = decode_run(row)
        task = get_task(run["processor"])
        entries.append(dict(run={key: run[key] for key in
                            ("id", "processor", "started_at", "finished_at", "status", "kind", "dry_run")},
                            task=task, module=get_processor(task.processor) if task else None,
                            metrics=run_metrics(run)))
    return dict(entries=entries, has_next=len(rows) > limit, page=page)


def console_data(store, selected="", *, full=True, page=1, started_from=None, started_until=None, status=None, include_events=True):
    events, has_more, offset = [], False, 0
    needed = page * 200
    # Walk archived runs until this event page is complete. No events disappear
    # behind a fixed number of recent runs or a per-run truncation.
    while include_events:
        recent = store.list_runs(15, offset=offset, processor=selected or None, exclude_import=True,
            started_from=started_from, started_until=started_until, status=status, overlap=True)
        if not recent:
            break
        for row in recent:
            task = get_task(row['processor'])
            if not task:
                continue
            run = decode_run(row)
            visible = run['events'] if full else public_run(run)['events']
            for index, event in enumerate(visible):
                if started_from is not None and event['at'] < started_from or started_until is not None and event['at'] >= started_until:
                    continue
                details = any(key not in {'at', 'code', 'message', 'title', 'error'} for key in event)
                events.append(dict(at=event['at'], code=event['code'], message=event['message'],
                    title=event.get('title', ''), error=event.get('error'), run_id=run['id'], task=task, index=index,
                    tone=event.get('tone', 'error' if event['code'] == 'error' else 'success' if event['code'] in {'edited', 'table_edited'} else 'neutral'),
                    details=bool(full and details)))
            if len(events) > needed:
                has_more = True
                break
        if has_more or len(recent) < 15:
            break
        offset += 15
    ordered = sorted(events, key=lambda event: (event['at'], event['run_id'], event['index']), reverse=True)
    page_events = ordered[(page - 1) * 200:needed]
    return dict(events=list(reversed(page_events)), has_more=has_more,
                history=run_history(store, selected, page=page, started_from=started_from, started_until=started_until, status=status),
                waiting=[dict(job=job, task=get_task(job['processor'])) for job in ready_jobs(store, exclude_running=True)
                         if not selected or job['processor'] == selected],
                running=[dict(run=run, task=get_task(run['processor'])) for run in
                         store.list_runs(20, processor=selected or None, exclude_import=True) if run['status'] == 'running'],
                online=time.time() - store.get_state('executor_heartbeat', 0) < 300,
                tasks=[task for task in TASKS if task.enabled])


def grouped_queue(jobs):
    groups = []
    pages = [job for job in jobs if job['processor'] == 'obkat' and job['kind'] == 'page']
    if pages:
        groups.append(dict(task=get_task('obkat'), label='После правок', count=len(pages),
            due_at=min(job['due_at'] for job in pages), latest=max(job['due_at'] for job in pages)))
    for job in jobs:
        if job in pages:
            continue
        groups.append(dict(task=get_task(job['processor']), label=None, job=job, count=1,
                           due_at=job['due_at'], latest=job['due_at']))
    return sorted(groups, key=lambda row: row['due_at'])
