"""Two daily actions using the shared serial executor and durable checked ledger."""
import copy
import time

from ...edit_comments import sections_comment, fit_comment

from ...schedules import next_daily_time, search_interval

from ...wiki import RUN_ERRORS, WikiClient, WikiError, require_bot_permission
from ...run_statistics import report_changes, report_snapshot
from ..daily_worker import DailyWorker, Controlled
from . import TASK_SLUGS
from .config import get_config
from .inventory import Catalogue, inventory_signature, refresh_inventory, report, signature
from .transform import normalize, switch_templates

LABELS = {
    'bot-excluded': 'На странице запрещена работа бота',
    'page-in-use': 'Статья сейчас редактируется участником; проверка будет повторена в следующий запуск',
    'article-missing': 'Статья отсутствует',
    'redirect': 'Страница стала перенаправлением',
    'editconflict': 'Статья изменилась во время обработки; будет проверена в следующий запуск',
    'network': 'Не удалось получить данные Википедии; проверка будет повторена',
    'template-overlap': 'Искомый шаблон и шаблон замены ведут на одну страницу; исправьте параметры',
    'inventory-limited': 'Список статей ограничен настройкой; увеличьте лимит или установите 0',
}
SKIPS = {'bot-excluded', 'article-missing', 'redirect'}


class SectionWorker(DailyWorker):
    def __init__(self, settings, store, slug, wiki=None):
        if slug not in TASK_SLUGS:
            raise ValueError('Unknown section action')
        super().__init__(settings, store, slug, wiki)

    def _execute(self, job):
        control = self.store.control(self.slug)
        if control['mode'] != 'active' or job not in self.store.queue(self.slug):
            return
        config = copy.deepcopy(get_config(self.store, self.slug))
        dry_run = not (self.settings.wiki_write and config['autosave'])
        self.generation, self.events = control['generation'], []
        self.run_id = self.store.start_run(job['kind'], job['requested_by'], dry_run=dry_run, processor=self.slug, job=job)
        summary = dict(checked=0, changed=0, proposed=0, skipped=0, problems=0, errors=0, remaining=None)
        before = report_snapshot(report(self.store, self.slug, config))
        failed, controlled, result_report = False, None, {}
        try:
            self.event('started', 'Начало обработки разделов', configuration=config,
                       job={key: job[key] for key in ('key', 'revision', 'due_at', 'spacing')})
            self.checkpoint()
            if job['kind'] == 'recheck':
                if dry_run:
                    self.event('recheck', 'Проверка всех статей; сохранённые отметки не изменяются в режиме проверки')
                elif self.store.reset_article_checks(self.slug, job):
                    self.event('recheck', 'Начата проверка с нуля. История запусков сохранена')
            self.event("scan", "Поиск статей для обработки")
            inventory = refresh_inventory(self.wiki, self.store, self.slug, config=config,
                                          force=job['kind'] in {'full', 'recheck', 'article'})
            allowed = set(inventory['articles'])
            checked = self.store.checked_articles(self.slug)
            candidates = allowed if not config['resume'] or dry_run and job['kind'] == 'recheck' else allowed - checked.keys()
            selected = sorted(candidates)
            if job['kind'] == 'article':
                selected = sorted(allowed & {job['title']})
                self.event('article_retry', 'Повторная проверка выбранной статьи', title=job['title'])
            if config['limit_articles']:
                selected = selected[:config['limit_articles']]
            summary['previously_checked'] = len(allowed & checked.keys())
            self.event('inventory', f"Найдено статей: {len(allowed)}; к проверке: {len(selected)}",
                       sources=inventory['categories'], previously_checked=summary['previously_checked'])
            catalogue = Catalogue(self.wiki, self.store, config['redirects_cache_days'])
            if selected:
                source = catalogue.aliases(config['source_template'])
                replacement = catalogue.aliases(config['replacement_template'])
                if {normalize(name) for name in source['aliases']} & {normalize(name) for name in replacement['aliases']}:
                    raise WikiError('template-overlap')
                ignored = [catalogue.aliases(name) for name in config['ignored_templates']]
            notes, skipped = {}, {}
            def remember(title, outcome, reason=''):
                # Dry-run proposals and reads must not suppress a later real execution.
                if not dry_run:
                    self.store.record_article_checks(self.slug, [dict(title=title, outcome=outcome, reason=reason,
                        checked_at=time.time(), run_id=self.run_id)])
            for title in selected:
                self.checkpoint()
                summary['checked'] += 1
                try:
                    base = self.wiki.fetch(title)
                    if base.missing:
                        raise WikiError('article-missing')
                    require_bot_permission(base.text, self.settings.bot_username)
                    self.event('article', 'Проверка разделов статьи', title=title, revision=base.revision)
                    text, changes, issues, diagnostics = switch_templates(base.text, self.slug, source, replacement, ignored)
                    if config['debug_output']:
                        self.event('sections', 'Содержимое разделов', title=title, sections=diagnostics)
                    if issues:
                        reason = '; '.join(f"«{item['section']}»: {item['reason']}" for item in issues)
                        notes[title] = dict(title=title, outcome='conflicting-markers', reason=reason)
                        summary['problems'] += 1
                        self.event('notice', reason, title=title, issues=issues)
                    if text == base.text:
                        targets = [row for row in diagnostics if row['targets']]
                        reason = ('Разделы с шаблоном пустые; замена не требуется' if self.slug == 'sections-empty-to-fill'
                                  else 'Разделы с шаблоном содержат текст; замена не требуется') if targets else 'Шаблон отсутствует в прямом содержимом разделов'
                        if issues:
                            reason = notes[title]['reason']
                        if not issues:
                            summary['skipped'] += 1
                            skipped[title] = dict(title=title, outcome='unchanged', reason=reason)
                        self.event('unchanged', reason, title=title, revision=base.revision, sections=targets)
                        remember(title, 'conflicting-markers' if issues else 'unchanged', notes[title]['reason'] if issues else reason)
                        continue
                    self.checkpoint()
                    edit_summary = sections_comment(changes)
                    self.event('edit_summary', 'Описание правки: ' + edit_summary, title=title,
                               edit_summary=fit_comment(edit_summary))
                    if dry_run:
                        summary['proposed'] += 1
                        code, message, revision = 'would_edit', 'Проверка: подготовлена замена шаблонов', base.revision
                    else:
                        revision = self.wiki.edit_article(base, text, fit_comment(edit_summary), allowed)
                        summary['changed'] += 1
                        code, message = 'edited', 'Шаблоны разделов заменены'
                    self.event(code, message, title=title, revision=revision, changes=changes, diff_before=base.text, diff_after=text)
                    if not dry_run:
                        remember(title, 'conflicting-markers' if issues else 'edited', notes[title]['reason'] if issues else '')
                except WikiError as exc:
                    if exc.code in RUN_ERRORS:
                        self.event('error', 'Не удалось обработать страницу', title=title, error=exc.code)
                        raise
                    reason = LABELS.get(exc.code, 'Ошибка обработки: ' + exc.code)
                    if exc.code in SKIPS:
                        summary['skipped'] += 1
                        skipped[title] = dict(title=title, outcome=exc.code, reason=reason)
                        remember(title, exc.code, reason)
                        self.event('unchanged', reason, title=title, error=exc.code)
                    else:
                        summary['errors'] += 1
                        notes[title] = dict(title=title, outcome='error', reason=reason)
                        self.event('error', reason, title=title, error=exc.code)
            self.checkpoint()
            after = refresh_inventory(self.wiki, self.store, self.slug, config=config, force=False)
            summary['remaining'] = after['total']
            if after['limited']:
                summary['errors'] += 1
                self.event('error', LABELS['inventory-limited'], error='inventory-limited')
            self.store.set_state(self.slug + ':result', dict(signature=signature(config), at=time.time(),
                manual=list(notes.values()), skipped=list(skipped.values()), run_id=self.run_id, dry_run=dry_run))
            result_report = report(self.store, self.slug, config)
            failed = bool(summary['errors'])
            self.store.set_state(self.slug + ':worker_error', None)
        except Controlled as exc:
            controlled = {'paused': 'paused', 'stopped': 'stopped'}.get(exc.mode, 'interrupted')
            self.event('control', 'Обработка прервана командой администратора', mode=controlled)
        except WikiError as exc:
            failed = True
            summary['errors'] += 1
            self.store.set_state(self.slug + ':worker_error', dict(code=exc.code, at=time.time()))
            self.event('error', LABELS.get(exc.code, 'Ошибка получения данных Википедии'), error=exc.code)
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
        self.store.finish_run(self.run_id, self.events, summary, result_report, status=controlled or ('failed' if failed else 'success'))
        self.run_id, self.generation = None, None
        if not controlled:
            if failed and job['kind'] == 'article':
                self.store.move_job(job, next_daily_time(config['run_time'], time.time()))
            else:
                self.store.acknowledge(job)


