"""Two serial daily actions; the monitor never edits articles or talk pages."""
import copy
import time

from ...schedules import next_daily_time, search_interval

from ...wiki import RUN_ERRORS, WikiClient, WikiError, require_bot_permission
from ...run_statistics import report_changes, report_snapshot
from ..daily_worker import DailyWorker, Controlled
from ..maintenance.inventory import Catalogue
from . import TASK_SLUGS
from .config import get_config
from .inventory import refresh_inventory, report, signature
from .transform import normalize, parse_creation_comment, parse_talk, update_templates

from .outcomes import LABELS, SOURCE_NOTICES, is_skipped



class TranslationWorker(DailyWorker):
    def __init__(self, settings, store, slug, wiki=None):
        if slug not in TASK_SLUGS:
            raise ValueError('Unknown translation action')
        super().__init__(settings, store, slug, wiki)

    def languages(self):
        key = 'translations:languages'
        cached = self.store.get_state(key)
        if cached and time.time() - cached['at'] < 86400:
            return set(cached['languages'])
        languages = self.wiki.wikipedia_languages()
        self.store.set_state(key, dict(at=time.time(), languages=sorted(languages)))
        return languages

    def _execute(self, job):
        control = self.store.control(self.slug)
        if control['mode'] != 'active' or job not in self.store.queue(self.slug):
            return
        config = copy.deepcopy(get_config(self.store, self.slug))
        dry_run = not (self.settings.wiki_write and config['autosave'])
        self.generation, self.events = control['generation'], []
        self.run_id = self.store.start_run(job['kind'], job['requested_by'], dry_run=dry_run, processor=self.slug, job=job)
        summary = dict(checked=0, changed=0, proposed=0, skipped=0, problems=0, errors=0, remaining=None)
        before_report = report_snapshot(report(self.store, self.slug, config))
        failed, controlled, result_report = False, None, {}
        try:
            self.event('started', 'Начало обработки', configuration=config,
                       job={key: job[key] for key in ('key', 'revision', 'due_at', 'spacing')})
            self.checkpoint()
            if job['kind'] == 'recheck':
                if dry_run:
                    self.event('recheck', 'Проверка всех статей; сохранённые отметки не изменяются в режиме проверки')
                elif self.store.reset_article_checks(self.slug, job):
                    self.event('recheck', 'Начата проверка с нуля. История запусков сохранена')
            self.event("scan", "Поиск статей для обработки")
            inventory = refresh_inventory(self.wiki, self.store, self.slug, config=config)
            allowed = set(inventory['articles'])
            checked = self.store.checked_articles(self.slug)
            candidates = allowed if dry_run and job['kind'] == 'recheck' else allowed - checked.keys()
            selected = sorted(candidates)
            if job['kind'] == 'article':
                selected = sorted(allowed & {job['title']})
                self.event('article_retry', 'Повторная проверка выбранной статьи', title=job['title'])
            summary['previously_checked'] = len(allowed & checked.keys())
            self.event('inventory', f"Статей в категориях: {inventory['total']}; к проверке: {len(selected)}",
                       categories=inventory['categories'], previously_checked=summary['previously_checked'])
            catalogue = Catalogue(self.wiki, self.store)
            definitions = [catalogue.aliases(name) for name in config['target_templates']] if selected else []
            languages = self.languages() if selected else set()
            talk_aliases = {normalize(name) for name in catalogue.aliases(config['parse_talk_template'])['aliases']} if selected and self.slug == 'translations-talk' else set()
            notes, outcomes = {}, {}
            def remember(title, outcome):
                # Unsaved proposals never prevent a later real pass.
                if not dry_run:
                    self.store.record_article_checks(self.slug, [dict(title=title, checked_at=time.time(),
                        outcome=outcome, reason=notes.get(title, ''), run_id=self.run_id)])
            for title in selected:
                self.checkpoint()
                summary['checked'] += 1
                try:
                    base = self.wiki.fetch(title)
                    if base.missing:
                        raise WikiError('article-missing')
                    require_bot_permission(base.text, self.settings.bot_username)
                    self.event('article', 'Начало обработки статьи', title=title, revision=base.revision)
                    talk = None
                    if self.slug == 'translations-categories':
                        entry = self.wiki.creation_comment(title, base.revision)
                        lang, original = parse_creation_comment(entry['comment'], languages)
                        source_page, source_revision = title, entry['revid']
                        if config['debug_output']:
                            self.event('creation_comment', 'Комментарий к созданию статьи', title=title,
                                       comment=entry['comment'], revision=source_revision)
                    else:
                        talk = self.wiki.fetch('Обсуждение:' + title)
                        if talk.missing:
                            raise WikiError('talk-missing')
                        lang, original = parse_talk(talk.text, talk_aliases, languages)
                        source_page, source_revision = talk.title, talk.revision
                    self.event('source', f'Найден источник: {lang} · {original}', title=source_page,
                               revision=source_revision, language=lang, original=original)
                    text, changes, issues = update_templates(base.text, definitions, lang, original)
                    if issues:
                        notes[title] = '; '.join(dict.fromkeys(LABELS[item['code']] for item in issues))
                        outcomes[title] = issues[0]['code']
                        summary['problems'] += 1
                        self.event('notice', notes[title], title=title, issues=issues)
                    if text == base.text:
                        if not issues:
                            summary['skipped'] += 1
                        self.event('unchanged', 'Шаблоны не изменены', title=title)
                        remember(title, 'unchanged')
                        continue
                    self.checkpoint()
                    revision = base.revision
                    if dry_run:
                        code, message = 'would_edit', 'Проверка: подготовлены параметры перевода'
                        summary['proposed'] += 1
                    else:
                        if talk:
                            current = self.wiki.revisions([talk.title])[0]
                            if current.missing or current.revision != talk.revision:
                                raise WikiError('talk-changed')
                        self.checkpoint()
                        revision = self.wiki.edit_article(base, text,
                            'Заполнение языка и оригинала перевода по ' + ('первой правке' if not talk else 'СО') +
                            f' ([[Special:Diff/{source_revision}|источник]])', allowed)
                        summary['changed'] += 1
                        code, message = 'edited', 'Параметры перевода сохранены'
                    self.event(code, message, title=title, revision=revision, changes=changes,
                               source_page=source_page, source_revision=source_revision,
                               diff_before=base.text, diff_after=text)
                    if not dry_run:
                        remember(title, 'edited')
                except WikiError as exc:
                    if exc.code in RUN_ERRORS:
                        self.event('error', 'Не удалось обработать страницу', title=title, error=exc.code)
                        raise
                    message = LABELS.get(exc.code, 'Ошибка обработки: ' + exc.code)
                    notes[title] = message
                    outcomes[title] = exc.code
                    if exc.code in SOURCE_NOTICES:
                        summary['skipped' if is_skipped(dict(outcome=exc.code)) else 'problems'] += 1
                        self.event('unchanged', message, title=title, error=exc.code)
                        remember(title, exc.code)
                    else:
                        summary['errors'] += 1
                        self.event('error', message, title=title, error=exc.code)
            self.checkpoint()
            after = refresh_inventory(self.wiki, self.store, self.slug, config=config)
            summary['remaining'] = after['total']
            rows = [dict(title=title, reason=reason, outcome=outcomes[title]) for title, reason in sorted(notes.items())
                    if title in after['articles']]
            self.store.set_state(self.slug + ':result', dict(signature=signature(config), at=time.time(),
                                 manual=[item for item in rows if not is_skipped(item)],
                                 skipped=[item for item in rows if is_skipped(item)], run_id=self.run_id, dry_run=dry_run))
            self.event('remaining', f"Осталось статей в категориях: {after['total']}")
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
            summary.update(report_changes(before_report, result_report))
        self.event('finished', 'Обработка завершена с ошибками' if failed else 'Обработка прервана' if controlled else 'Обработка завершена')
        self.store.finish_run(self.run_id, self.events, summary, result_report, status=controlled or ('failed' if failed else 'success'))
        self.run_id, self.generation = None, None
        if not controlled:
            if failed and job['kind'] == 'article':
                self.store.move_job(job, next_daily_time(config['run_time'], time.time()))
            else:
                self.store.acknowledge(job)


