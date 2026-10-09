from dataclasses import dataclass


@dataclass(frozen=True)
class Processor:
    slug: str
    title: str
    short_title: str
    description: str
    enabled: bool = False


PROCESSORS = (
    Processor("obkat", "Обсуждение категорий", "ОБКАТ", "Структура обсуждений, итоги и открытые номинации", True),
    Processor("maintenance", "Шаблоны о проблемах", "Шаблоны", "Расстановка дат, замена параметров и разворачивание RQ", True),
    Processor("translations", "Плохие и грубые переводы", "Переводы", "Язык оригинала и данные о переводах", True),
    Processor("sections", "Шаблоны разделов", "Разделы", "Взаимная замена шаблонов по содержимому раздела", True),
    Processor("categories", "Категории обслуживания", "Категории", "Создание месячных категорий и проверка оформления", True),
)


@dataclass(frozen=True)
class Task:
    slug: str
    processor: str
    title: str
    description: str
    enabled: bool = False
    schedules: tuple[str, ...] = ()
    daily_time: str = "03:00"
    month_end_time: str = "23:30"


TASKS = (
    Task("obkat", "obkat", "Зачёркивание и сводка", "Зачёркивает завершённые номинации и обновляет страницу «Текущие обсуждения».", True, ("after_edit", "month_end")),
    Task("maintenance-dates", "maintenance", "Даты в шаблонах о проблемах", "Вставляет дату установки шаблона по истории статьи.", True, ("daily",)),
    Task("maintenance-rq", "maintenance", "Замена параметров RQ", "Заменяет старые параметры RQ шаблонами о проблемах с датами.", True, ("daily",)),
    Task("maintenance-rq-unwrap", "maintenance", "Разворачивание одиночного RQ", "Убирает обёртку RQ, если внутри остался один шаблон о проблеме.", True, ("daily",)),
    Task("translations-categories", "translations", "Через категории", "Заполняет язык и оригинал перевода по комментарию к созданию статьи.", True, ("daily",), "04:00"),
    Task("translations-talk", "translations", "Со страниц обсуждения", "Заполняет язык и оригинал по шаблону «Переведённая статья» на СО.", True, ("daily",), "04:00"),
    Task("sections-empty-to-fill", "sections", "Пустой раздел → Дополнить раздел", "Меняет шаблон, если в разделе или его подразделах есть содержимое.", True, ("daily",), "04:00"),
    Task("sections-fill-to-empty", "sections", "Дополнить раздел → Пустой раздел", "Меняет шаблон, если раздел и его подразделы не содержат текста.", True, ("daily",), "04:00"),
    Task("categories-create", "categories", "Создание категорий", "Создаёт непустые месячные категории по форматам из вики-таблицы.", True, ("daily",), "05:00"),
    Task("categories-format", "categories", "Проверка оформления", "Исправляет оформление новых категорий; в конце месяца проверяет все.", True, ("weekly", "month_end"), "05:00", "05:00"),
)


def get_processor(slug):
    return next((p for p in PROCESSORS if p.slug == slug), None)


def get_task(slug):
    return next((task for task in TASKS if task.slug == slug), None)
