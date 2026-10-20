"""Temporary page obstacles are retried normally, never remembered as final skips."""
CODES = {'page-in-use', 'editconflict', 'talk-changed'}
LABELS = {
    'page-in-use': 'Статья сейчас редактируется участником. Проверим снова при следующем запуске; вручную исправлять не нужно.',
    'editconflict': 'Статья изменилась во время обработки. Проверим актуальную версию при следующем запуске.',
    'talk-changed': 'Страница обсуждения изменилась во время обработки. Проверим её снова при следующем запуске.',
}


def temporary_code(item):
    code = item.get('outcome') or item.get('error')
    if code in CODES:
        return code
    # Older snapshots did not save a structured outcome. Match known messages
    # only, without classifying arbitrary errors as temporary.
    reason = item.get('reason', '')
    if reason.startswith('Статья сейчас редактируется участником') or reason.startswith('Страница редактируется участником'):
        return 'page-in-use'
    if reason in {'Версия статьи изменилась во время обработки',
                  'Статья изменилась во время обработки; будет проверена в следующий запуск',
                  'Статья изменилась во время обработки; будет проверена в следующий ежедневный запуск.'}:
        return 'editconflict'
    return None


def split_report(report):
    temporary = {row['title']: row for row in report.get('temporary_articles', [])}
    for field in ('manual', 'skipped_articles'):
        kept = []
        for row in report.get(field, []):
            code = temporary_code(row)
            if code:
                temporary[row['title']] = dict(row, outcome=code, reason=LABELS[code])
            else:
                kept.append(row)
        report[field] = kept
    report['temporary_articles'] = sorted(temporary.values(), key=lambda row: row['title'])
    report['temporary'] = len(temporary)
    report['problems'] = len(report.get('manual', []))
    report['skipped'] = len(report.get('skipped_articles', []))
    pending = report.get('pending_articles', [])
    filtered = [row for row in pending if row['title'] not in temporary]
    if report.get('to_process') is not None:
        report['to_process'] = max(0, report['to_process'] - len(pending) + len(filtered))
    report['pending_articles'] = filtered
    return report