class SectionMonitor:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.wiki = wiki or WikiClient(settings, store)
        self.outer_renew = lambda: None

    def tick(self, now=None, force=False):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor='sections-monitor') as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
            self.wiki.request_guard = heartbeat
            heartbeat()
            for slug in TASK_SLUGS:
                config = get_config(self.store, slug)
                requests = [job for job in self.store.queue('sections-monitor') if job['title'] == slug]
                attempt = self.store.get_state(slug + ':monitor_attempt', {})
                selection = inventory_signature(config)
                last = attempt.get('at', 0) if attempt.get('selection') == selection else 0
                if not (force or requests or attempt.get('selection') != selection or now - last >= search_interval(self.settings, self.store, slug)):
                    continue
                self.store.set_state(slug + ':monitor_attempt', dict(at=now, selection=selection))
                try:
                    if force or any(job['kind'] == 'redirects' for job in requests):
                        catalogue = Catalogue(self.wiki, self.store, config['redirects_cache_days'])
                        for name in dict.fromkeys([config['source_template'], config['replacement_template'], *config['ignored_templates']]):
                            catalogue.aliases(name, force=True)
                    refresh_inventory(self.wiki, self.store, slug, now, config=config, force=True)
                except WikiError as exc:
                    self.store.set_state(slug + ':monitor_error', dict(code=exc.code, at=now))
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
