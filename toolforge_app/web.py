import csv
import difflib
import hmac
import hashlib
import io
import json
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

import mwoauth
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for, g)

from .config import Settings
from .overview import OVERVIEW, build_overview
from .processors import PROCESSORS, TASKS, Processor, get_processor, get_task
from .processors.obkat.report import (CHECKS, build_report, fix_guidance,
                                      open_nominations, plain_title, report_txt, wiki_url)
from .storage import Store
from .processors.daily import TASK_SLUGS, fields, form_value, get_config, parse_form
from .processors.logs import public_run
from .console import ACTIVITY, console_data, run_history, run_metrics
from .schedules import save_schedule
from .health import system_status, component_health
from .connections import (ERRORS as CONNECTION_ERRORS, FIELDS as CONNECTION_FIELDS, RIGHTS,
                          connection_summary, effective_settings, probe_bot, probe_oauth,
                          record_check, save_credentials)
from .wiki import WikiError
from .runtime import execution_settings, service_enabled, writes_enabled

STATUS_LABELS = {"success": "Завершён", "failed": "Ошибка", "running": "В работе", "interrupted": "Прерван",
                 "paused": "Приостановлен", "stopped": "Остановлен"}
KIND_LABELS = {"page": "После правки", "full": "Ручной запуск", "month_end": "Конец месяца",
               "import": "Локальный снимок", "bootstrap": "Первичная синхронизация", "daily": "По расписанию",
               "recheck": "Проверка с нуля", "article": "Повтор статьи"}


