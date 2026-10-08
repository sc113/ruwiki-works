"""Daily independent jobs and a separate configurable inventory monitor."""
import copy
import time

import mwparserfromhell

from ...wiki import RUN_ERRORS, WikiClient, WikiError, bot_edit_block, require_bot_permission
from ..daily_worker import DailyWorker, Controlled, MOSCOW
from ...schedules import next_daily_time, search_interval
from ...run_statistics import report_changes, report_snapshot
from ...edit_comments import dates_comment, rq_comment, unwrap_comment, fit_comment
from . import TASK_SLUGS
from .config import get_config
from .history import History, normalize
from .inventory import Catalogue, ERROR_LABELS, load_sections, refresh_inventory, report, source_category
from .transform import clean_value, update_dates, update_rq
from .unwrap import unwrap_rq



def next_daily(config, now):
    return next_daily_time(config["run_time"], now)


class MaintenanceWorker(DailyWorker):
    def __init__(self, settings, store, slug, wiki=None):
        if slug not in TASK_SLUGS:
            raise ValueError("Unknown maintenance task")
        super().__init__(settings, store, slug, wiki)

    def _execute(self, job):
        control = self.store.control(self.slug)
        if control["mode"] != "active" or not any(pending == job for pending in self.store.queue(self.slug)):
            return
        config = copy.deepcopy(get_config(self.store, self.slug))
        dry_run = not (self.settings.wiki_write and config["autosave"])
        self.generation = control["generation"]
        self.events = []
        self.run_id = self.store.start_run(job["kind"], job["requested_by"], dry_run=dry_run, processor=self.slug, job=job)
        summary = dict(checked=0, changed=0, proposed=0, skipped=0, errors=0, remaining=None)
        before_report = report_snapshot(report(self.store, self.slug, config))
        errors, unresolved, excluded, notes_by_title = {}, {}, {}, {}
        controlled, failed = None, False
        result_report = {}
        try:
            self.event("started", "Начало обработки", configuration=config, job={key: job[key] for key in ("key", "revision", "due_at", "spacing")})
            if "section_templates" in config:
                config["section_templates"] = load_sections(self.wiki, self.store, self.slug, config)
                self.event("section_templates", f"Шаблонов для разделов: {len(config['section_templates'])}", templates=config["section_templates"])
            self.event("scan", "Поиск статей для обработки")
            before = refresh_inventory(self.wiki, self.store, self.slug, config=config)
            self.event("inventory", f"Статей в категориях: {before['total']}", categories=before["categories"])
            allowed = set(before["articles"])
            selected = set(allowed)
            if self.slug == "maintenance-rq" and config["rq_start_prefix"]:
                selected &= {row["title"] for row in self.wiki.category_members(config["rq_category"],
                              start_prefix=config["rq_start_prefix"])}
            catalogue = Catalogue(self.wiki, self.store)
            by_category = {}
            rq = catalogue.aliases("Rq") if self.slug in {"maintenance-rq", "maintenance-rq-unwrap"} and selected else None
            if self.slug == "maintenance-dates":
                for category in before["categories"]:
                    if not category["count"]:
                        continue
                    self.checkpoint()
                    try:
                        by_category[category["title"]] = catalogue.category_templates(category["title"])
                        self.event("category_templates", "Определены шаблоны категории", title=category["title"],
                                   templates=by_category[category["title"]])
                    except WikiError as exc:
                        if exc.code in RUN_ERRORS:
                            raise
                        summary["errors"] += 1
                        self.event("error", ERROR_LABELS.get(exc.code, "Ошибка определения шаблонов категории"),
                                   title=category["title"], error=exc.code)
                        errors.update({title: ERROR_LABELS.get(exc.code, "Ошибка категории") for title in category["articles"]})
            for title in sorted(selected):
                self.checkpoint()
                summary["checked"] += 1
                try:
                    base = self.wiki.fetch(title)
                    if base.missing:
                        raise WikiError("article-missing")
                    require_bot_permission(base.text, self.settings.bot_username)
                    self.event("article", "Начало обработки статьи", title=title, revision=base.revision)
                    history = History(self.wiki, base, config, lambda code, message, **details:
                                      self.event(code, message, title=title, **details)) if self.slug != "maintenance-rq-unwrap" else None
                    if self.slug == "maintenance-dates":
                        definitions = {item["name"]: item for category in before["articles"][title]
                                       for item in by_category.get(category, [])}
                        if not definitions:
                            raise WikiError("category-templates-missing")
                        text, changes, notes = update_dates(base, list(definitions.values()), config, history,
                            lambda code, message, **details: self.event(code, message, title=title, **details))
                    elif self.slug == "maintenance-rq":
                        rq_names = {normalize(name) for name in rq["aliases"]}
                        used = {config["rq_param_templates"][clean_value(p.value)] for t in mwparserfromhell.parse(base.text).filter_templates()
                                if normalize(t.name) in rq_names for p in t.params if str(p.name).strip().isdigit()
                                and clean_value(p.value) in config["rq_param_templates"]}
                        definitions = {item["name"]: item for item in (catalogue.aliases(name) for name in sorted(used))}
                        text, changes, notes = update_rq(base, list(definitions.values()), rq, config, history,
                            lambda code, message, **details: self.event(code, message, title=title, **details))
                    else:
                        text, changes, notes = unwrap_rq(base, rq, config,
                            lambda code, message, **details: self.event(code, message, title=title, **details))
                    notes_by_title[title] = notes
                    if text == base.text:
                        summary["skipped"] += 1
                        reason = '; '.join(notes) or ('В RQ нет параметров, подходящих для замены'
                            if self.slug == 'maintenance-rq' and not used else 'Изменения не требуются')
                        unresolved[title] = reason if reason != 'Изменения не требуются' else (
                            'Не найдено изменений для очистки категории; проверьте шаблоны и их параметры')
                        self.event("unchanged", reason, title=title)
                        continue
                    self.checkpoint()
                    edit_summary = {'maintenance-dates': dates_comment, 'maintenance-rq': rq_comment,
                                    'maintenance-rq-unwrap': unwrap_comment}[self.slug](changes)
                    self.event('edit_summary', 'Описание правки: ' + edit_summary, title=title,
                               edit_summary=fit_comment(edit_summary))
                    if dry_run:
                        summary["proposed"] += 1
                        code, message, revision = "would_edit", "Подготовлены изменения без сохранения", base.revision
                    else:
                        revision = self.wiki.edit_article(base, text, fit_comment(edit_summary), allowed)
                        summary["changed"] += 1
                        code, message = "edited", "Статья обновлена"
                    self.event(code, message, title=title, changes=changes, revision=revision,
                               diff_before=base.text, diff_after=text)
                except WikiError as exc:
                    if exc.code == 'bot-excluded':
                        excluded[title] = ERROR_LABELS[exc.code]
                        summary['skipped'] += 1
                        self.event('bot_excluded', excluded[title], title=title, error=exc.code)
                        continue
                    if exc.code in RUN_ERRORS:
                        self.event('error', 'Не удалось обработать страницу', title=title, error=exc.code)
                        raise
                    summary["errors"] += 1
                    errors[title] = ERROR_LABELS.get(exc.code, "Ошибка обработки: " + exc.code)
                    self.event("error", errors[title], title=title, error=exc.code)
            self.checkpoint()
            after = refresh_inventory(self.wiki, self.store, self.slug, config=config)
            summary["remaining"] = after["total"]
            summary["verification"] = not dry_run
            if not dry_run:
                # New category members also need verification, including bot exclusions.
                for title in sorted(set(after['articles']) - selected):
                    self.checkpoint()
                    try:
                        if bot_edit_block(self.wiki.fetch(title).text, self.settings.bot_username) == 'bot-excluded':
                            excluded[title] = ERROR_LABELS['bot-excluded']
                    except WikiError as exc:
                        if exc.code in RUN_ERRORS:
                            raise
                        errors[title] = ERROR_LABELS.get(exc.code, 'Ошибка проверки: ' + exc.code)
                        summary['errors'] += 1
                candidates = {title: errors.get(title) or unresolved.get(title)
                              or '; '.join(notes_by_title.get(title, []))
                              or 'Статья осталась в отслеживающей категории; проверьте шаблоны и их параметры'
                              for title in after['articles']}
            else:
                # A proposal is not a cleanup failure. An article with no possible
                # edit is already a confirmed manual issue, even in a dry run.
                previous = self.store.get_state(self.slug + ':result', {})
                candidates = {item['title']: item['reason'] for item in previous.get('manual', [])
                              if previous.get('source') == source_category(self.slug, config)}
                candidates.update(unresolved)
                candidates.update({title: '; '.join(notes) for title, notes in notes_by_title.items() if notes})
                candidates.update(errors)
            manual = [dict(title=title, reason=reason) for title, reason in sorted(candidates.items())
                      if title in after['articles'] and title not in excluded]
            exclusions = [dict(title=title, reason=reason) for title, reason in sorted(excluded.items())
                          if title in after['articles']]
            self.store.set_state(self.slug + ':result', dict(source=source_category(self.slug, config),
                at=time.time(), manual=manual, excluded=exclusions, verification=not dry_run, run_id=self.run_id))
            manual_titles = {item['title'] for item in manual}
            for event in self.events:
                if event['code'] == 'unchanged' and event.get('title') in manual_titles:
                    event['requires_manual'] = True
            failed = bool(manual or summary['errors'])
            if not dry_run:
                self.event('remaining', f"Требуют исправления после обработки: {len(manual)}; запрет бота: {len(exclusions)}",
                           categories=after['categories'])
            else:
                self.event("dry_run_verification", "Очистка категорий не оценивается в режиме проверки")
                if manual:
                    self.event('manual_issues', f'Требуют ручного исправления: {len(manual)}')
            result_report = report(self.store, self.slug, config)
            self.store.set_state(self.slug + ":worker_error", None)
        except Controlled as exc:
            controlled = {"paused": "paused", "stopped": "stopped"}.get(exc.mode, "interrupted")
            self.event("control", "Обработка прервана командой администратора", mode=controlled)
        except WikiError as exc:
            failed = True
            summary["errors"] += 1
            self.store.set_state(self.slug + ":worker_error", {"code": exc.code, "at": time.time()})
            self.event("error", ERROR_LABELS.get(exc.code, "Ошибка получения данных Википедии"), error=exc.code)
        except Exception:
            failed = True
            summary["errors"] += 1
            self.store.set_state(self.slug + ":worker_error", {"code": "internal", "at": time.time()})
            self.event("error", "Внутренняя ошибка обработчика", error="internal")
            import traceback
            traceback.print_exc()
        if result_report:
            summary.update(report_changes(before_report, result_report))
        self.event("finished", "Обработка завершена с ошибками" if failed else "Обработка прервана" if controlled else "Обработка завершена")
        self.store.finish_run(self.run_id, self.events, summary, result_report, status=controlled or ("failed" if failed else "success"))
        self.run_id, self.generation = None, None
        if not controlled:
            # Remaining articles and execution failures wait for the next daily
            # run, as requested. There is no immediate automatic edit retry.
            self.store.acknowledge(job)


