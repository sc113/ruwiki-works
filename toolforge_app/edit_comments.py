"""Human-readable edit provenance, with intact links within MediaWiki's limit."""
import re
from collections import OrderedDict

COMMENT_LIMIT = 500  # Unicode characters, not UTF-8 bytes.


def fit_comment(text, limit=COMMENT_LIMIT):
    """Shorten only when required, never in the middle of a wikilink."""
    if len(text) <= limit:
        return text
    sources = list(re.finditer(r'\[\[Special:Diff/[^\]]+\]\]', text, re.I))
    if sources and sources[-1].end() > limit and len(sources[-1][0]) + 2 < limit:
        suffix = ' ' + sources[-1][0]
        return _trim_comment(text, limit - len(suffix)) + suffix
    return _trim_comment(text, limit)


def _trim_comment(text, limit):
    end = limit - 1
    for link in re.finditer(r'\[\[.*?\]\]', text):
        if link.start() < end < link.end():
            end = link.start()
            break
    shortened = text[:end].rstrip(' ,;:.')
    if end and end < len(text) and text[end - 1].isalnum() and text[end].isalnum():
        shortened = shortened.rsplit(' ', 1)[0] if ' ' in shortened else ''
    return shortened.rstrip(' ,;:.') + '…'


def template_link(name):
    name = re.sub(r'^(?:Шаблон|Template):', '', str(name).strip(), flags=re.I)
    return '[[ш:' + name[:1].upper() + name[1:] + ']]'


def date_link(change):
    date = change.get('source_date') or change.get('date', '')
    revision = change.get('revision')
    return f'[[Special:Diff/{revision}|{date}]]' if revision else date


def section_description(change):
    section = change.get('section', '')
    description = f'в разделе «{section}»' if section else 'во вводной части статьи'
    original = change.get('original_section')
    if original and original != section:
        description += f' (ранее: {original})'
    return description


def dates_comment(changes):
    from .processors.maintenance.history import normalize
    groups = OrderedDict()
    for change in changes:
        groups.setdefault(change['template'], []).append(change)
    parts = []
    for name, rows in groups.items():
        redirects = list(dict.fromkeys(template_link(row['previous']) for row in rows
            if normalize(row['previous']) != normalize(name)))
        if redirects:
            prefix = 'Замена редиректа ' + ' и '.join(redirects) + ' на актуальный ' + template_link(name)
            prefix += ' с добавлением даты установки'
        else:
            prefix = 'В ' + template_link(name) + ' добавлена дата установки'
        details = list(dict.fromkeys(
            section_description(row) + ' (' + date_link(row) + ')' if row.get('section_scoped')
            else date_link(row) for row in rows))
        parts.append(prefix + (': ' if not any(row.get('section_scoped') for row in rows) else ' ') + ', '.join(details))
    return '. '.join(parts)


def rq_comment(changes):
    from .processors.maintenance.history import normalize
    groups = OrderedDict()
    for change in changes:
        groups.setdefault(change['previous'], []).append(change)
    parts = []
    for previous, rows in groups.items():
        converted, removed = [], []
        for row in rows:
            parameters = ', '.join(row.get('parameters') or [row['parameter']])
            target = template_link(row['template'])
            if row['action'] == 'removed_parameter':
                kept = f"; дата {row['date']} сохранена" if row.get('date') else ''
                removed.append(f"Удаление параметра {parameters} из {template_link(previous)}: {target} уже присутствует{kept} (основание: {date_link(row)})")
                continue
            evidence = ''
            variant = row.get('source_variant')
            if row.get('source_kind') == 'standalone':
                evidence = ('шаблон уже был в статье, ' if normalize(variant or row['template']) == normalize(row['template'])
                            else 'редирект ' + template_link(variant) + ' уже был в статье, ')
            elif variant and variant not in parameters.split(', '):
                evidence = f'ранее {variant}, '
            converted.append(f'{parameters} → {target} ({evidence}{date_link(row)})')
        if converted:
            parts.append('Замена параметров ' + template_link(previous) + ' на вложенные шаблоны с датами установки: ' + ', '.join(converted))
        parts.extend(removed)
    return '. '.join(parts)


def unwrap_comment(changes):
    parts = []
    for row in changes:
        date = f"; дата {row['date']} сохранена" if row.get('date') else ''
        section = ' ' + section_description(row) if row.get('section') else ''
        parts.append(template_link(row['previous']) + ' убран, т.к. осталась одна проблема: ' +
                     template_link(row['template']) + section + ' (параметры сохранены' + date + ')')
    return '. '.join(dict.fromkeys(parts))


def translation_comment(changes, *, source_revision, talk_title=None):
    descriptions = list(dict.fromkeys(template_link(row['template']) +
        f": +язык={row['language']}, +оригинал={row['original']}" for row in changes))
    prefix = 'Обновление' if talk_title else 'Заполнение'
    comment = prefix + ' параметров шаблонов перевода (' + ', '.join(descriptions) + ')'
    if talk_title:
        return comment + f' по данным [[ш:Переведённая статья]] на [[{talk_title}|СО]] ([[Special:Diff/{source_revision}|источник]])'
    return comment + f' на основе [[Special:Diff/{source_revision}|первой правки]]'


def sections_comment(changes):
    groups = OrderedDict()
    for row in changes:
        groups.setdefault((row['template'], row['replacement']), []).append(row)
    parts = []
    for (previous, replacement), rows in groups.items():
        sections = list(dict.fromkeys(f"«{row['section'] or 'Вводная часть'}» ({row['reason'].lower()})" for row in rows))
        parts.append('Замена ' + template_link(previous) + ' на ' + template_link(replacement) +
                     (' в разделе ' if len(sections) == 1 else ' в разделах ') + ', '.join(sections))
    return '. '.join(parts)
