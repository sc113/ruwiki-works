import argparse
import time
from pathlib import Path

from .config import Settings
from .processors.obkat.analyzer import analyze_text
from .processors.obkat.report import build_report, page_title
from .processors.obkat.table import generate_wiki_table
from .storage import Store, dump
from .web import create_app
from .worker import Worker
from .processors.maintenance import TASK_SLUGS
from .processors.maintenance.service import InventoryMonitor, MaintenanceWorker
from .dispatcher import Dispatcher, Monitor
from .processors.translations import TASK_SLUGS as TRANSLATION_TASKS
from .processors.translations.service import TranslationWorker, TranslationMonitor
from .processors.translations.history import import_logs
from .processors.sections import TASK_SLUGS as SECTION_TASKS
from .processors.sections.service import SectionWorker, SectionMonitor
from .processors.sections.history import import_legacy


def import_snapshot(source, store):
    directory = Path(source) / "wiki_pages"
    files = sorted(directory.glob("????-??.txt"))
    if not files:
        raise ValueError("No monthly pages in wiki_pages")
    # A local import must never silently replace a live site's state.
    if store.all_pages():
        raise ValueError("Import requires an empty database; use a separate local database for previews")
    run_id = store.start_run("import", "local", dry_run=True)
    now = time.time()
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        store.save_page(page_title(path.stem), month=path.stem, revision=0,
            edited_at=0, checked_at=now, text=text, issues=dump(analyze_text(text)),
            origin="import", missing=False)
    rows = store.all_pages()
    report = build_report(rows)
    store.finish_run(run_id, [{"at": now, "code": "import", "message": f"Импортировано страниц: {len(files)}",
        "title": ""}], {"checked": len(files), "changed": 0, "proposed": 0, "deferred": 0,
        "problems": report["problems"], "nuances": report["nuances"]}, report,
        generate_wiki_table({p["month"]: p["text"] for p in rows}))
    print(f"Imported {len(files)} local pages. No wiki requests or edits.")


