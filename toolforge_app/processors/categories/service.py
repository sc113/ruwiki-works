"""Two serial category actions with incremental weekly and full monthly passes."""
import copy
import json
import time

from ...wiki import RUN_ERRORS, WikiClient, WikiError, require_bot_permission
from ...run_statistics import report_changes, report_snapshot
from ...schedules import search_interval
from ..daily_worker import DailyWorker, Controlled
from . import TASK_SLUGS
from .config import get_config
from .formats import expected_category, refresh_formats
from .inventory import refresh_inventory, refresh_snapshot, report, selection, is_checked, snapshot_key

LABELS = {
    'category-formats-invalid': 'Таблица форматов неполная или некорректная; правки не выполнялись',
    'category-formats-conflict': 'В таблице обнаружены противоречащие друг другу форматы',
    'category-formats-missing': 'Страница с форматами отсутствует',
    'category-replica-unavailable': 'Не удалось прочитать базу реплик; проверка будет повторена',
    'bot-excluded': 'На странице запрещена работа бота',
    'page-in-use': 'Страница редактируется участником; проверка будет повторена',
    'editconflict': 'Страница изменилась во время обработки; проверка будет повторена',
    'protectedpage': 'Категория защищена; требуется ручное исправление',
    'redirect': 'Страница стала перенаправлением; автоматическая замена не выполняется',
}


class CategoryWorker(DailyWorker):
    def __init__(self, settings, store, slug, wiki=None):
        if slug not in TASK_SLUGS:
            raise ValueError('Unknown category action')
        super().__init__(settings, store, slug, wiki)
        self.last_progress_at = 0

    def event(self, code, message, title='', **details):
        self.events.append(dict(at=time.time(), code=code, message=message, title=title, **details))
        # A first pass may verify thousands of pages. Batch progress writes while
        # retaining every diagnostic event, and immediately publish saved edits/errors.
        if self.run_id and (time.time() - self.last_progress_at >= 2 or code in
                            {'started', 'edited', 'would_edit', 'error', 'control', 'finished'}):
            self.store.update_progress(self.run_id, self.events)
            self.last_progress_at = time.time()

    def _execute(self, job):
        control = self.store.control(self.slug)
        if control['mode'] != 'active' or job not in self.store.queue(self.slug):
            return
        config = copy.deepcopy(get_config(self.store, self.slug))
        dry_run = not (self.settings.wiki_write and config['autosave'])
        creates = self.slug == 'categories-create'
        self.generation, self.events = control['generation'], []
        self.run_id = self.store.start_run(job['kind'], job['requested_by'], dry_run=dry_run, processor=self.slug, job=job)
        summary = dict(checked=0, changed=0, proposed=0, skipped=0, problems=0, errors=0, remaining=None)
        before = report_snapshot(report(self.store, self.slug, config))
        failed, controlled, result_report = False, None, {}
        try:
            self.event('started', 'Создание категорий обслуживания' if creates else 'Проверка оформления категорий',
                       configuration=config, job={key: job[key] for key in ('key', 'revision', 'due_at', 'spacing')})
            self.checkpoint()
            self.event('scan', 'Загрузка форматов из вики-таблицы и поиск категорий')
            formats = refresh_formats(self.wiki, self.store, config)
            inventory = refresh_inventory(self.wiki, self.store, self.slug, config, formats=formats, force=True)
            self.event('formats', f"Форматы: {len(formats['simple'])} обычных, {len(formats['complex'])} сложных",
                       title=formats['source'], revision=formats['revision'])
            allowed = set(inventory['articles'])
            checks = self.store.checked_articles(self.slug) if not creates else {}
            done = {title for title in allowed if is_checked(checks.get(title))}
            recheck = job['kind'] in {'recheck', 'month_end'}
            candidates = allowed if creates or recheck else allowed - done
            if job['kind'] == 'article':
                candidates = allowed & {job['title']}
            selected = sorted(candidates)
            if config['limit_categories']:
                selected = selected[:config['limit_categories']]
            summary['previously_checked'] = len(done)
            self.event('inventory', f'Найдено категорий: {len(allowed)}; к проверке: {len(selected)}',
                       previously_checked=len(done), backend=inventory['backend'])
            notes = {}
            # Content reads are batched; each save uses its own conflict-protected revision.
            for offset in range(0, len(selected), 20):
                self.checkpoint()
                pages = self.wiki.category_pages(selected[offset:offset + 20])
                for title in selected[offset:offset + 20]:
                    self.checkpoint()
                    summary['checked'] += 1
                    try:
                        page = pages[title]
                        if page.get('error'):
                            raise WikiError(page['error'])
                        base, population = page['base'], page['population']
                        expected = expected_category(base.title, formats, config)
                        if not expected:
                            raise WikiError('outside-scope')
                        if creates and (not base.missing or not population) or not creates and base.missing:
                            summary['skipped'] += 1
                            self.event('unchanged', 'Категория уже создана' if creates and not base.missing
                                       else 'Категория опустела' if creates else 'Категория удалена', title=title)
                            continue
                        require_bot_permission(base.text, self.settings.bot_username)
                        self.event('article', 'Проверка категории', title=title, revision=base.revision, population=population)
                        if not creates and base.text.strip() == expected['text'].strip():
                            summary['skipped'] += 1
                            self.event('unchanged', 'Оформление соответствует формату', title=title, revision=base.revision)
                            if not dry_run:
                                self.remember(title, 'ok', base.revision, expected['signature'])
                            continue
                        comment = expected['summary'] if creates else 'обновление оформления категории'
                        self.event('edit_summary', 'Описание правки: ' + comment, title=title, edit_summary=comment)
                        self.checkpoint()
                        if dry_run:
                            summary['proposed'] += 1
                            code, revision = 'would_edit', base.revision
                        else:
                            revision = self.wiki.edit_category(base, expected['text'], comment, allowed, create=creates)
                            summary['changed'] += 1
                            code = 'edited'
                        self.event(code, ('Проверка: подготовлено создание категории' if creates else 'Проверка: подготовлено оформление')
                                   if dry_run else 'Категория создана' if creates else 'Оформление категории обновлено',
                                   title=title, revision=revision, diff_before=base.text, diff_after=expected['text'],
                                   changes=[dict(template='Категория к ежемесячной очистке' if expected['kind'] == 'simple'
                                                 else 'Категория обслуживания', action='created' if creates else 'formatted')])
                        if not dry_run and not creates:
                            self.remember(title, 'edited', revision, expected['signature'])
                    except WikiError as exc:
                        if exc.code in RUN_ERRORS:
                            self.event('error', 'Не удалось обработать категорию', title=title, error=exc.code)
                            raise
                        if exc.code in {'bot-excluded', 'articleexists', 'missingtitle', 'redirect'}:
                            summary['skipped'] += 1
                            self.event('unchanged', LABELS.get(exc.code, 'Состояние категории изменилось'), title=title, error=exc.code)
                            if not creates and exc.code == 'bot-excluded' and not dry_run:
                                self.remember(title, exc.code, base.revision, expected['signature'])
                        else:
                            # Page-local obstacles are manual issues, independent of execution status.
                            reason = LABELS.get(exc.code, 'Требуется проверить категорию: ' + exc.code)
                            notes[title] = dict(title=title, reason=reason, categories=[], outcome=exc.code)
                            summary['problems'] += 1
                            self.event('notice', reason, title=title, error=exc.code)
            self.checkpoint()
            after = refresh_inventory(self.wiki, self.store, self.slug, config, formats=formats, force=True)
            summary['remaining'] = after['total']
            self.store.set_state(self.slug + ':result', dict(at=time.time(), selection=selection(config),
                format_signature=formats['signature'], manual=list(notes.values()), run_id=self.run_id, dry_run=dry_run))
            result_report = report(self.store, self.slug, config)
            self.store.set_state(self.slug + ':worker_error', None)
        except Controlled as exc:
            controlled = exc.mode if exc.mode in {'paused', 'stopped'} else 'interrupted'
            self.event('control', 'Обработка прервана командой администратора', mode=controlled)
        except WikiError as exc:
            failed = True
            summary['errors'] += 1
            self.store.set_state(self.slug + ':worker_error', dict(code=exc.code, at=time.time()))
            self.event('error', LABELS.get(exc.code, 'Ошибка получения или сохранения данных'), error=exc.code)
        except Exception:
            failed = True
            summary['errors'] += 1
            self.store.set_state(self.slug + ':worker_error', dict(code='internal', at=time.time()))
            self.event('error', 'Внутренняя ошибка обработчика', error='internal')
            import traceback
            traceback.print_exc()
        if result_report:
            summary.update(report_changes(before, result_report))
        self.event('finished', 'Обработка завершена с ошибками' if failed else 'Обработка прервана' if controlled else 'Обработка завершена')
        # The full inventory and definitions belong to the current report, not every run archive.
        archive = {key: value for key, value in result_report.items() if key not in {'formats', 'pending_articles'}}
        self.store.finish_run(self.run_id, self.events, summary, archive, status=controlled or ('failed' if failed else 'success'))
        self.run_id, self.generation = None, None
        if not controlled:
            self.store.acknowledge(job)

    def remember(self, title, outcome, revision, signature):
        self.store.record_article_checks(self.slug, [dict(title=title, outcome=outcome,
            reason=json.dumps(dict(revision=revision, signature=signature)), checked_at=time.time(), run_id=self.run_id)])