def create_app(settings=None, store=None, *, admin_preview=False):
    settings = settings or Settings.from_env()
    if admin_preview and (settings.wiki_write or urlsplit(settings.public_url).scheme != "http"
                          or urlsplit(settings.public_url).hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("Admin preview requires a local HTTP URL and disabled wiki writes")
    store = store or Store(settings.database_url)
    app = Flask(__name__)
    app.secret_key = settings.secret_key or secrets.token_urlsafe(48)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        TEMPLATES_AUTO_RELOAD=admin_preview,
        SESSION_COOKIE_SECURE=settings.public_url.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=131072)
    app.extensions["store"] = store
    app.extensions["settings"] = settings

    def preview_enabled():
        return admin_preview and request.remote_addr in {"127.0.0.1", "::1"}

    def preview_active():
        return preview_enabled() and session.get("admin_preview") is True

    def preview_return():
        target = request.form.get("next", "/")
        if "\\" in target or not (target == "/" or target.startswith(("/?", "/processors/", "/runs/", "/console", "/notifications", "/admin"))):
            abort(400)
        return target

    if admin_preview:
        @app.route("/preview", methods=["POST"])
        def change_preview():
            if not preview_enabled():
                abort(404)
            mode = request.form.get("mode")
            if mode not in {"admin", "public"}:
                abort(400)
            target = preview_return()
            if mode == 'public' and target.startswith(('/notifications', '/admin')):
                target = '/'
            # This is a presentation preference, never an authenticated identity.
            session["admin_preview"] = mode == "admin"
            return redirect(target)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def authenticated_admin():
        return bool(settings.admin_username) and session.get("username") == settings.admin_username

    def admin_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if preview_active():
                flash("Это предпросмотр admin. Команды управления не выполняются.")
                slug = kwargs.get("slug")
                if slug in TASK_SLUGS:
                    return redirect(url_for("processor", slug=get_task(slug).processor, task=slug))
                if request.endpoint in {'connection_save', 'connection_check', 'service_control'} or request.form.get('return_to') == 'connections':
                    return redirect(url_for('connections_page'))
                return redirect(url_for("processor", slug="obkat") if request.endpoint in {"request_run", "task_schedule"}
                                or request.form.get("return_to") == "processor" else url_for("index"))
            if not authenticated_admin():
                abort(403)
            return fn(*args, **kwargs)
        return wrapped

    def full_logs():
        return preview_active() or authenticated_admin()

    def credentials(name=None):
        key = 'connection_settings_' + (name or 'all')
        if key not in g:
            setattr(g, key, effective_settings(settings, store, names=(name,) if name else None))
        return getattr(g, key)

    def health():
        if 'system_health' not in g:
            g.system_health = system_status(settings, store)
        return g.system_health

    def load_run(run_id):
        run = store.run(run_id)
        if not run:
            abort(404)
        for field in ("events", "summary", "report"):
            run[field] = json.loads(run[field])
        return run if full_logs() else public_run(run)

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            supplied = request.form.get("csrf", "")
            if not session.get("csrf") or not hmac.compare_digest(session["csrf"], supplied):
                abort(400, "Некорректный токен формы. Обновите страницу.")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store" if session else "no-cache"
        return response

    @app.template_filter("localtime")
    def localtime(value, fmt="%d.%m.%Y, %H:%M"):
        return datetime.fromtimestamp(value, settings.zone).strftime(fmt) if value else "—"

    @app.template_filter("countdown")
    def countdown(value):
        if not value:
            return ""
        seconds = max(0, int(value - time.time()))
        if seconds <= 0:
            return "Запуск ожидается"
        if seconds < 60:
            return "менее минуты"
        minutes = seconds // 60
        days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
        return "через " + " ".join(part for part in
            (f"{days} д" if days else "", f"{hours} ч" if hours else "", f"{minutes} мин" if minutes else "") if part)

    @app.template_filter('search_frequency')
    def search_frequency(minutes):
        if minutes is None:
            return '—'
        if minutes % 60 == 0:
            hours = minutes // 60
            return f'Каждые {hours} ч' if hours != 1 else 'Каждый час'
        return f'Каждые {minutes} мин' if minutes != 1 else 'Каждую минуту'

    @app.template_filter("duration")
    def duration(run):
        elapsed = max(0, (run["finished_at"] or time.time()) - run["started_at"])
        if elapsed < 1:
            return "< 1 с"
        seconds = int(elapsed)
        days, hours, minutes, seconds = seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60, seconds % 60
        return " ".join(part for part in (f"{days} д" if days else "", f"{hours} ч" if hours else "",
                                          f"{minutes} мин" if minutes else "", f"{seconds} с" if seconds else ""))

    @app.context_processor
    def context():
        heartbeat = store.get_state("worker_heartbeat", 0)
        preview = preview_active()
        return dict(processors=PROCESSORS, settings=execution_settings(settings, store), csrf_token=csrf_token,
            now=time.time(),
            username="admin" if preview else session.get("username"),
            is_admin=preview or authenticated_admin(),
            preview_enabled=preview_enabled(), preview_admin=preview,
            oauth_enabled=bool(settings.oauth_key and settings.oauth_secret),
            status_labels=STATUS_LABELS, kind_labels=KIND_LABELS, wiki_url=wiki_url,
            system_health=health(),
            worker_online=time.time() - heartbeat < max(300, settings.poll_seconds * 4),
            worker_heartbeat=heartbeat, worker_error=store.get_state("worker_error"))

    @app.route("/")
    def index():
        return render_template("overview.html", module=OVERVIEW, active="",
                               overview=build_overview(settings, store), recent=run_history(store, limit=8))

    @app.route("/runs/recent-fragment")
    def recent_runs_fragment():
        return render_template("recent_runs.html", recent=run_history(store, limit=8))

    @app.route("/tasks/overview-fragment")
    def overview_fragment():
        return render_template("task_list.html", overview=build_overview(settings, store))

    @app.route('/system/status-fragment')
    def system_fragment():
        return render_template('system_status.html')

    @app.route('/admin')
    @app.route('/admin/connections')
    def connections_page():
        if not full_logs():
            if session.get('username'):
                abort(403)
            session['login_next'] = '/admin'
            return redirect(url_for('login'))
        connection_error = ''
        try:
            current = credentials()
        except WikiError as exc:
            current = settings
            connection_error = CONNECTION_ERRORS[exc.code]
        return render_template('connections.html',
            module=Processor('connections', 'Подключения', 'Подключения', '', True),
            connections=connection_summary(current, store), connection_error=connection_error,
            bot_login=current.bot_login, rights=RIGHTS, database_kind=store.engine.dialect.name,
            overview=build_overview(settings, store), write_ceiling=settings.wiki_write,
            writes_allowed=writes_enabled(settings, store))

    @app.route('/admin/services/<name>', methods=['POST'])
    @admin_required
    def service_control(name):
        if name not in {'executor', 'monitor', 'writes'}:
            abort(404)
        action = request.form.get('action')
        if action not in {'enable', 'disable'}:
            abort(400)
        if action == 'enable':
            if name == 'writes':
                if not settings.wiki_write:
                    abort(409, 'Запись запрещена в настройках сервера. Сначала завершите проверку пробного запуска.')
                try:
                    current = credentials('bot')
                except WikiError:
                    abort(409, 'Сначала восстановите подключение бота.')
                if not connection_summary(current, store)['bot']['check'].get('verified'):
                    abort(409, 'Сначала проверьте подключение и права бота.')
            elif not component_health(settings, store, name)['online']:
                abort(409, 'Нет связи с процессом. Проверьте фоновое задание Toolforge.')
        store.set_state('service:' + name, dict(enabled=action == 'enable', at=time.time(), by=session['username']))
        flash(('Запись в Википедию' if name == 'writes' else 'Обработка задач' if name == 'executor' else 'Поиск изменений') +
              (' включена.' if action == 'enable' else ' выключена. Текущий запрос завершится; новые действия не начнутся.'))
        return redirect(url_for('connections_page'))

    @app.route('/admin/status-fragment')
    def admin_status_fragment():
        if not full_logs():
            abort(403)
        return render_template('admin_status.html', overview=build_overview(settings, store),
            database_kind=store.engine.dialect.name, write_ceiling=settings.wiki_write,
            writes_allowed=writes_enabled(settings, store))

    def check_connection(name, current):
        # One shared cooldown across web instances prevents repeated login attempts.
        with store.worker_lease(processor='connection-check') as renew:
            if not renew:
                abort(409, 'Проверка подключения уже выполняется.')
            at = time.time()
            if at - store.get_state('connection:last_attempt:' + name, 0) < 30:
                abort(409, 'Подождите 30 секунд перед следующей проверкой подключения.')
            store.set_state('connection:last_attempt:' + name, at)
            result = (probe_bot if name == 'bot' else probe_oauth)(current)
            record_check(current, store, name, result)
        return result

    @app.route('/admin/connections/<name>/check', methods=['POST'])
    @admin_required
    def connection_check(name):
        if name not in CONNECTION_FIELDS:
            abort(404)
        try:
            result = check_connection(name, credentials(name))
            flash('Подключение проверено.' if result['verified'] else CONNECTION_ERRORS.get(result['code'], CONNECTION_ERRORS['api']))
        except WikiError as exc:
            flash(CONNECTION_ERRORS.get(exc.code, CONNECTION_ERRORS['api']))
        return redirect(url_for('connections_page') + '#' + name)

    @app.route('/admin/connections/<name>/save', methods=['POST'])
    @admin_required
    def connection_save(name):
        if name not in CONNECTION_FIELDS:
            abort(404)
        fields = CONNECTION_FIELDS[name]
        values = {field: request.form.get(field, '').strip() for field in fields}
        if any(not value or len(value) > 256 or any(c in value for c in '\r\n\x00') for value in values.values()):
            flash('Заполните оба поля. Введите данные без переносов строк.')
            return redirect(url_for('connections_page') + '#' + name)
        from dataclasses import replace
        try:
            current = credentials(name)
        except WikiError:
            current = settings
        candidate = replace(current, **values)
        result = check_connection(name, candidate)
        if result['verified']:
            save_credentials(settings, store, name, values)
            flash('Данные сохранены. Новое подключение проверено; режим записи не изменён.')
        else:
            # Keep working credentials after a failed replacement. The receipt is
            # scoped to the submitted candidate and is not mistaken for live status.
            flash(CONNECTION_ERRORS.get(result['code'], CONNECTION_ERRORS['api']))
        return redirect(url_for('connections_page') + '#' + name)

    @app.route('/notifications')
    def notifications_page():
        if not full_logs():
            abort(403)
        health()
        try:
            page = int(request.args.get('page', 1))
            if not 1 <= page <= 100000:
                raise ValueError()
        except ValueError:
            abort(400)
        entries = store.list_notifications(41, (page - 1) * 40)
        return render_template('notifications.html', module=Processor('notifications', 'Уведомления', 'Уведомления', '', True), entries=entries[:40],
                               page=page, has_next=len(entries) > 40)

    @app.route('/admin/notifications/<notification_id>/read', methods=['POST'])
    @admin_required
    def notification_read(notification_id):
        if not store.read_notification(notification_id, time.time()):
            abort(404)
        return redirect(url_for('notifications_page'))

    @app.route('/admin/tasks/<slug>/retry-article', methods=['POST'])
    @admin_required
    def retry_article(slug):
        task = get_task(slug)
        if not task or task.processor not in {'translations', 'sections'}:
            abort(404)
        if store.control(slug)['mode'] != 'active':
            abort(409, 'Возобновите задачу перед повторной проверкой.')
        title = request.form.get('title', '')
        from .processors.daily import report as task_report
        if not any(row['title'] == title for row in task_report(store, slug)['skipped_articles']):
            abort(409, 'Статья больше не находится в текущих пропусках.')
        key = 'article:' + hashlib.sha256(title.encode()).hexdigest()
        created = store.enqueue(key, 'article', time.time(), title=title, processor=slug,
                                requested_by=session['username'])
        flash('Статья возвращена в очередь. Остальные отметки сохранены.' if created else 'Статья уже в очереди.')
        return redirect(url_for('processor', slug=task.processor, task=slug, view='skipped') + '#reports')

    @app.route("/console")
    @app.route("/console/fragment", endpoint="console_fragment")
    def work_console():
        selected = request.args.get("task", "")
        if selected and selected not in {"obkat", *TASK_SLUGS}:
            abort(400)
        day = request.args.get("day", "")
        status = request.args.get("status", "")
        if status and status not in STATUS_LABELS:
            abort(400)
        started_from, started_until = None, None
        try:
            page = int(request.args.get("page", 1))
            if not 1 <= page <= 100000:
                raise ValueError()
            if day:
                date = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=settings.zone)
                started_from, started_until = date.timestamp(), (date + timedelta(days=1)).timestamp()
        except ValueError:
            abort(400)
        template = "console_feed.html" if request.path.endswith("/fragment") else "console.html"
        return render_template(template, module=ACTIVITY, selected=selected, day=day, status=status, page=page,
            live=page == 1 and (not day or day == datetime.now(settings.zone).strftime("%Y-%m-%d")),
            console=console_data(store, selected, full=full_logs(), page=page,
                                 started_from=started_from, started_until=started_until, status=status))

    @app.route("/tasks/<slug>/status-fragment")
    def task_status_fragment(slug):
        task = get_task(slug)
        if not task or not task.enabled:
            abort(404)
        card = next(card for card in build_overview(settings, store)["cards"] if card["task"].slug == slug)
        config = get_config(store, slug) if slug in TASK_SLUGS else None
        return render_template("processing_daily.html" if config else "processing_obkat.html",
            task=task, task_state=card, control=card["control"], config=config, report=card["report"],
            queue=card["pending"])

    @app.route("/tasks/<slug>")
    def task_page(slug):
        task = get_task(slug)
        if not task:
            abort(404)
        return redirect(url_for("processor", slug=task.processor, task=task.slug))

    @app.route("/processors/<slug>")
    def processor(slug):
        module = get_processor(slug)
        if not module:
            abort(404)
        if not module.enabled:
            task = get_task(request.args.get("task"))
            if task and task.processor != slug:
                abort(404)
            active = request.args.get("view", "problems")
            if active not in {"problems", "logs"}:
                abort(404)
            return render_template("planned.html", module=module, task=task, active=active,
                                   history=[], history_page=1, history_has_next=False)
        if slug in {"maintenance", "translations", "sections"}:
            actions = [task for task in TASKS if task.processor == slug and task.enabled]
            task_slug = request.args.get("task", actions[0].slug)
            if task_slug not in {task.slug for task in actions}:
                abort(404)
            task_state = next(card for card in build_overview(settings, store)["cards"] if card["task"].slug == task_slug)
            try:
                history_page = max(1, int(request.args.get("history_page", 1)))
                page_number = max(1, int(request.args.get("page", 1)))
            except ValueError:
                abort(400)
            history = store.list_runs(26, offset=(history_page - 1) * 25, processor=task_slug)
            for run in history:
                run["summary"] = json.loads(run["summary"])
            config = get_config(store, task_slug)
            report = task_state["report"]
            active = request.args.get("view", "problems" if report["problems"] else "pending")
            if active not in ({"problems", "pending", "skipped", "logs"} if slug in {"translations", "sections"} else {"problems", "pending", "logs"}):
                abort(404)
            result_items = report["pending_articles"] if active == "pending" else report["skipped_articles"] if active == "skipped" else report["manual"]
            config_fields = [dict(key=key, label=label, kind=kind, group=group, value=form_value(config, key, kind))
                             for key, label, kind, group in fields(task_slug)]
            return render_template("daily.html", module=module, active=active, task=task_state["task"], actions=actions,
                task_state=task_state, control=task_state["control"], report=report, config=config,
                config_fields=config_fields, history=history[:25], history_page=history_page,
                history_has_next=len(history) > 25, items=result_items[(page_number - 1) * 40:page_number * 40],
                page_number=page_number, has_next=len(result_items) > page_number * 40,
                config_updated=store.get_state(task_slug + ":config_updated"))
        task_state = build_overview(settings, store)["cards"][0]
        report = task_state["report"]
        active = request.args.get("view", "problems")
        active = {"nuances": "open"}.get(active, active)
        if active not in {"problems", "open", "logs"}:
            abort(404)
        query = request.args.get("q", "").strip()
        kind = request.args.get("type", "")
        month = request.args.get("month", "")
        opened = open_nominations(report)
        items = opened if active == "open" else [i for i in report["items"] if i["section"] == "problems"]
        labels = {"no_itog": "Без подитогов", "sub_itog_no_main": "С подитогами"} if active == "open" else CHECKS
        available_types = [(key, labels.get(key, key), sum(i["type"] == key for i in items)) for key in labels
            if any(i["type"] == key for i in items)]
        available_months = sorted({i["month"] for i in items}, reverse=True)
        items = [i for i in items if (not kind or i["type"] == kind) and (not month or i["month"] == month)
            and (not query or query.casefold() in (i["display_title"] + i["label"] + i["detail"]).casefold())]
        try:
            page_number = max(1, int(request.args.get("page", 1)))
            history_page = max(1, int(request.args.get("history_page", 1)))
        except ValueError:
            abort(400)
        total = len(items)
        history = store.list_runs(limit=26, offset=(history_page - 1) * 25)
        for run in history:
            run["summary"] = json.loads(run["summary"])
        for item in items:
            if active == "problems":
                item["fix"] = fix_guidance(item)
                item["subject"] = plain_title(item.get("parent_title") or item["display_title"])
                item["context"] = "Раздел: " + item["display_title"] if item.get("parent_title") else item["detail"]
        return render_template("processor.html", module=module, active=active, report=report,
            items=items if active == "open" else items[(page_number - 1) * 40:page_number * 40], total=total, query=query, kind=kind, month=month,
            available_types=available_types, available_months=available_months, history=history[:25],
            page_number=page_number, has_next=active == "problems" and total > page_number * 40,
            history_page=history_page, history_has_next=len(history) > 25, open_count=len(opened),
            queue=task_state["pending"], task_state=task_state,
            control=store.control(),
            local_snapshot=any(p["origin"] == "import" for p in store.all_pages()))

    @app.route("/runs/<run_id>")
    def run_detail(run_id):
        run = load_run(run_id)
        task = get_task(run["processor"])
        module = get_processor(task.processor) if task else get_processor(run["processor"])
        for event in run["events"]:
            if "diff_before" in event:
                event["diff"] = "".join(difflib.unified_diff(event["diff_before"].splitlines(True),
                    event["diff_after"].splitlines(True), fromfile="До обработки", tofile="После обработки"))
        return render_template("run.html", module=module, task=task, active="logs", run=run,
                               metrics=run_metrics(run), log_text=plain_log(run))

    def plain_log(run):
        lines = [localtime(run["started_at"]) + " · " + KIND_LABELS.get(run["kind"], run["kind"]),
                 STATUS_LABELS.get(run["status"], run["status"]) + (" · Режим проверки" if run["dry_run"] else ""), ""]
        for event in run["events"]:
            lines.append(localtime(event["at"], "%H:%M:%S") + "  " + event["message"])
            if event.get("title"):
                lines.append("          " + event["title"])
            if event.get("revision"):
                lines.append("          Версия: " + str(event["revision"]))
            if event.get("error"):
                lines.append("          Ошибка: " + event["error"])
            if "diff_before" in event:
                lines.append("".join(difflib.unified_diff(event["diff_before"].splitlines(True),
                    event["diff_after"].splitlines(True), fromfile="До обработки", tofile="После обработки")))
            details = {key: value for key, value in event.items() if key not in
                       {"at", "code", "message", "title", "revision", "error", "diff_before", "diff_after", "diff"}}
            if details and run["processor"] in TASK_SLUGS and full_logs():
                lines.append(json.dumps(details, ensure_ascii=False, indent=2))
            lines.append("")
        if not run["events"] and run["processor"] in TASK_SLUGS:
            lines.append("Сохранённых изменений в статьях нет.")
        return "\n".join(lines)

    @app.route("/runs/<run_id>/log.txt")
    def export_text_log(run_id):
        run = load_run(run_id)
        return Response(plain_log(run), content_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{run["processor"]}-{run_id}.txt"'})

    @app.route("/reports/obkat.<extension>")
    def export_report(extension):
        run_id = request.args.get("run")
        if run_id:
            run = store.run(run_id)
            if not run or run["processor"] != "obkat":
                abort(404)
            report = json.loads(run["report"])
            if not report:
                abort(409)
        else:
            report = build_report(store.all_pages())
        if extension == "json":
            content, mime = json.dumps(report, ensure_ascii=False, indent=2), "application/json"
        elif extension == "txt":
            content, mime = report_txt(report), "text/plain"
        elif extension == "csv":
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            fields = ("section", "label", "month", "line", "display_title", "detail", "url", "revision")
            writer.writerow(fields)
            for item in report["items"]:
                # Spreadsheet programs must treat wiki headings as text.
                writer.writerow("'" + str(item[k]) if str(item[k]).lstrip().startswith(("=", "+", "-", "@")) else item[k] for k in fields)
            content, mime = "\ufeff" + stream.getvalue(), "text/csv"
        else:
            abort(404)
        response = Response(content, content_type=mime + "; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="obkat.{extension}"'
        return response

    @app.route("/runs/<run_id>/log.json")
    def export_log(run_id):
        run = load_run(run_id)
        return Response(json.dumps(run, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{run["processor"]}-{run_id}.json"'})

    @app.route("/runs/<run_id>/table.wiki")
    def export_table(run_id):
        run = store.run(run_id)
        if not run or run["processor"] != "obkat":
            abort(404)
        return Response(run["table_text"], content_type="text/plain; charset=utf-8")

    @app.route("/admin/obkat/run", methods=["POST"])
    @admin_required
    def request_run():
        if store.control()["mode"] != "active":
            abort(409, "Возобновите или перезапустите задачу перед ручным запуском.")
        mode = request.form.get("mode", "normal")
        if mode not in {"normal", "spacing"}:
            abort(400)
        key = "manual:" + mode
        created = store.enqueue(key, "full", time.time(), spacing=mode == "spacing", requested_by=session["username"])
        flash("Запуск добавлен в очередь; свободный обработчик подхватит его в течение нескольких секунд." if created else "Такой запуск уже находится в очереди.")
        return redirect(url_for("processor", slug="obkat", view="logs") + "#history")

    @app.route("/admin/tasks/<slug>/control", methods=["POST"])
    @admin_required
    def task_control(slug):
        task = get_task(slug)
        if not task:
            abort(404)
        module = get_processor(task.processor)
        if not task.enabled:
            abort(409, "Задача ещё не подключена.")
        action = request.form.get("action")
        if action not in {"pause", "resume", "stop", "restart"}:
            abort(400)
        store.change_control(task.slug, action, session["username"])
        flash({"pause": "Пауза включена. Очередь сохранена; текущий запрос к Википедии завершится.",
               "resume": "Задача возобновлена. Ожидающие задания будут продолжены.",
               "stop": "Задача остановлена, ожидающие задания отменены. Текущий запрос к Википедии завершится.",
               "restart": "Задача перезапущена: запланирована проверка новых статей." if task.processor in {"translations", "sections"}
                          else "Задача перезапущена: запланирована полная обработка."}[action])
        if request.form.get('return_to') == 'connections':
            return redirect(url_for('connections_page') + '#tasks')
        return redirect(url_for("processor", slug=module.slug, task=task.slug) if request.form.get("return_to") == "processor"
                        else url_for("index"))

    @app.route("/admin/maintenance/<slug>/run", methods=["POST"])
    @app.route("/admin/tasks/<slug>/run", methods=["POST"], endpoint="task_run")
    @admin_required
    def maintenance_run(slug):
        if slug not in TASK_SLUGS:
            abort(404)
        if store.control(slug)["mode"] != "active":
            abort(409, "Возобновите задачу перед ручным запуском.")
        mode = request.form.get("mode", "normal")
        if mode not in {"normal", "recheck"} or mode == "recheck" and get_task(slug).processor not in {"translations", "sections"}:
            abort(400)
        created = store.enqueue("manual:" + mode, "recheck" if mode == "recheck" else "full", time.time(),
                                processor=slug, requested_by=session["username"])
        flash(("Проверка с нуля добавлена в очередь. История запусков сохранится." if mode == "recheck"
               else "Запуск добавлен в очередь.") if created else "Запуск уже находится в очереди.")
        return redirect(url_for("processor", slug=get_task(slug).processor, task=slug, view="logs") + "#history")

    @app.route("/admin/maintenance/<slug>/settings", methods=["POST"])
    @app.route("/admin/tasks/<slug>/settings", methods=["POST"], endpoint="task_settings")
    @admin_required
    def maintenance_settings(slug):
        if slug not in TASK_SLUGS:
            abort(404)
        previous = get_config(store, slug)
        try:
            values = dict(request.form)
            values.setdefault("run_time", previous["run_time"])
            config = parse_form(values, slug)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("processor", slug=get_task(slug).processor, task=slug) + "#settings")
        if "section_templates" in config and config["section_templates"] != previous["section_templates"]:
            config["section_templates_auto"] = False
        store.patch_state(slug + ":config", {key: value for key, value in config.items() if key != "run_time"})
        if "run_time" in request.form:
            save_schedule(settings, store, slug, {"run_time": config["run_time"]}, session["username"], partial=True)
        store.set_state(slug + ":config_updated", dict(at=time.time(), by=session["username"]))
        flash("Настройки сохранены. Они применятся со следующего запуска.")
        return redirect(url_for("processor", slug=get_task(slug).processor, task=slug) + "#settings")

    @app.route("/admin/maintenance/<slug>/schedule", methods=["POST"])
    @app.route("/admin/tasks/<slug>/schedule", methods=["POST"], endpoint="task_schedule")
    @admin_required
    def maintenance_schedule(slug):
        task = get_task(slug)
        if not task or not task.enabled or not task.schedules or request.endpoint == "maintenance_schedule" and slug not in TASK_SLUGS:
            abort(404)
        target = url_for("processor", slug=task.processor, **({"task": slug} if slug in TASK_SLUGS else {}))
        try:
            save_schedule(settings, store, slug, {key: value for key, value in request.form.items() if key != "csrf"}, session["username"])
        except ValueError as exc:
            flash(str(exc))
            return redirect(target)
        flash("Расписание сохранено. Таймеры обновятся при следующей проверке очереди.")
        return redirect(target)

    @app.route("/admin/maintenance/<slug>/refresh", methods=["POST"])
    @app.route("/admin/tasks/<slug>/refresh", methods=["POST"], endpoint="task_refresh")
    @admin_required
    def maintenance_refresh(slug):
        if slug not in TASK_SLUGS:
            abort(404)
        mode = request.form.get("mode", "inventory")
        if (mode not in {"inventory", "sections", "redirects"}
                or mode == "sections" and "section_templates" not in get_config(store, slug)
                or mode == "redirects" and get_task(slug).processor != "sections"):
            abort(400)
        store.enqueue("refresh:" + slug, mode, time.time(), title=slug, processor=get_task(slug).processor + "-monitor",
                      requested_by=session["username"])
        flash("Запрошено обновление счётчиков." if mode == "inventory" else "Запрошено обновление перенаправлений." if mode == "redirects" else "Запрошено обновление списка шаблонов из категории.")
        return redirect(url_for("processor", slug=get_task(slug).processor, task=slug))

    @app.route("/login")
    def login():
        try:
            current = credentials('oauth')
        except WikiError:
            flash(CONNECTION_ERRORS['credentials-unreadable'])
            return redirect(url_for('index'))
        if not current.oauth_key or not current.oauth_secret:
            flash("Вход через Википедию будет доступен после подключения OAuth на Toolforge.")
            return redirect(url_for("index"))
        store.prune_oauth_requests(time.time())
        consumer = mwoauth.ConsumerToken(current.oauth_key, current.oauth_secret)
        try:
            auth_url, token = mwoauth.initiate(current.oauth_url, consumer,
                callback=settings.public_url.rstrip("/") + url_for("oauth_callback"), user_agent=settings.user_agent)
        except Exception:
            flash("Не удалось связаться с сервисом входа Википедии. Попробуйте позднее.")
            return redirect(url_for("index"))
        pending_id = secrets.token_urlsafe(32)
        # Request secrets remain server-side, never in the signed but readable cookie.
        store.set_state("oauth:" + pending_id, {"key": token.key, "secret": token.secret, "expires": time.time() + 600})
        session["oauth_pending"] = pending_id
        return redirect(auth_url)

    @app.route("/oauth/callback")
    def oauth_callback():
        pending_id = session.pop("oauth_pending", "")
        pending = store.pop_state("oauth:" + pending_id) if pending_id else None
        if not pending or pending["expires"] < time.time() or not hmac.compare_digest(pending["key"], request.args.get("oauth_token", "")):
            abort(400, "Сеанс входа истёк. Начните вход заново.")
        try:
            current = credentials('oauth')
            consumer = mwoauth.ConsumerToken(current.oauth_key, current.oauth_secret)
            access = mwoauth.complete(settings.oauth_url, consumer,
                mwoauth.RequestToken(pending["key"], pending["secret"]), request.query_string.decode("ascii"), user_agent=settings.user_agent)
            identity = mwoauth.identify(settings.oauth_url, consumer, access, user_agent=settings.user_agent)
        except Exception:
            flash("Википедия не подтвердила вход. Попробуйте снова.")
            return redirect(url_for("index"))
        target = session.get('login_next', '/')
        session.clear()
        if not settings.admin_username or identity.get("username") != settings.admin_username:
            flash("Вход доступен только администратору. Отчёты можно читать без входа.")
            return redirect(url_for("index"))
        session["username"] = identity["username"]
        session.permanent = True
        record_check(current, store, 'oauth', dict(verified=True, code='', login_verified=True))
        return redirect('/admin' if target == '/admin' else url_for("index"))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz():
        # Web health is independent of worker health and network availability.
        store.get_state("worker_heartbeat")
        return {"status": "ok"}

    @app.route('/healthz/workers/<component>')
    def worker_healthz(component):
        if component not in {'executor', 'monitor'}:
            abort(404)
        state = component_health(settings, store, component)
        return {'status': 'ok' if state['online'] else 'unavailable', 'last_signal': state['at']}, 200 if state['online'] else 503

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def error_page(error):
        return render_template("error.html", code=error.code,
            module=get_processor("obkat"), active=""), error.code

    return app
