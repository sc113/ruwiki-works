"""Durable per-action visits and import of TSV processing records."""
import hashlib
import time
from pathlib import Path

FILES = {'translations-categories': 'log_categories.tsv', 'translations-talk': 'log_parse_talk.tsv'}
HEADERS = ['template_name', 'article_title', 'old_template', 'comment', 'status', 'lang', 'original_title', 'new_template']
LEGACY_REASONS = {
    'комментарий имеет другой формат': 'В комментарии к созданию не распознана ссылка на оригинал. Проверьте источник на СО или заполните язык и оригинал вручную.',
    'комментарий не найден': 'Комментарий к созданию не найден. Проверьте источник перевода на СО.',
    'страница обсуждения не существует': 'Страница обсуждения отсутствует. Укажите язык и оригинал в шаблоне статьи.',
    'шаблон страницы обсуждения не найден': 'На СО нет шаблона с данными перевода. Найдите источник и заполните язык и оригинал.',
    'шаблон на странице обсуждения не содержит необходимые параметры': 'В шаблоне на СО нужны код языка и название оригинала в параметрах 1 и 2.',
}


def read_legacy_log(path):
    entries, pending = {}, ''
    with path.open(encoding='utf-8-sig', newline='') as handle:
        first = handle.readline()
        if not first:
            return entries
        if [name.strip() for name in first.rstrip('\r\n').split('\t')] != HEADERS:
            raise ValueError(f'{path.name}: неизвестный формат заголовка')
        for line in handle:
            if not pending and not line.strip():
                continue
            pending += line
            cells = pending.rstrip('\r\n').split('\t')
            if len(cells) < len(HEADERS):
                continue
            # Comments may contain unquoted newlines or tabs.
            cells = cells[:3] + ['\t'.join(cells[3:-4])] + cells[-4:]
            title, status = cells[1].strip().replace('_', ' '), cells[4].strip()
            if not title or len(title) > 255 or any(c in title for c in '\r\n|{}[]<>\x00'):
                raise ValueError(f'{path.name}: некорректное название статьи')
            entries[title] = dict(title=title, outcome='legacy', reason='' if status == 'шаблон обновлен'
                                  else LEGACY_REASONS.get(status, status), run_id='')
            pending = ''
    if pending.strip():
        raise ValueError(f'{path.name}: незавершённая строка')
    return entries


def import_logs(source, store):
    source = Path(source)
    # Validate both files before changing the database. Never read bot credentials.
    parsed = {slug: read_legacy_log(source / name) for slug, name in FILES.items()}
    now, counts = time.time(), {}
    for slug, entries in parsed.items():
        before = store.checked_articles(slug)
        values = [dict(entry, checked_at=now) for entry in entries.values()]
        store.record_article_checks(slug, values, overwrite=False)
        counts[slug] = dict(total=len(entries), added=len(set(entries) - before.keys()))
        store.set_state(slug + ':legacy_import', dict(at=now, filename=FILES[slug], **counts[slug],
                        sha256=hashlib.sha256((source / FILES[slug]).read_bytes()).hexdigest()))
    return counts
