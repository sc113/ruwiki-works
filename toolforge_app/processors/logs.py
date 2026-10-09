"""One public page result per edit or check; detailed diagnostics stay private."""
import copy

from ..run_statistics import public_summary

START_CODES = {'article', 'page'}
RESULTS = {
    'edited': ('success', 'Обработано'), 'table_edited': ('success', 'Обработано'),
    'would_edit': ('preview', 'Подготовлено без сохранения'),
    'table_would_edit': ('preview', 'Подготовлено без сохранения'),
    'unchanged': ('neutral', 'Изменения не требуются'),
    'table_unchanged': ('neutral', 'Изменения не требуются'),
    'missing': ('warning', 'Страница отсутствует'), 'table_missing': ('warning', 'Страница отсутствует'),
    'bot_excluded': ('warning', 'Пропущено'), 'table_excluded': ('warning', 'Пропущено'),
    'deferred': ('warning', 'Отложено после новой правки'),
    'error': ('error', 'Не обработано'),
}


def public_run(run):
    result = {key: copy.deepcopy(run[key]) for key in
              ('id', 'processor', 'kind', 'started_at', 'finished_at', 'status', 'dry_run')}
    result.update(summary=public_summary(run['summary']), report={}, table_text='', spacing=False)
    expired = run['report'].get('_details_expired_at')
    if expired:
        result['details_expired_at'] = expired
    result['summary'].setdefault('changed', 0)
    for field in ('problems', 'skipped'):
        if type(run['report'].get(field)) is int:
            result['summary'].setdefault('report_' + field, run['report'][field])
    if expired or run['report'].get('_public_events'):
        result['events'] = copy.deepcopy(run['events'])
        return result
    events, pending = [], {}
    for event in run['events']:
        code, title = event['code'], event.get('title', '')
        if not title:
            continue
        if code in START_CODES:
            pending[title] = len(events)
            events.append(dict(at=event['at'], code='processing', title=title, message='Обрабатывается', tone='running'))
            continue
        if code not in RESULTS:
            continue
        tone, message = RESULTS[code]
        projected = dict(at=event['at'], code=code, title=title, message=message, tone=tone)
        if code in {'edited', 'table_edited'} and type(event.get('revision')) is int:
            projected['revision'] = event['revision']
        if code in {'edited', 'table_edited'}:
            projected['template_count'] = len(event.get('changes') or [])
        # Only saved edits expose template names and inserted values.
        if code == 'edited' and event.get('changes'):
            keys = (('template', 'replacement', 'section', 'action') if run['processor'].startswith('sections-') else
                    ('template', 'language', 'original', 'action') if run['processor'].startswith('translations-') else
                    ('template', 'date', 'parameter', 'action'))
            changes = [{key: change[key] for key in keys if key in change} for change in event['changes']]
            projected['changes'] = changes
            descriptions = []
            for change in changes:
                text = '{{' + change.get('template', '') + '}}'
                if change.get('replacement'):
                    text += ' → {{' + change['replacement'] + '}}'
                if change.get('action') == 'unwrapped':
                    text = 'Убрана обёртка RQ → ' + text
                elif change.get('action') == 'removed_parameter':
                    text = 'RQ: удалён параметр «' + change.get('parameter', '') + '»; ' + text + ' уже присутствовал'
                if change.get('date'):
                    text += ('; дата сохранена: ' if change.get('action') == 'unwrapped' else ' · дата ') + change['date']
                if change.get('language'):
                    text += ' · язык=' + change['language']
                if change.get('original'):
                    text += ' · оригинал=' + change['original']
                descriptions.append(text)
            projected['message'] += ' · ' + '; '.join(descriptions)
        if code == 'unchanged' and event.get('error'):
            projected.update(message='Пропущено', tone='warning')
        if code == 'unchanged' and event.get('requires_manual'):
            projected.update(message='Требует исправления', tone='warning')
        index = pending.pop(title, None)
        if index is None:
            events.append(projected)
        else:
            events[index] = projected
    if run['status'] != 'running':
        for index in pending.values():
            events[index].update(code='unfinished', message='Обработка не завершена', tone='warning')
    result['events'] = events
    return result
