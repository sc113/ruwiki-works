"""Read-only category inventories and cached template redirects."""
import hashlib
import time

import mwparserfromhell

from ...wiki import WikiError
from .config import get_config
from .history import normalize


def source_category(slug, config):
    key = {"maintenance-dates": "meta_category", "maintenance-rq": "rq_category",
           "maintenance-rq-unwrap": "target_category"}[slug]
    return config[key]


def scan(wiki, slug, config, now=None):
    root = source_category(slug, config)
    categories = [row["title"] for row in wiki.category_members(root, kind="subcat", namespace=14)] if slug == "maintenance-dates" else [root]
    rows, articles = [], {}
    for category in categories:
        titles = sorted({row["title"] for row in wiki.category_members(category, namespace=0)})
        rows.append({"title": category, "count": len(titles), "articles": titles})
        for title in titles:
            articles.setdefault(title, []).append(category)
    return {"source": root, "checked_at": time.time() if now is None else now, "total": len(articles),
            "categories": sorted(rows, key=lambda row: (-row["count"], row["title"])), "articles": articles}


def refresh_inventory(wiki, store, slug, now=None, *, config=None):
    inventory = scan(wiki, slug, config or get_config(store, slug), now)
    store.set_state(slug + ":inventory", inventory)
    store.set_state(slug + ":monitor_error", None)
    from ...issues import annotate_report
    annotate_report(store, slug, report(store, slug, config))
    return inventory


def load_sections(wiki, store, slug, config, *, force=False):
    if "section_templates_auto" not in config:
        return []
    if not config["section_templates_auto"] and not force:
        return config["section_templates"]
    cached = store.get_state(slug + ":sections", {})
    category = config["section_templates_category"]
    if force or cached.get("source") != category or time.time() - cached.get("at", 0) >= 86400:
        templates = sorted({row["title"].split(":", 1)[1] for row in wiki.category_members(category, namespace=10)})
        store.set_state(slug + ":sections", dict(source=category, at=time.time(), templates=templates))
        return templates
    return cached["templates"]


class Catalogue:
    def __init__(self, wiki, store):
        self.wiki, self.store = wiki, store

    def aliases(self, name):
        key = "maint:aliases:" + hashlib.sha256(name.encode()).hexdigest()[:40]
        cached = self.store.get_state(key)
        if cached and time.time() - cached["at"] < 86400:
            return cached["definition"]
        definition = self.wiki.template_aliases(name)
        self.store.set_state(key, {"at": time.time(), "definition": definition})
        return definition

    def category_templates(self, title):
        base = self.wiki.fetch(title)
        if base.missing:
            raise WikiError("category-missing")
        names = []
        for template in mwparserfromhell.parse(base.text).filter_templates():
            if normalize(template.name) == normalize("Категория к ежемесячной очистке"):
                names.extend(str(p.value).strip() for p in template.params if str(p.name).strip().isdigit() and str(p.value).strip())
        if not names:
            raise WikiError("category-templates-missing")
        return [self.aliases(name) for name in dict.fromkeys(names)]


ERROR_LABELS = {
    "revision-limit": "Превышен установленный лимит ревизий",
    "hidden-history": "Часть истории скрыта; дата требует ручной проверки",
    "incomplete-history": "Не получена полная история статьи",
    "ambiguous-section-history": "Неоднозначная история раздела",
    "ambiguous-template-occurrence": "Несколько одинаковых шаблонов в одном разделе; уточните даты установки вручную",
    "category-templates-missing": "В описании категории не найден перечень шаблонов",
    "template-missing": "Целевой шаблон не существует",
    "redirect": "Статья является перенаправлением",
    "bot-excluded": "Страница запрещает работу бота",
    "page-in-use": "Статья сейчас редактируется участником; проверка будет повторена в следующий запуск",
    "editconflict": "Версия статьи изменилась во время обработки",
    "network": "Не удалось получить данные Википедии",
}


def report(store, slug, config=None):
    config = config or get_config(store, slug)
    inventory = store.get_state(slug + ":inventory")
    source = source_category(slug, config)
    if inventory and inventory["source"] != source:
        inventory = None
    result = store.get_state(slug + ":result", {})
    current = inventory["articles"] if inventory else {}
    excluded = [dict(item, categories=current[item['title']]) for item in result.get('excluded', [])
                if result.get('source') == source and item['title'] in current]
    excluded_titles = {item['title'] for item in excluded}
    manual = [dict(item, categories=current[item["title"]]) for item in result.get("manual", [])
              if result.get("source") == source and item["title"] in current and item['title'] not in excluded_titles]
    return {"problems": len(manual), "nuances": 0, "pages": inventory["total"] if inventory else 0,
            "to_process": len(current) - len(excluded) if inventory else None, "latest_check": inventory["checked_at"] if inventory else None,
            "source": source, "categories": inventory["categories"] if inventory else [], "manual": manual,
            "excluded": excluded, "skipped": len(excluded), "skipped_articles": excluded,
            "pending_articles": [dict(title=title, categories=categories) for title, categories in sorted(current.items())
                                 if title not in excluded_titles],
            "after_run": result.get("at") if result.get("source") == source else None,
            "monitor_error": store.get_state(slug + ":monitor_error")}