class TranslationMonitor:
    def __init__(self, settings, store, wiki=None):
        self.settings, self.store = settings, store
        self.wiki = wiki or WikiClient(settings, store)
        self.outer_renew = lambda: None

    def tick(self, now=None, force=False):
        now = time.time() if now is None else now
        with self.store.worker_lease(processor='translations-monitor') as renew:
            if not renew:
                return False
            def heartbeat():
                renew()
                self.outer_renew()
            self.wiki.request_guard = heartbeat
            heartbeat()
            for slug in TASK_SLUGS:
                config = get_config(self.store, slug)
                inventory = self.store.get_state(slug + ':inventory', {})
                requests = [job for job in self.store.queue('translations-monitor') if job['title'] == slug]
                attempt = self.store.get_state(slug + ':monitor_attempt', {})
                valid = inventory.get('sources') == config['target_categories']
                last = max(inventory.get('checked_at', 0) if valid else 0,
                           attempt.get('at', 0) if attempt.get('sources') == config['target_categories'] else 0)
                if force or requests or not valid and attempt.get('sources') != config['target_categories'] or now - last >= search_interval(self.settings, self.store, slug):
                    self.store.set_state(slug + ':monitor_attempt', dict(at=now, sources=config['target_categories']))
                    try:
                        refresh_inventory(self.wiki, self.store, slug, now, config=config)
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
