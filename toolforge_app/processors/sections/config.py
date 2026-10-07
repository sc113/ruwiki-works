"""Editable configuration for section template processing."""
import copy

from ..maintenance.config import template_name, form_value
from ...schedules import parse_clock

IGNORED_TEMPLATES = [
    'Основная статья', 'См. также', 'Оценки игры', 'Оценки серии игр',
    'Рейтинги альбома', 'Перевести', 'Системные требования', 'Внешние медиафайлы',
    'Перевести раздел', 'Запланированное событие', 'Clear', 'Сдвоенное изображение',
    'Нет источников в разделе', 'Врезка', 'Изолированная статья', 'Спам-ссылки',
]


def fields(slug):
    return [
        ('source_template', 'Искать шаблон', 'text', 'Источники'),
        ('replacement_template', 'Заменять на', 'text', 'Источники'),
        ('ignored_templates', 'Шаблоны, которые не считаются содержимым раздела', 'list', 'Проверка раздела'),
        ('autosave', 'Автосохранение правок', 'bool', 'Обработка'),
        ('resume', 'Пропускать уже проверенные статьи', 'bool', 'Обработка'),
        ('limit_articles', 'Лимит статей за запуск · 0 — все', 'number', 'Обработка'),
        ('embeddedin_limit', 'Лимит списка на шаблон · 0 — все', 'number', 'Источники'),
        ('redirects_cache_days', 'Кеш перенаправлений · дни', 'number', 'Кеш'),
        ('articles_cache_days', 'Кеш списка статей · дни, 0 — обновлять', 'number', 'Кеш'),
        ('debug_output', 'Подробная диагностика разделов', 'bool', 'Отладка'),
        ('run_time', 'Ежедневный запуск, МСК', 'time', 'Расписание'),
    ]


def defaults(slug):
    source, replacement = ('Пустой раздел', 'Дополнить раздел') if slug == 'sections-empty-to-fill' else ('Дополнить раздел', 'Пустой раздел')
    return dict(source_template=source, replacement_template=replacement,
                ignored_templates=list(IGNORED_TEMPLATES), autosave=True, resume=True,
                limit_articles=0, embeddedin_limit=0, redirects_cache_days=14, articles_cache_days=0,
                debug_output=False, run_time='04:00')


def get_config(store, slug):
    result = defaults(slug)
    result.update(copy.deepcopy(store.get_state(slug + ':config', {})))
    result.pop('debug_article', None)
    return result


def parse_form(form, slug):
    result = {}
    for key, label, kind, _ in fields(slug):
        value = form.get(key, '').strip()
        if kind == 'bool':
            result[key] = value == 'on'
        elif kind == 'time':
            result[key] = parse_clock(value)
        elif kind == 'number':
            if not value.isascii() or not value.isdigit() or not 0 <= int(value) <= 1000000:
                raise ValueError(label + ': нужно целое число от 0 до 1000000')
            result[key] = int(value)
        elif kind == 'list':
            values = [template_name(name.strip()) for name in value.splitlines() if name.strip()]
            if len(values) > 100:
                raise ValueError(label + ': не более 100 шаблонов')
            result[key] = list(dict.fromkeys(values))
        elif key in {'source_template', 'replacement_template'}:
            result[key] = template_name(value)
        else:
            if len(value) > 255 or any(c in value for c in '\r\n|{}[]<>\x00'):
                raise ValueError(label + ': некорректное название')
            result[key] = value
    from .transform import normalize
    if normalize(result['source_template']) == normalize(result['replacement_template']):
        raise ValueError('Шаблоны для поиска и замены должны отличаться')
    if normalize(result['source_template']) in {normalize(name) for name in result['ignored_templates']}:
        raise ValueError('Искомый шаблон не должен входить в список игнорируемых')
    return result
