"""Daily serial processing and independent incremental observation."""
import time
from dataclasses import replace

from ...run_statistics import report_changes, report_snapshot
from ...schedules import search_due
from ...wiki import RUN_ERRORS, WikiClient, WikiError, require_bot_permission
from ..daily_worker import DailyWorker, Controlled
from .config import get_config
from .inventory import refresh, record, report, selection, complete_year
from .rules import format_discussion, edit_comment, BARE_NAV, visible_text


class ProjectWorker(DailyWorker):
    def __init__(self, settings, store, slug='projects', wiki=None):
        if slug != 'projects':
            raise ValueError('Unknown project discussion action')
        super().__init__(settings, store, slug, wiki)

    def _execute(self, job):
        # Monitor and executor share this short-lived observation lease: an old
        # monitor snapshot cannot overwrite freshly saved discussion results.
        with self.store.worker_lease(processor='projects-observer') as renew:
            if not renew:
                return
            previous = self.renew
            def heartbeat():
                previous()
                renew()
            self.renew = heartbeat
            try:
                self.process(job)
            finally:
                self.renew = previous

    def process(self, job):
        control = self.store.control(self.slug)
        if control['mode'] != 'active' or job not in self.store.queue(self.slug):
            return
        config = get_config(self.store, self.slug)
        dry_run = not (self.settings.wiki_write and config['autosave'])
        self.generation, self.events = control['generation'], []
        self.run_id = self.store.start_run(job['kind'], job['requested_by'], dry_run=dry_run, processor=self.slug, job=job)
        summary = dict(checked=0, changed=0, proposed=0, skipped=0, errors=0)
        before = report_snapshot(report(self.store))
        failed, controlled = False, None
        inventory = None
        try:
            self.event('started', 'Обработка обсуждений проектов', configuration=config,
                       job={key: job[key] for key in ('key', 'revision', 'due_at', 'spacing')})
            self.checkpoint()
            self.event('scan', 'Поиск ежегодных страниц обсуждений и проверка актуальных версий')
            inventory, bases = refresh(self.wiki, self.store, config, self.settings.bot_username, force=True,
                                       include_completed=job['kind'] == 'recheck')
            summary['previously_checked'] = len(inventory['pages']) - len(bases)
            self.event('inventory', f"К проверке страниц: {len(bases)}; завершённых лет: {summary['previously_checked']}")
            allowed = set(bases)
            for row in inventory['pages']:
                if row['title'] not in bases:
                    continue
                self.checkpoint()
                title, base = row['title'], bases[row['title']]
                summary['checked'] += 1
                self.event('article', 'Проверка номинаций и навигации', title=title, revision=base.revision)
                try:
                    require_bot_permission(base.text, self.settings.bot_username)
                    text, closed, navigation_error = format_discussion(base.text, config['spacing'])
                    if navigation_error:
                        self.event('notice', navigation_error, title=title)
                    if text == base.text:
                        self.event('unchanged', 'Изменений не требуется', title=title, revision=base.revision)
                        continue
                    comment = edit_comment(base.text, text)
                    self.event('edit_summary', 'Описание правки: ' + comment, title=title, edit_summary=comment)
                    old_navigation = BARE_NAV.findall(visible_text(base.text))
                    new_navigation = BARE_NAV.findall(visible_text(text))
                    self.checkpoint()
                    if dry_run:
                        summary['proposed'] += 1
                        revision, code = base.revision, 'would_edit'
                    else:
                        revision = self.wiki.edit_project_discussion(base, text, comment, allowed)
                        code = 'edited' if revision != base.revision else 'unchanged'
                        summary['changed'] += code == 'edited'
                        updated = record(replace(base, revision=revision, text=text), time.time(), config, self.settings.bot_username)
                        row.clear()
                        row.update(updated)
                        complete_year(self.store, row)
                        self.store.set_state('projects:inventory', inventory)
                    self.event(code, 'Подготовлены изменения обсуждения' if dry_run else
                               'Обсуждение обработано' if code == 'edited' else 'Изменений не требуется',
                               title=title, revision=revision, closed=closed,
                               **(dict(diff_before=base.text, diff_after=text) if code != 'unchanged' else {}),
                               changes=[dict(template='ОБПРО-Навигация', action='updated')]
                                       if code != 'unchanged' and old_navigation != new_navigation else [])
                except WikiError as exc:
                    if exc.code in RUN_ERRORS:
                        raise
                    summary['skipped'] += 1
                    if exc.code in {'bot-excluded', 'page-in-use', 'editconflict', 'missingtitle'}:
                        self.event('unchanged', 'Запрет бота' if exc.code == 'bot-excluded' else
                                   'Страница изменилась или временно занята; повторная проверка по расписанию',
                                   title=title, error=exc.code)
                    else:
                        summary['errors'] += 1
                        failed = True
                        self.event('error', 'Не удалось сохранить обсуждение', title=title, error=exc.code)
            self.store.set_state('projects:worker_error', None)
            self.store.set_state('projects:last_run_at', time.time())
        except Controlled as exc:
            controlled = exc.mode if exc.mode in {'paused', 'stopped'} else 'interrupted'
            self.event('control', 'Обработка прервана командой администратора', mode=controlled)
        except WikiError as exc:
            failed = True
            summary['errors'] += 1
            self.store.set_state('projects:worker_error', dict(code=exc.code, at=time.time()))
            self.event('error', 'Ошибка получения или сохранения обсуждений', error=exc.code)
        except Exception:
            failed = True
            summary['errors'] += 1
            self.store.set_state('projects:worker_error', dict(code='internal', at=time.time()))
            self.event('error', 'Внутренняя ошибка обработчика', error='internal')
            import traceback
            traceback.print_exc()
        result = report(self.store)
        summary.update(report_changes(before, result))
        self.event('finished', 'Обработка завершена с ошибками' if failed else 'Обработка прервана' if controlled else 'Обработка завершена')
        archive = {key: value for key, value in result.items() if key not in {'page_rows', 'pending_articles', 'manual'}}
        self.store.finish_run(self.run_id, self.events, summary, archive, status=controlled or ('failed' if failed else 'success'))
        self.run_id, self.generation = None, None
        if not controlled:
            self.store.acknowledge(job)


class ProjectMonitor:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.wiki = wiki or WikiClient(settings, store)
        self.outer_renew = lambda: None

    def tick(self, now=None, force=False):
        now = time.time() if now is None else now
        if self.store.control('projects')['mode'] == 'stopped':
            return False
        with self.store.worker_lease(processor='projects-observer') as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
            self.wiki.request_guard = heartbeat
            config = get_config(self.store, 'projects')
            requests = self.store.queue('projects-monitor')
            attempt = self.store.get_state('projects:monitor_attempt', {})
            if not (force or requests or attempt.get('selection') != selection(config) or
                    search_due(self.settings, self.store, 'projects', attempt.get('at'), now)):
                return False
            self.store.set_state('projects:monitor_attempt', dict(at=now, selection=selection(config)))
            try:
                refresh(self.wiki, self.store, config, self.settings.bot_username, now=now)
                report(self.store)
            except WikiError as exc:
                self.store.set_state('projects:monitor_error', dict(code=exc.code, at=now))
            for job in requests:
                self.store.acknowledge(job)
            return True

    def forever(self):
        while True:
            self.tick()
            time.sleep(5)
