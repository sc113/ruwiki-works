import html
import json
import re
from collections import Counter
from urllib.parse import quote

MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
PREFIX = "Википедия:Обсуждение категорий/"
TABLE_TITLE = PREFIX + "Текущие обсуждения"
CHECKS = {
    "orphan_itog": "Итог без родительской номинации",
    "itog_under_date": "Итог под датой",
    "itog_under_itog": "Итог вложен в другой итог",
    "struck_date": "Зачёркнутая дата",
    "wrong_itog_level": "Итог на неправильном уровне",
    "service_wrong_level": "Служебный раздел на неправильном уровне",
    "itog_under_service": "Итог под служебным разделом",
    "level_skip": "Пропуск уровня заголовков",
    "duplicate_itog": "Дублирующиеся итоги",
    "empty_nomination": "Пустая номинация",
    "date_order": "Неправильный порядок дат",
    "unclosed_tag": "Незакрытые теги зачёркивания",
    "sub_itog_no_main": "Подитоги без основного итога",
    "duplicate_nomination": "Дублирующиеся номинации",
    "mismatched_level": "Разные уровни в начале и конце заголовка",
    "same_day_duplicate": "Дубликат за один день",
    "group_for_one": "Раздел «По всем» для одной номинации",
    "not_struck": "Завершённая номинация не зачёркнута",
    "no_itog": "Открытая номинация",
}
NUANCES = {"no_itog", "sub_itog_no_main"}

FIX_GUIDANCE = {
    "orphan_itog": "Определите, к какой номинации относится итог, и поместите его непосредственно под её заголовком, на один уровень глубже.",
    "itog_under_date": "Перенесите итог под соответствующую номинацию. Под датой должны находиться заголовки номинаций, а не итогов.",
    "itog_under_itog": "Разместите окончательный «Итог» рядом с предварительным итогом, на том же уровне, чтобы оба относились к номинации.",
    "struck_date": "Уберите теги <s> и </s> из заголовка даты. Зачёркивать нужно закрытые номинации, а не даты.",
    "service_wrong_level": "Проверьте, к какой номинации относится служебный раздел, и вложите его в неё. Такие разделы должны находиться на уровне 4 или глубже.",
    "itog_under_service": "Вынесите итог из служебного раздела и разместите его непосредственно под номинацией, на один уровень глубже её заголовка.",
    "level_skip": "Проверьте вложенность раздела и число знаков «=». Дочерний заголовок должен быть на один уровень глубже родителя, без пропуска уровня.",
    "duplicate_itog": "Сопоставьте соседние итоги. Если это один раздел, объедините заголовки, сохранив текст, реплики и подписи участников.",
    "empty_nomination": "Проверьте, не потеряно ли обоснование. Дополните номинацию или уберите случайный пустой заголовок.",
    "date_order": "Переместите раздел даты целиком, вместе с обсуждениями: более поздние дни должны находиться выше более ранних.",
    "unclosed_tag": "Проверьте парность тегов <s> и </s> в заголовке: добавьте отсутствующий тег или уберите лишний.",
    "duplicate_nomination": "Сравните повторные номинации. Если обсуждается один вопрос, объедините записи с сохранением реплик; обоснованную повторную номинацию оставьте.",
    "mismatched_level": "Сделайте одинаковое число знаков «=» в начале и конце заголовка, выбрав уровень по его месту в обсуждении.",
    "same_day_duplicate": "Сравните запись с первым вхождением. Если это одна номинация, объедините обсуждение под одним заголовком, сохранив реплики и подписи.",
    "group_for_one": "Проверьте область действия «По всем». Для одной подноминации перенесите текст в неё; для нескольких расположите общий раздел на одном уровне с ними.",
    "not_struck": "Зачеркните заголовки закрытой номинации и завершённых подноминаций тегами <s>…</s>. Дату и заголовок «Итог» не зачёркивайте.",
}


def fix_guidance(issue):
    if issue["type"] == "wrong_itog_level":
        expected = issue.get("expected_level", issue.get("level", 3) + 1)
        marks = "=" * expected
        return f"Измените заголовок итога на {marks} Итог {marks} (уровень {expected}), сохранив текст итога."
    return FIX_GUIDANCE.get(issue["type"], "Проверьте оформление этого раздела в Википедии.")