class InventoryMonitor:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.wiki = wiki or WikiClient(settings, store)
        self.outer_renew = lambda: None

    def tick(self, now=None, force=False):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor="maintenance-monitor") as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
            self.wiki.request_guard = heartbeat
            heartbeat()
            for slug in TASK_SLUGS:
                config = get_config(self.store, slug)
                inventory = self.store.get_state(slug + ":inventory", {})
                requests = [job for job in self.store.queue("maintenance-monitor") if job["title"] == slug]
                attempt = self.store.get_state(slug + ":monitor_attempt", {})
                if not isinstance(attempt, dict):
                    attempt = {}
                source = source_category(slug, config)
                last_check = max(inventory.get("checked_at", 0) if inventory.get("source") == source else 0,
                                 attempt.get("at", 0) if attempt.get("source") == source else 0)
                if (force or requests or inventory.get('source') != source and attempt.get('source') != source
                        or now - last_check >= search_interval(self.settings, self.store, slug)):
                    self.store.set_state(slug + ":monitor_attempt", {"source": source, "at": now})
                    try:
                        load_sections(self.wiki, self.store, slug, config, force=any(job['kind'] == 'sections' for job in requests))
                        refresh_inventory(self.wiki, self.store, slug, now, config=config)
                    except WikiError as exc:
                        self.store.set_state(slug + ":monitor_error", {"code": exc.code, "at": now})
                    for job in requests:
                        self.store.acknowledge(job)
            return True

    def forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                import traceback
                traceback.print_exc()
            time.sleep(5)