def main():
    parser = argparse.ArgumentParser(description="Russian Wikipedia bot tasks and monitoring interface")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--admin-preview", action="store_true", help="Local read-only preview of the administrator interface")
    commands.add_parser("init-db")
    storage = commands.add_parser('maintain-storage', help='Compress reports and expire old diagnostics; preserve public history')
    storage.add_argument('--batch-size', type=int, default=100)
    check = commands.add_parser('check-health', help='Read-only health probe for background jobs')
    check.add_argument('component', choices=('executor', 'monitor'))
    connection = commands.add_parser('check-connection', help='Read-only authentication check; never enables wiki writes')
    connection.add_argument('name', choices=('bot', 'oauth'))
    clear = commands.add_parser('clear-connection', help='Remove a web-saved override and use server environment credentials')
    clear.add_argument('name', choices=('bot', 'oauth'))
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--task", choices=("all", "monitor", "obkat", *TASK_SLUGS, *TRANSLATION_TASKS, *SECTION_TASKS, "maintenance-monitor", "translations-monitor", "sections-monitor"), default="all")
    commands.add_parser("refresh-maintenance", help="Read-only category counts and section template lists")
    commands.add_parser("refresh-translations", help="Read-only counts for both translation actions")
    translation_logs = commands.add_parser("import-translation-logs", help="Import checked titles from translation TSV records")
    translation_logs.add_argument("source")
    commands.add_parser("refresh-sections", help="Read-only transclusion counts for both section actions")
    section_logs = commands.add_parser("import-section-logs", help="Import section processing records and inventory caches")
    section_logs.add_argument("source")
    run = commands.add_parser("run-obkat")
    run.add_argument("--spacing", action="store_true")
    snapshot = commands.add_parser("import-obkat")
    snapshot.add_argument("source")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.database_url)
    if args.command == 'maintain-storage':
        import json
        from .log_storage import maintain_storage, storage_usage
        with store.worker_lease(processor='storage-maintenance') as renew:
            if not renew:
                parser.error('Storage maintenance is already running')
            result = maintain_storage(store, retention_days=settings.log_retention_days, batch_size=args.batch_size)
            result.update(storage_usage(store), checked_at=time.time(), retention_days=settings.log_retention_days)
            store.set_state('storage:maintenance', result)
            store.set_state('storage:maintenance_error', None)
            print(json.dumps(result))
    elif args.command == 'clear-connection':
        store.clear_connection_secret(args.name)
        store.pop_state('connection:' + args.name)
        print('Saved override removed. Server environment credentials will be used; wiki write mode unchanged.')
    elif args.command == 'check-connection':
        from .connections import effective_settings, probe_bot, probe_oauth, record_check
        from .wiki import WikiError
        import json
        try:
            current = effective_settings(settings, store)
            result = (probe_bot if args.name == 'bot' else probe_oauth)(current)
            record_check(current, store, args.name, result)
        except WikiError as exc:
            result = dict(verified=False, code=exc.code)
        print(json.dumps(result))
        raise SystemExit(0 if result['verified'] else 1)
    elif args.command == 'check-health':
        from .health import component_health
        state = component_health(settings, store, args.component)
        print(args.component + ': ' + ('ok' if state['online'] else 'unavailable'))
        raise SystemExit(0 if state['online'] else 1)
    elif args.command == "serve":
        create_app(settings, store, admin_preview=args.admin_preview).run(host="127.0.0.1", port=args.port, debug=False)
    elif args.command == "worker":
        instance = (Dispatcher(settings, store) if args.task == "all" else Monitor(settings, store) if args.task == "monitor"
                    else Worker(settings, store) if args.task == "obkat" else InventoryMonitor(settings, store)
                    if args.task == "maintenance-monitor" else TranslationMonitor(settings, store) if args.task == "translations-monitor"
                    else SectionMonitor(settings, store) if args.task == "sections-monitor"
                    else SectionWorker(settings, store, args.task) if args.task in SECTION_TASKS
                    else TranslationWorker(settings, store, args.task) if args.task in TRANSLATION_TASKS else MaintenanceWorker(settings, store, args.task))
        instance.tick() if args.once else instance.forever()
    elif args.command == "refresh-maintenance":
        InventoryMonitor(settings, store).tick(force=True)
        for slug in TASK_SLUGS:
            inventory = store.get_state(slug + ":inventory")
            error = store.get_state(slug + ":monitor_error")
            print(slug + ": " + ("error " + error["code"] if error else
                  str(inventory["total"]) + " articles, " + str(len(inventory["categories"])) + " categories" if inventory else "no data"))
    elif args.command == "run-obkat":
        if store.control()["mode"] != "active":
            parser.error("Task is paused or stopped; resume or restart it on the website first.")
        store.enqueue("manual:" + ("spacing" if args.spacing else "normal"), "full", time.time(),
            spacing=args.spacing, requested_by="cli")
        print("Queued. Start the worker to process the request.")
    elif args.command == "refresh-translations":
        TranslationMonitor(settings, store).tick(force=True)
        for slug in TRANSLATION_TASKS:
            inventory = store.get_state(slug + ":inventory")
            error = store.get_state(slug + ":monitor_error")
            print(slug + ": " + ("error " + error["code"] if error else str(inventory["total"]) + " articles" if inventory else "no data"))
    elif args.command == "refresh-sections":
        SectionMonitor(settings, store).tick(force=True)
        for slug in SECTION_TASKS:
            inventory = store.get_state(slug + ":inventory")
            error = store.get_state(slug + ":monitor_error")
            print(slug + ": " + ("error " + error["code"] if error else str(inventory["total"]) + " articles" if inventory else "no data"))
    elif args.command == "import-section-logs":
        for slug, counts in import_legacy(args.source, store).items():
            print(f"{slug}: {counts['total']} checked titles, {counts['added']} added. No wiki requests or edits.")
    elif args.command == "import-obkat":
        import_snapshot(args.source, store)
    elif args.command == "import-translation-logs":
        for slug, counts in import_logs(args.source, store).items():
            print(f"{slug}: {counts['total']} checked titles, {counts['added']} added. No wiki requests or edits.")
    else:
        print("Database initialized.")


if __name__ == "__main__":
    main()
