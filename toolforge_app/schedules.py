"""Shared timer definitions, validation, persistence and deadline calculations."""
import calendar
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .processors import get_task

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class Timer:
    kind: str
    key: str
    label: str
    input_type: str
    unit: str
    default: object
    minimum: int | None = None
    maximum: int | None = None


TIMERS = {
    "daily": Timer("daily", "run_time", "Ежедневно", "time", "МСК", "03:00"),
    "after_edit": Timer("after_edit", "quiet_minutes", "После последней правки", "number", "мин", "quiet_minutes", 1, 1440),
    "month_end": Timer("month_end", "month_end_time", "Форматирование в конце месяца", "time", "МСК", "month_end_time"),
    "search": Timer("search", "search_minutes", "Поиск изменений", "number", "мин", 360, 1, 10080),
    "weekly": Timer("weekly", "run_time", "Еженедельно", "time", "МСК", "05:00"),
    "weekday": Timer("weekday", "weekday", "День недели", "select", "", 0, 0, 6),
}


def timer_definitions(slug):
    task = get_task(slug)
    if not task or not task.schedules:
        return []
    definitions = [TIMERS['search']]
    for kind in task.schedules:
        if kind == 'weekly':
            definitions.append(TIMERS['weekday'])
        definitions.append(TIMERS[kind])
    return definitions


def get_schedule(settings, store, slug):
    saved = store.get_state(slug + ":config", {})
    result = {}
    for timer in timer_definitions(slug):
        if timer.kind == 'search':
            default = max(1, settings.poll_seconds // 60) if slug == 'obkat' else 360
        elif timer.kind == 'weekday':
            default = 0
        elif timer.kind == 'month_end' and slug != 'obkat':
            default = get_task(slug).month_end_time
        elif timer.kind in {'after_edit', 'month_end'}:
            default = getattr(settings, timer.default)
        else:
            default = get_task(slug).daily_time
        result[timer.key] = saved.get(timer.key, default)
    return result


def search_interval(settings, store, slug):
    return get_schedule(settings, store, slug)['search_minutes'] * 60


def schedule_fields(settings, store, slug):
    values = get_schedule(settings, store, slug)
    return [dict(kind=timer.kind, key=timer.key, label='Полная проверка в конце месяца'
                 if slug == 'categories-format' and timer.kind == 'month_end' else timer.label, input_type=timer.input_type,
                 unit=timer.unit, value=values[timer.key], minimum=timer.minimum, maximum=timer.maximum)
            for timer in timer_definitions(slug)]


def parse_clock(value):
    value = value.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Время запуска: используйте ЧЧ:ММ")
    return value


def parse_schedule(slug, values, *, partial=False):
    definitions = timer_definitions(slug)
    allowed = {timer.key for timer in definitions}
    if set(values) - allowed:
        raise ValueError("Неизвестный параметр расписания")
    if not definitions or not values or not partial and (allowed - {'search_minutes'}) - set(values):
        raise ValueError("Заполните все поля расписания")
    parsed = {}
    for timer in definitions:
        if timer.key not in values:
            continue
        if timer.input_type == "time":
            parsed[timer.key] = parse_clock(str(values[timer.key]))
        else:
            raw = str(values[timer.key]).strip()
            if not re.fullmatch(r"\d+", raw) or not timer.minimum <= int(raw) <= timer.maximum:
                raise ValueError(f"{timer.label}: от {timer.minimum} до {timer.maximum} минут")
            parsed[timer.key] = int(raw)
    return parsed


def save_schedule(settings, store, slug, values, actor, *, partial=False):
    parsed = parse_schedule(slug, values, partial=partial)
    previous = get_schedule(settings, store, slug)
    store.patch_state(slug + ":config", parsed)
    store.set_state(slug + ":schedule_updated", dict(at=time.time(), by=actor))
    reschedule_keys = ('run_time', 'weekday') if slug == 'obkat' else ('run_time', 'weekday', 'month_end_time')
    if any(key in parsed and parsed[key] != previous.get(key) for key in reschedule_keys):
        store.cancel_schedule(slug)
    if "quiet_minutes" in parsed and parsed["quiet_minutes"] != previous["quiet_minutes"]:
        # Observation performs API reads outside the short HTTP request.
        store.set_state(slug + ":retime_requested", uuid.uuid4().hex)
    return parsed


def after_edit_due(minutes, edited_at, now):
    return max(now, edited_at + minutes * 60)


def next_daily_time(value, now, zone=MOSCOW):
    local = datetime.fromtimestamp(now, zone)
    hour, minute = map(int, value.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def next_weekly_time(value, weekday, now, zone=MOSCOW):
    local = datetime.fromtimestamp(now, zone)
    hour, minute = map(int, value.split(':'))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - local.weekday()) % 7)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def next_task_time(settings, store, slug, now):
    task = get_task(slug)
    schedule = get_schedule(settings, store, slug)
    due = []
    if 'daily' in task.schedules:
        due.append(next_daily_time(schedule['run_time'], now))
    if 'weekly' in task.schedules:
        due.append(next_weekly_time(schedule['run_time'], schedule['weekday'], now))
    if 'month_end' in task.schedules:
        due.append(next_month_end_time(schedule['month_end_time'], now))
    return min(due) if due else None

def previous_task_time(settings, store, slug, now):
    task = get_task(slug)
    schedule = get_schedule(settings, store, slug)
    due = []
    if 'daily' in task.schedules:
        due.append(next_daily_time(schedule['run_time'], now) - 86400)
    if 'weekly' in task.schedules:
        due.append(next_weekly_time(schedule['run_time'], schedule['weekday'], now) - 7 * 86400)
    if 'month_end' in task.schedules:
        local = datetime.fromtimestamp(now, MOSCOW)
        year, number = local.year, local.month
        candidate = month_end_deadline(f'{year}-{number:02d}', schedule['month_end_time'])
        if candidate > now:
            year, number = (year - 1, 12) if number == 1 else (year, number - 1)
            candidate = month_end_deadline(f'{year}-{number:02d}', schedule['month_end_time'])
        due.append(candidate)
    return max(due) if due else None


def month_end_deadline(month, value, zone=MOSCOW):
    year, number = map(int, month.split("-"))
    hour, minute = map(int, value.split(":"))
    return datetime(year, number, calendar.monthrange(year, number)[1], hour, minute, tzinfo=zone).timestamp()


def next_month_end_time(value, now, zone=MOSCOW, last_completed=None):
    local = datetime.fromtimestamp(now, zone)
    year, number = local.year, local.month
    month = f"{year}-{number:02d}"
    candidate = month_end_deadline(month, value, zone)
    if candidate <= now or last_completed and month <= last_completed:
        year, number = (year + 1, 1) if number == 12 else (year, number + 1)
        candidate = month_end_deadline(f"{year}-{number:02d}", value, zone)
    return candidate
