"""Shared overview of enabled processors and their actual run/queue state."""
import json
import time

from .processors import TASKS, Processor, get_processor
from .processors.obkat.service import report as obkat_report
from .processors.daily import TASK_SLUGS as DAILY_TASKS, get_config, report as daily_report
from .schedules import get_schedule, schedule_fields, next_task_time, next_month_end_time
from .execution import ready_jobs
from .runtime import execution_settings, service_enabled
from .health import component_health

OVERVIEW = Processor("overview", "Обзор", "Обзор", "Состояние всех задач", True)


def next_month_end(settings, now, store=None):
    clock = get_schedule(settings, store, "obkat")["month_end_time"] if store else settings.month_end_time
    return next_month_end_time(clock, now, settings.zone, store.get_state("last_month_end") if store else None)


def build_overview(settings, store, now=None):
    settings = execution_settings(settings, store)
    now = time.time() if now is None else now
    cards = []
    monitor = component_health(settings, store, 'monitor', now)
    waiting = ready_jobs(store, now, exclude_running=True)
    active_runs = [run for run in store.list_runs(20, processor=None, exclude_import=True, details=False) if run["status"] == "running"]
    for task in TASKS:
        processor = get_processor(task.processor)
        if not task.enabled:
            cards.append({"task": task, "processor": processor, "enabled": False,
                          "status": "unconnected", "status_label": "Не подключена",
                          "report": None, "queued": 0, "last_run": None, "next_job": None})
            continue
        maintenance = task.slug in DAILY_TASKS
        config = get_config(store, task.slug) if maintenance else None
        schedule = get_schedule(settings, store, task.slug)
        report = daily_report(store, task.slug, config) if maintenance else obkat_report(store)
        history = store.list_runs(1, processor=task.slug, exclude_import=True, details=False)
        imports = store.list_runs(1, processor=task.slug, details=False)
        latest = history[0] if history else None
        control = store.control(task.slug)
        mode = control["mode"]
        pending = store.queue(task.slug)
        running = latest and latest["status"] == "running"
        position = next((index + 1 + bool(active_runs) for index, job in enumerate(waiting)
                         if job["processor"] == task.slug), None)
        progress = None
        if running:
            events = json.loads(latest["events"])
            terminal = {"edited", "would_edit", "unchanged", "missing", "bot_excluded"}
            progress = dict(checked=sum(event["code"] in terminal for event in events),
                            title=next((event["title"] for event in reversed(events) if event.get("title")), ""),
                            message=events[-1]["message"] if events else "Начало обработки")
        if running and mode == "active":
            active_job = next((event.get("job") for event in json.loads(latest["events"])
                               if event["code"] == "started"), None)
            # Keep a newer edit of the same page visible as the next job.
            if active_job:
                pending = [job for job in pending
                           if any(job[key] != value for key, value in active_job.items())]
        prefix = task.slug + ":" if maintenance else ""
        heartbeat = store.get_state(prefix + "worker_heartbeat", 0)
        online = now - heartbeat < max(300, settings.poll_seconds * 4)
        error = store.get_state(prefix + "worker_error") or report.get("monitor_error")
        failed = latest and latest["status"] == "failed" and not (
            control.get("action") == "restart" and latest["started_at"] < control["at"])
        status = (mode if mode != "active" else "running" if running else "queued" if position
                  else "error" if error or failed else "scheduled" if pending or (
                      maintenance and (online or latest and latest['status'] == 'success')) else
                  "success" if latest and latest["status"] == "success" else "waiting" if online else "offline")
        if mode == 'active' and not running and not position and not service_enabled(store, 'executor'):
            status = 'paused'
        labels = {"paused": "Пауза запрошена" if running else "На паузе",
                  "stopped": "Остановка запрошена" if running else "Остановлена",
                  "error": "Ошибка", "running": "В работе", "scheduled": "Запланирована",
                  "queued": "Ожидает очереди",
                  "success": "Выполнена",
                  "waiting": "Ожидает запуска", "offline": "Ожидает запуска"}
        if mode == 'active' and not running and not position and not service_enabled(store, 'executor'):
            labels['paused'] = 'Обработка выключена'
        last_search = report['latest_check'] if maintenance else store.get_state('last_poll')
        search_enabled = monitor['enabled'] and mode != 'stopped'
        search_online = search_enabled and monitor['online'] and not (
            store.get_state(prefix + 'monitor_error') if maintenance else store.get_state('worker_error'))
        search_status = ('Поиск остановлен' if mode == 'stopped' else 'Поиск выключен' if not monitor['enabled']
                         else 'Поиск работает' if search_online else 'Поиск недоступен')
        next_job = min(pending, key=lambda job: job['due_at']) if pending else None
        cards.append({"task": task, "processor": processor, "enabled": True, "report": report,
            "last_run": latest, "snapshot": imports[0] if imports and imports[0]["kind"] == "import" else None,
            "next_job": next_job,
            "queued": len(pending), "online": online, "error": error,
            "pending": pending,
            "control": control, "running": running, "status": status, "status_label": labels[status],
            "queue_position": position, "progress": progress,
            "schedule": schedule, "schedule_fields": schedule_fields(settings, store, task.slug),
            "last_search": last_search,
            "next_search": last_search + schedule['search_minutes'] * 60 if last_search and search_enabled else None,
            "search_status": search_status, "search_online": search_online,
            "automatic_enabled": service_enabled(store, 'executor'),
            "manual_next": bool(next_job and store.run_request(job_key=next_job['key'])),
            "next_scheduled": next_task_time(settings, store, task.slug, now) if maintenance else None,
            "run_time": config["run_time"] if maintenance else None,
            "dry_run": bool(latest['dry_run']) if running else (
                not (settings.wiki_write and config["autosave"]) if maintenance else not settings.wiki_write),
            "month_end": next_month_end(settings, now, store) if processor.slug == "obkat" else None})
    groups = []
    for card in cards:
        group = next((group for group in groups if group["processor"] == card["processor"]), None)
        if group is None:
            group = {"processor": card["processor"], "cards": []}
            groups.append(group)
        group["cards"].append(card)
    return {"cards": cards, "groups": groups, "enabled_count": sum(card["enabled"] for card in cards),
        "total_problems": sum(card["report"]["problems"] for card in cards if card["report"]),
        "total_queued": sum(card["queued"] for card in cards), "now": now}