def open_nominations(report):
    """One line per nomination, merging its open/partial findings."""
    nominations = {}
    for item in report["items"]:
        if item["type"] not in NUANCES:
            continue
        key = (item["page"], item["line"])
        if key not in nominations or item["type"] == "sub_itog_no_main":
            nominations[key] = {**item, "partial": item["type"] == "sub_itog_no_main"}
    return sorted(nominations.values(), key=lambda item: (item["month"], -item["line"]), reverse=True)


def page_title(month):
    year, number = month.split("-")
    return f"{PREFIX}{MONTHS[int(number) - 1]} {year}"


def title_month(title):
    match = re.fullmatch(re.escape(PREFIX) + r"(" + "|".join(MONTHS) + r") (\d{4})", title)
    return f"{match[2]}-{MONTHS.index(match[1]) + 1:02d}" if match else None


def month_range(start, now):
    year, month = map(int, start.split("-"))
    if not 1 <= month <= 12 or year < 2000:
        raise ValueError("Invalid start month")
    while (year, month) <= (now.year, now.month):
        yield f"{year}-{month:02d}"
        month += 1
        if month == 13:
            year, month = year + 1, 1


def plain_title(value):
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"\[\[:?([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], value)
    value = re.sub(r"\{\{(?:cl|категория)\|([^}]+)\}\}", r"Категория:\1", value, flags=re.I)
    return html.unescape(value.replace("'''", "").replace("''", "")).strip("= ")


def wiki_url(title, heading=""):
    url = "https://ru.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="/:()")
    return url + ("#" + quote(plain_title(heading).replace(" ", "_"), safe="()") if heading else "")


def issue_detail(issue):
    kind = issue["type"]
    if kind == "sub_itog_no_main":
        return f"Подноминаций: {issue['total_subs']}; с итогом: {issue['with_itog']}; без итога: {issue['open_subs']}"
    if kind in {"same_day_duplicate", "duplicate_nomination"}:
        return f"Первое вхождение: строка {issue['first_line']}"
    if kind == "mismatched_level":
        return f"Уровни {issue['start']} и {issue['end']}"
    if kind == "level_skip":
        return f"Переход с уровня {issue['prev_level']} на {issue['level']}"
    if kind == "date_order":
        return f"День {issue['curr_day']} после дня {issue['prev_day']}"
    if kind == "unclosed_tag":
        return f"Открывающих тегов: {issue['open']}; закрывающих: {issue['close']}"
    if "parent_title" in issue:
        return "Родитель: " + plain_title(issue["parent_title"])
    return ""


def build_report(page_rows):
    items = []
    for page in page_rows:
        if page["missing"]:
            continue
        anchors, seen = {}, Counter()
        for number, line in enumerate(page["text"].splitlines(), 1):
            if re.match(r"^=+\s*.+\s*=+\s*$", line.strip()):
                heading = plain_title(line)
                seen[heading] += 1
                anchors[number] = heading + (f"_{seen[heading]}" if seen[heading] > 1 else "")
        for issue in json.loads(page["issues"]):
            heading = issue.get("title", issue.get("parent_title", ""))
            items.append({**issue, "page": page["title"], "month": page["month"],
                "label": CHECKS.get(issue["type"], issue["type"]),
                "section": "nuances" if issue["type"] in NUANCES else "problems",
                "display_title": plain_title(heading), "detail": issue_detail(issue),
                "url": wiki_url(page["title"], anchors.get(issue["line"], heading)), "revision": page["revision"],
                "checked_at": page["checked_at"], "origin": page["origin"]})
    return {"items": items, "counts": dict(Counter(i["type"] for i in items)),
        "problems": sum(i["section"] == "problems" for i in items),
        "nuances": sum(i["section"] == "nuances" for i in items),
        "pages": sum(not p["missing"] for p in page_rows),
        "oldest_check": min((p["checked_at"] for p in page_rows if not p["missing"]), default=0),
        "latest_check": max((p["checked_at"] for p in page_rows), default=0)}


def report_txt(report):
    lines = ["ОТЧЁТ ОБ АНАЛИЗЕ СТРУКТУРЫ", "=" * 70]
    for kind, label in CHECKS.items():
        items = [i for i in report["items"] if i["type"] == kind]
        if items:
            lines.extend(["", f"{label}: {len(items)}", "-" * 40])
            lines.extend(f"{i['month']}.txt:{i['line']} - {i.get('title', '')}" +
                         (f" ({i['detail']})" if i["detail"] else "") for i in items)
    return "\n".join(lines) + "\n"
