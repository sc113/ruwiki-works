"""Settings for annual discussion processing."""
import copy

from ...schedules import parse_clock


def defaults(slug):
    return dict(start_year=2009, autosave=True, spacing=True, run_time='06:00')


def fields(slug):
    return [('start_year', 'Проверять начиная с года', 'number', 'Источники'),
            ('autosave', 'Автосохранение правок', 'bool', 'Обработка'),
            ('spacing', 'Выравнивать пробелы и пустые строки', 'bool', 'Обработка'),
            ('run_time', 'Ежедневный запуск, МСК', 'time', 'Расписание')]


def get_config(store, slug):
    result = defaults(slug)
    result.update(copy.deepcopy(store.get_state(slug + ':config', {})))
    result.pop('debug_article', None)
    return result


def parse_form(form, slug):
    raw = str(form.get('start_year', '')).strip()
    if not raw.isascii() or not raw.isdigit() or not 2009 <= int(raw) <= 2099:
        raise ValueError('Начальный год: от 2009 до 2099')
    return dict(start_year=int(raw), autosave=form.get('autosave') == 'on',
                spacing=form.get('spacing') == 'on', run_time=parse_clock(form.get('run_time', '')))
