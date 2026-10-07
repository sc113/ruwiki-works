from toolforge_app.processors.obkat.analyzer import analyze_text
from toolforge_app.processors.obkat.formatter import format_text
from toolforge_app.processors.obkat.report import build_report, open_nominations, page_title, title_month
from toolforge_app.processors.obkat.table import generate_wiki_table
from toolforge_app.storage import dump
from conftest import CLOSED, OPEN


def test_closed_nomination_struck_without_striking_date_or_itog():
    result = format_text(CLOSED)
    assert "=== <s>Категория:Пример</s> ===" in result
    assert "== 5 октября 2026 ==" in result
    assert "==== Итог ====" in result
    assert format_text(result) == result


def test_no_blank_line_growth_when_nothing_changes():
    assert format_text(OPEN) == OPEN
    assert format_text(format_text(OPEN)) == OPEN


def test_spacing_is_separate_and_idempotent():
    raw = "{{ОБК-Навигация}}\n==5 октября 2026==\n\n===Категория:Пример===\n\nТекст\n\n\n"
    assert format_text(raw) == raw
    formatted = format_text(raw, spacing=True)
    assert "== 5 октября 2026 ==" in formatted
    assert "=== Категория:Пример ===\nТекст" in formatted
    assert "\n\n\n" not in formatted
    assert format_text(formatted, spacing=True) == formatted


def test_open_nomination_is_nuance_and_duplicate_anchor_is_numbered():
    text = OPEN + "=== Категория:Пример ===\nДругой текст.\n"
    issues = analyze_text(text)
    rows = [dict(title=page_title("2026-10"), month="2026-10", text=text, issues=dump(issues),
                 missing=False, revision=1, checked_at=1, origin="wiki")]
    report = build_report(rows)
    open_items = [i for i in report["items"] if i["type"] == "no_itog"]
    assert all(i["section"] == "nuances" for i in open_items)
    assert any(i["url"].endswith("_2") for i in report["items"])


def test_index_contains_only_open_nominations():
    index = generate_wiki_table({"2026-10": OPEN, "2026-09": format_text(CLOSED)})
    assert "** Категория:Пример" in index
    assert "|2026-10|" in index
    assert "|2026-09|" not in index


def test_scope_accepts_only_monthly_discussion_titles():
    assert title_month(page_title("2026-10")) == "2026-10"
    assert title_month("Википедия:Обсуждение категорий/Текущие обсуждения") is None
    assert title_month("Участник:admin") is None


def test_open_list_merges_partial_findings_for_the_same_nomination():
    text = OPEN + "==== <s>Подноминация А</s> ====\nОбсуждение А.\n===== Итог =====\nГотово.\n==== Подноминация Б ====\nОбсуждение Б.\n"
    report = build_report([dict(title=page_title("2026-10"), month="2026-10", text=text,
        issues=dump(analyze_text(text)), missing=False, revision=1, checked_at=1, origin="wiki")])
    opened = open_nominations(report)
    assert len(opened) == 1
    assert opened[0]["partial"] and opened[0]["type"] == "sub_itog_no_main"
    assert opened[0]["with_itog"] == 1 and opened[0]["open_subs"] == 1
