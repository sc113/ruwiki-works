"""Read category definitions as data from the three supported wiki table blocks."""
import hashlib
import html
import json
import re

import mwparserfromhell

from ...wiki import WikiError

SOURCE_PAGE = 'Проект:Технические работы/Список сообщений для статей/Категории для создания'
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
          'августа', 'сентября', 'октября', 'ноября', 'декабря')
GROUPS = {'GenerateTemplates: надстрочные': 'complex',
          'GenerateTemplates2: надстрочные': 'simple', 'GenerateTemplates2: боксы': 'simple'}
TOKENS = ('text_month_year', 'month_name', 'year', 'yyyy-mm')
MONTH_DATE = re.compile(r' с (' + '|'.join(MONTHS) + r') (\d{4})(?: года)?$')


def clean_cell(code):
    value = str(code).strip()
    # nowiki protects template pipes and placeholders; its contents are literal data.
    value = re.sub(r'<nowiki\s*>(.*?)</nowiki>', lambda match: match[1], value, flags=re.S | re.I)
    return html.unescape(value).strip()


def parse_formats(text):
    mappings = {'simple': {}, 'complex': {}}
    found = set()
    for table in mwparserfromhell.parse(text).filter_tags(recursive=False):
        if str(table.tag).lower() != 'table':
            continue
        group = None
        for row in table.contents.filter_tags(recursive=False):
            cells = ([row] if str(row.tag).lower() == 'th' else
                     row.contents.filter_tags(recursive=False) if str(row.tag).lower() == 'tr' else [])
            headers = [clean_cell(cell.contents) for cell in cells if str(cell.tag).lower() == 'th']
            if headers:
                group = headers[0] if len(headers) == 1 and headers[0] in GROUPS else None
                if group:
                    found.add(group)
                continue
            if not group:
                continue
            cells = [cell for cell in cells if str(cell.tag).lower() == 'td']
            if len(cells) != 2:
                raise WikiError('category-formats-invalid')
            pattern, content = [clean_cell(cell.contents) for cell in cells]
            pattern = re.sub(r'<[^>]+>|_', lambda match: ' ' if match[0] == '_' else match[0], pattern.strip('"'))
            pattern = pattern.removeprefix('Категория:').removeprefix('Википедия:')
            tokens = re.findall(r'<([^<>]+)>', pattern + content)
            if (not pattern or not content or any(token not in TOKENS for token in tokens)
                    or not ('<text_month_year>' in pattern or '<month_name>' in pattern and '<year>' in pattern)
                    or any(char in pattern for char in '\n\r|{}[]\x00') or len(pattern) > 255):
                raise WikiError('category-formats-invalid')
            content = content.replace('\\n', '\n')
            kind = GROUPS[group]
            if kind == 'simple' and any(char in content for char in '\n\r{}[]\x00'):
                raise WikiError('category-formats-invalid')
            if any(pattern in mappings[other] and mappings[other][pattern] != content for other in mappings):
                raise WikiError('category-formats-conflict')
            if pattern in mappings['simple'] and kind == 'complex' or pattern in mappings['complex'] and kind == 'simple':
                raise WikiError('category-formats-conflict')
            mappings[kind][pattern] = content
    if found != set(GROUPS) or not all(mappings.values()):
        raise WikiError('category-formats-invalid')
    mappings['signature'] = hashlib.sha256(json.dumps(mappings, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return mappings


def date_of(title):
    match = MONTH_DATE.search(title.replace('_', ' '))
    return (int(match[2]), MONTHS.index(match[1]) + 1) if match else None


def expected_category(title, formats, config):
    title = title.replace('_', ' ')
    prefix = 'Категория:' + config['category_prefix']
    date = date_of(title)
    if not title.startswith(prefix) or not date:
        return None
    year, month = date
    values = dict(text_month_year=f'{MONTHS[month - 1]} {year}', month_name=MONTHS[month - 1],
                  year=str(year), **{'yyyy-mm': f'{year:04d}-{month:02d}'})
    def render(value):
        for key, replacement in values.items():
            value = value.replace('<' + key + '>', replacement)
        return value
    for kind in ('complex', 'simple'):
        if not config['check_' + kind]:
            continue
        for pattern, content in formats[kind].items():
            if not title.removeprefix(prefix).startswith(pattern.split('<', 1)[0]):
                continue
            if title.removeprefix(prefix) != render(pattern):
                continue
            if kind == 'simple':
                params = render(content)
                text = '{{Категория к ежемесячной очистке|' + params + '}}'
                summary = 'Создание категории обслуживания с шаблоном ' + text
            else:
                text = render(content)
                summary = 'Создание категории обслуживания для надстрочного шаблона'
            return dict(text=text, kind=kind, pattern=pattern, summary=summary,
                        signature=hashlib.sha256(text.encode()).hexdigest())
    return None


def refresh_formats(wiki, store, config):
    import time
    base = wiki.fetch(config['source_page'])
    if base.missing:
        raise WikiError('category-formats-missing')
    key = 'categories:formats:' + hashlib.sha256(config['source_page'].encode()).hexdigest()[:40]
    cached = store.get_state(key)
    if cached and cached['revision'] == base.revision:
        return cached
    formats = dict(parse_formats(base.text), revision=base.revision, source=base.title, at=time.time())
    store.set_state(key, formats)
    return formats


def saved_formats(store, config):
    return store.get_state('categories:formats:' + hashlib.sha256(config['source_page'].encode()).hexdigest()[:40])
