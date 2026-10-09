"""Independent, validated settings for category actions."""
import copy
import re

from .formats import SOURCE_PAGE
from ...schedules import parse_clock


def defaults(slug):
    return dict(source_page=SOURCE_PAGE, category_prefix='Википедия:', check_simple=True,
                check_complex=True, start_year=2004, start_month=10, autosave=True,
                resume=True, limit_categories=0, run_time='05:00' if slug == 'categories-format' else '04:00')


def fields(slug):
    result = [('source_page', 'Вики-страница с форматами', 'text', 'Источники'),
              ('category_prefix', 'Префикс категорий', 'text', 'Источники'),
              ('check_simple', 'Обычные форматы · GenerateTemplates2', 'bool', 'Форматы'),
              ('check_complex', 'Сложные форматы · GenerateTemplates', 'bool', 'Форматы'),
              ('autosave', 'Автосохранение правок', 'bool', 'Обработка'),
              ('limit_categories', 'Лимит категорий за запуск · 0 — все', 'number', 'Обработка'),
              ('run_time', 'Ежедневный запуск, МСК', 'time', 'Расписание')]
    if slug == 'categories-format':
        result += [('start_year', 'Проверять начиная с года', 'number', 'Обработка'),
                   ('start_month', 'Начиная с месяца · 1–12', 'number', 'Обработка'),
                   ('resume', 'Пропускать неизменённые проверенные категории', 'bool', 'Обработка')]
    return result


def get_config(store, slug):
    result = defaults(slug)
    result.update(copy.deepcopy(store.get_state(slug + ':config', {})))
    result.pop('debug_article', None)
    return result


def parse_form(form, slug):
    result = defaults(slug)
    for key, label, kind, _ in fields(slug):
        value = form.get(key, '').strip()
        if kind == 'bool':
            result[key] = value == 'on'
        elif kind == 'time':
            result[key] = parse_clock(value)
        elif kind == 'number':
            limits = (2001, 2100) if key == 'start_year' else (1, 12) if key == 'start_month' else (0, 1000000)
            if not value.isascii() or not value.isdigit() or not limits[0] <= int(value) <= limits[1]:
                raise ValueError(f'{label}: от {limits[0]} до {limits[1]}')
            result[key] = int(value)
        else:
            if not value or len(value) > 255 or re.search(r'[\r\n|{}\[\]<>\x00]', value):
                raise ValueError(label + ': некорректное название')
            result[key] = value.replace('_', ' ')
    if not (result['check_simple'] or result['check_complex']):
        raise ValueError('Выберите хотя бы один тип форматов')
    return result