class CategoryMonitor:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.wiki = wiki or WikiClient(settings, store)
        self.outer_renew = lambda: None

    def tick(self, now=None, force=False):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor='categories-monitor') as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
            self.wiki.request_guard = heartbeat
            scans, definitions = {}, {}
            for slug in TASK_SLUGS:
                if self.store.control(slug)['mode'] == 'stopped':
                    continue
                config = get_config(self.store, slug)
                requests = [job for job in self.store.queue('categories-monitor') if job['title'] == slug]
                attempt = self.store.get_state(slug + ':monitor_attempt', {})
                if not (force or requests or attempt.get('selection') != selection(config)
                        or now - attempt.get('at', 0) >= search_interval(self.settings, self.store, slug)):
                    continue
                self.store.set_state(slug + ':monitor_attempt', dict(at=now, selection=selection(config)))
                try:
                    heartbeat()
                    if config['source_page'] not in definitions:
                        definitions[config['source_page']] = refresh_formats(self.wiki, self.store, config)
                    formats = definitions[config['source_page']]
                    key = snapshot_key(config)
                    if key not in scans:
                        scans[key] = refresh_snapshot(self.wiki, self.store, config, formats, now=now)
                    refresh_inventory(self.wiki, self.store, slug, config, scan=scans[key], formats=formats, now=now)
                except WikiError as exc:
                    self.store.set_state(slug + ':monitor_error', dict(code=exc.code, at=now))
                for job in requests:
                    self.store.acknowledge(job)
            return True

    def forever(self):
        while True:
            self.tick()
            time.sleep(5)
