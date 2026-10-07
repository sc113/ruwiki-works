"""Editable processing configuration, separate from deployment credentials."""
import copy
import re

from ...schedules import parse_clock

from . import TASK_SLUGS

META_CATEGORY = "Категория:Отслеживающие категории:Статьи с шаблонами-сообщениями без указанных дат"
RQ_CATEGORY = "Категория:Википедия:Статьи к замене параметров шаблона rq"
UNWRAP_CATEGORY = "Категория:Википедия:Статьи с одной проблемой в шаблоне rq"
SECTION_CATEGORY = "Категория:Шаблоны:Шаблоны-сообщения для разделов статей"

RQ_PARAM_TEMPLATES = {
    "birth": "нет даты рождения", "birthplace": "нет места рождения", "burialplace": "нет места захоронения",
    "cat": "нет категорий", "check": "проверить факты", "checktranslate": "плохой перевод",
    "cleanup": "переработать", "coord": "нет координат", "death": "нет даты смерти",
    "deathplace": "нет места смерти", "empty": "дописать", "global": "глобализировать",
    "grammar": "опечатки", "img": "нет иллюстрации", "infobox": "нет карточки", "isbn": "оформить литературу",
    "linkless": "изолированная статья", "morefootnotes": "частично без сносок", "neutral": "проверить нейтральность",
    "nolead": "нет преамбулы", "notability": "значимость", "overlinked": "много внутренних ссылок",
    "part": "нет разделов", "patronymic": "отчество", "pronun": "нужна транскрипция", "recat": "уточнить категории",
    "refless": "нет сносок", "renew": "обновить", "shortlead": "короткая преамбула", "sources": "нет источников",
    "sources-cleanup": "чистить ссылки", "stress": "нужно ударение", "style": "стиль статьи", "taxobox": "нет таксобокса",
    "tex": "оформить формулы", "translate": "закончить перевод", "underlinked": "мало внутренних ссылок",
    "wikify": "плохое оформление", "yo": "ёфицировать", "dewikify": "много внутренних ссылок",
    "footnotes": "нет сносок", "image": "нет иллюстрации", "images": "нет иллюстрации", "introduction": "нет преамбулы",
    "makeup": "плохое оформление", "pre": "короткая преамбула", "ref": "нет сносок", "source": "нет источников",
    "stub": "дописать", "taxbox": "нет таксобокса", "обновить": "обновить",
}

COMMON_FIELDS = (
    ("run_time", "Ежедневный запуск, МСК", "time", "Расписание"),
    ("search_mode", "Поиск даты в истории", "search", "Обработка"),
    ("max_revisions", "Лимит ревизий статьи", "number", "Обработка"),
    ("debug_output", "Подробный отладочный вывод", "bool", "Отладка"),
    ("autosave", "Автосохранение правок", "bool", "Обработка"),
    ("section_templates_category", "Категория шаблонов для разделов", "text", "Шаблоны разделов"),
    ("section_templates_auto", "Обновлять список из категории", "bool", "Шаблоны разделов"),
    ("section_templates", "Шаблоны для разделов", "list", "Шаблоны разделов"),
)


def fields(slug):
    if slug == "maintenance-rq-unwrap":
        return (
            ("run_time", "Ежедневный запуск, МСК", "time", "Расписание"),
            ("target_category", "Категория одиночных RQ", "text", "Источники"),
            ("autosave", "Автосохранение правок", "bool", "Обработка"),
            ("debug_output", "Подробный отладочный вывод", "bool", "Отладка"),
        )
    specific = (("meta_category", "Родительская категория без дат", "text", "Источники"),) if slug == TASK_SLUGS[0] else (
        ("rq_category", "Категория замены параметров RQ", "text", "Источники"),
        ("rq_start_prefix", "Начать с префикса сортировки", "text", "Источники"),
        ("rq_param_templates", "Соответствия параметров и шаблонов", "mapping", "Параметры RQ"),
        ("rq_skip_params", "Параметры, при которых пропускается статья", "list", "Параметры RQ"),
    )
    return COMMON_FIELDS + specific


def defaults(slug):
    if slug == "maintenance-rq-unwrap":
        return dict(run_time="03:00", target_category=UNWRAP_CATEGORY, autosave=True, debug_output=False)
    values = dict(run_time="03:00", search_mode=1, max_revisions=0, debug_output=False,
                  autosave=True, section_templates_category=SECTION_CATEGORY, section_templates_auto=True,
                  section_templates=[])
    if slug == TASK_SLUGS[0]:
        values["meta_category"] = META_CATEGORY
    elif slug == TASK_SLUGS[1]:
        values.update(rq_category=RQ_CATEGORY, rq_start_prefix="", rq_param_templates=copy.deepcopy(RQ_PARAM_TEMPLATES),
                      rq_skip_params=["all"])
    else:
        raise ValueError("Unknown maintenance task")
    return values


def get_config(store, slug):
    config = defaults(slug)
    config.update(store.get_state(slug + ":config", {}))
    # Single-article settings must never narrow scheduled processing.
    config.pop("debug_article", None)
    loaded = store.get_state(slug + ":sections", {})
    if config.get("section_templates_auto") and loaded.get("source") == config["section_templates_category"]:
        config["section_templates"] = loaded["templates"]
    return config


def template_name(value):
    value = re.sub(r"^(?:Шаблон|Template):", "", value.strip(), flags=re.I).replace("_", " ")
    value = " ".join(value.split())
    if not value or len(value) > 255 or any(c in value for c in "|{}[]<>#"):
        raise ValueError("Некорректное название шаблона: " + value[:80])
    return value


def parse_run_time(value):
    return parse_clock(value)


def parse_form(form, slug):
    config = {}
    for key, label, kind, _ in fields(slug):
        raw = form.get(key, "").strip()
        if kind == "bool":
            config[key] = raw == "on"
        elif kind in {"search", "number"}:
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(label + ": требуется целое число") from None
            if key == "search_mode" and value not in {1, 2, 3} or key == "max_revisions" and not 0 <= value <= 1_000_000:
                raise ValueError(label + ": недопустимое значение")
            config[key] = value
        elif kind == "list":
            values = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(values) > 1000 or any(len(value) > 255 for value in values):
                raise ValueError(label + ": список слишком большой")
            config[key] = list(dict.fromkeys(template_name(v) if key == "section_templates" else v.casefold() for v in values))
        elif kind == "mapping":
            mapping = {}
            for line in raw.splitlines():
                if not line.strip():
                    continue
                name, separator, target = line.partition("=")
                name = name.strip().casefold()
                if not separator or not re.fullmatch(r"[\w-]{1,80}", name) or name == "topic" or name in mapping:
                    raise ValueError("Соответствия RQ: нужны уникальные строки «параметр=шаблон»")
                mapping[name] = template_name(target)
            config[key] = mapping
            if len(mapping) > 1000:
                raise ValueError("Соответствия RQ: список слишком большой")
        else:
            if len(raw) > 255 or any(c in raw for c in "\r\n|{}[]<>"):
                raise ValueError(label + ": некорректное значение")
            if key.endswith("category") and not raw.startswith("Категория:"):
                raise ValueError(label + ": название должно начинаться с «Категория:»")
            config[key] = parse_run_time(raw) if key == "run_time" else raw
    return config


def form_value(config, key, kind):
    value = config[key]
    if kind == "list":
        return "\n".join(value)
    if kind == "mapping":
        return "\n".join(f"{name}={target}" for name, target in value.items())
    return value
