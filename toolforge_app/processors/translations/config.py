"""Independent configuration for comment and talk-page extraction."""
from ..maintenance.config import template_name, form_value
from ...schedules import parse_clock

CATEGORIES = ["Категория:Википедия:Грубый перевод с неуказанного языка",
              "Категория:Википедия:Плохой перевод с неуказанного языка"]


def fields(slug):
    result = [("target_categories", "Категории статей", "list", "Источники"),
              ("target_templates", "Шаблоны перевода", "list", "Источники"),
              ("autosave", "Автосохранение правок", "bool", "Обработка"),
              ("debug_output", "Подробная диагностика", "bool", "Отладка"),
              ("run_time", "Ежедневный запуск, МСК", "time", "Расписание")]
    if slug == "translations-talk":
        result.insert(2, ("parse_talk_template", "Шаблон на СО", "text", "Источники"))
    return result


def defaults(slug):
    result = dict(target_categories=list(CATEGORIES), target_templates=["Грубый перевод", "Плохой перевод"],
                  autosave=True, debug_output=False, run_time="04:00")
    if slug == "translations-talk":
        result["parse_talk_template"] = "Переведённая статья"
    return result


def get_config(store, slug):
    result = defaults(slug)
    result.update(store.get_state(slug + ":config", {}))
    result.pop("debug_article", None)
    return result


def parse_form(form, slug):
    result = {}
    for key, label, kind, _ in fields(slug):
        value = form.get(key, "").strip()
        if kind == "bool":
            result[key] = value == "on"
        elif kind == "time":
            result[key] = parse_clock(value)
        elif kind == "list":
            values = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
            if not values or len(values) > 100:
                raise ValueError(label + ": укажите от 1 до 100 названий")
            if key == "target_templates":
                values = [template_name(name) for name in values]
            elif any(not name.startswith("Категория:") or len(name) > 255 or any(c in name for c in "|{}[]<>#") for name in values):
                raise ValueError(label + ": нужны полные названия «Категория:…»")
            result[key] = list(dict.fromkeys(values))
        elif key == "parse_talk_template":
            result[key] = template_name(value)
        else:
            if len(value) > 255 or any(c in value for c in "\r\n|{}[]<>"):
                raise ValueError(label + ": некорректное название")
            result[key] = value
    return result
