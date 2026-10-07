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
}


def timer_definitions(slug):
    task = get_task(slug)
    return [TIMERS['search'], *[TIMERS[kind] for kind in task.schedules]] if task and task.schedules else []


def get_schedule(settings, store, slug):
    saved = store.get_state(slug + ":config", {})
    return {timer.key: saved.get(timer.key, (max(1, settings.poll_seconds // 60) if slug == 'obkat' else 360)
            if timer.kind == 'search' else getattr(settings, timer.default)
            if timer.kind in {"after_edit", "month_end"} else get_task(slug).daily_time)
            for timer in timer_definitions(slug)}


def search_interval(settings, store, slug):
    return get_schedule(settings, store, slug)['search_minutes'] * 60


def schedule_fields(settings, store, slug):
    values = get_schedule(settings, store, slug)
    return [dict(kind=timer.kind, key=timer.key, label=timer.label, input_type=timer.input_type,
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
    if "run_time" in parsed and parsed["run_time"] != previous["run_time"]:
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
