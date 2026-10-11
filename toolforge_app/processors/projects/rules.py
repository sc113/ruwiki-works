"""Shared structural checks adapted to nominations at heading level two."""
import re
from collections import Counter

import mwparserfromhell

from ..obkat.analyzer import analyze_text
from ..obkat.formatter import parse_headers, find_nominations_with_itog, strike_header
from ..obkat.report import CHECKS, NUANCES, plain_title, wiki_url, issue_detail, fix_guidance
from . import NAVIGATION


PROTECTED = re.compile(r'<!--.*?-->|<(nowiki|pre|source|syntaxhighlight)\b[^>]*>.*?</\1\s*>', re.I | re.S)
NAV = r'\{\{\s*(?:Шаблон\s*:\s*)?ОБПРО-Навигация\s*(?:\|[^{}]*)?\}\}'
WRAPPED_NAV = re.compile(r'<noinclude>\s*' + NAV + r'\s*</noinclude>', re.I)
BARE_NAV = re.compile(NAV, re.I)


def visible_text(text):
    # Keep offsets and line numbers; examples and comments are never nominations.
    return PROTECTED.sub(lambda m: re.sub(r'[^\n]', ' ', m[0]), text)


def subjects(title):
    clean = re.sub(r'<[^>]*>', '', title)
    outside_links = re.sub(r'\[\[[^\]]*\]\]', lambda m: ' ' * len(m[0]), clean)
    arrows = list(re.finditer(r'→|⇒|->', outside_links))
    end = arrows[0].start() if len(arrows) == 1 else len(clean)
    links = re.findall(r'\[\[:?([^\]|]+)(?:\|[^\]]*)?\]\]', clean[:end])
    return {re.sub(r'\s+', ' ', item.replace('_', ' ')).strip().casefold()
            for item in links or [plain_title(clean[:end])]}


def analyze(text):
    return analyze_text(visible_text(text), nomination_level=2, subjects=subjects)


def nominations(text):
    return [h for h in parse_headers(visible_text(text).split('\n'))
            if h['level'] == 2 and not (h['is_date'] or h['is_service'] or h['is_any_itog'])]


def declared_closed(text):
    headers = parse_headers(visible_text(text).split('\n'))
    leading = '\n'.join(text.split('\n')[:headers[0]['line_idx']]) if headers else text
    matches = list(BARE_NAV.finditer(visible_text(leading)))
    if len(matches) != 1:
        return False
    template = mwparserfromhell.parse(leading[matches[0].start():matches[0].end()]).filter_templates()[0]
    return template.has('closed') and str(template.get('closed').value).strip() == '1'


def set_navigation(text, closed):
    headers = parse_headers(visible_text(text).split('\n'))
    first = headers[0]['line_idx'] if headers else len(text.split('\n'))
    lines = text.split('\n')
    leading = '\n'.join(lines[:first])
    masked = visible_text(leading)
    matches = list(BARE_NAV.finditer(masked))
    if len(matches) > 1:
        return text, 'Несколько навигационных шаблонов перед обсуждениями'
    if not matches:
        return '<noinclude>{{' + NAVIGATION + ('|closed=1' if closed else '') + '}}</noinclude>\n\n' + text, None
    match = matches[0]
    template = mwparserfromhell.parse(leading[match.start():match.end()]).filter_templates()[0]
    template.name = NAVIGATION
    while template.has('closed'):
        template.remove('closed')
    if closed:
        template.add('closed', '1')
    wrapped = next((m for m in WRAPPED_NAV.finditer(masked)
                    if m.start() <= match.start() and m.end() >= match.end()), None)
    start, end = (wrapped.start(), wrapped.end()) if wrapped else (match.start(), match.end())
    if not wrapped and re.search(r'</?noinclude\b', leading, re.I):
        return text, 'Навигация находится в неоднозначном блоке noinclude; проверьте его границы'
    canonical = '<noinclude>' + str(template) + '</noinclude>'
    return text[:start] + canonical + text[end:], None


def normalize_spacing(text):
    blocked = {i['line'] - 1 for i in analyze(text) if i['type'] in {'unclosed_tag', 'mismatched_level'}}
    header_lines = {h['line_idx'] for h in parse_headers(visible_text(text).split('\n'))} - blocked
    protected_lines = set()
    for match in PROTECTED.finditer(text):
        protected_lines.update(range(text.count('\n', 0, match.start()), text.count('\n', 0, match.end()) + 1))
    result, previous = [], None
    for number, line in enumerate(text.split('\n')):
        if number in protected_lines:
            result.append(line)
            previous = 'text' if line.strip() else 'empty'
        elif not line.strip():
            if previous not in {'header', 'nav', 'empty'}:
                result.append('')
                previous = 'empty'
        elif number in header_lines:
            if previous in {'nav', 'text'}:
                result.append('')
            result.append(re.sub(r'^(=+)\s*(.+?)\s*(=+)\s*$', r'\1 \2 \3', line.strip()))
            previous = 'header'
        elif WRAPPED_NAV.fullmatch(line.strip()):
            result.append(line)
            previous = 'nav'
        else:
            if previous == 'nav':
                result.append('')
            result.append(line)
            previous = 'text'
    while result and not result[-1].strip():
        result.pop()
    # MediaWiki strips a final newline when saving. Match the stored text so a
    # formatting-only pass does not repeatedly propose an empty edit.
    return '\n'.join(result)


def format_discussion(text, spacing=False):
    lines = text.split('\n')
    headers = parse_headers(visible_text(text).split('\n'))
    blocked = {i['line'] - 1 for i in analyze(text) if i['type'] in {'unclosed_tag', 'mismatched_level'}}
    for index in find_nominations_with_itog(headers):
        header = headers[index]
        if header['level'] >= 2 and header['line_idx'] not in blocked:
            lines[header['line_idx']] = strike_header(lines[header['line_idx']])
    candidate = '\n'.join(lines)
    primary = nominations(candidate)
    closed = bool(primary) and all(h['struck'] for h in primary)
    # An existing archive marker is an editorial decision. Unresolved headings
    # are reported for review, rather than silently reopening a completed year.
    candidate, error = set_navigation(candidate, closed or declared_closed(text))
    if spacing:
        candidate = normalize_spacing(candidate)
    return candidate, closed, error


def edit_comment(before, after):
    return 'Зачёркивание завершённых обсуждений, форматирование.'


def page_report(base, checked_at, *, spacing=False, excluded=False):
    candidate, closed, navigation_error = format_discussion(base.text, spacing)
    seen, anchors = Counter(), {}
    for header in parse_headers(visible_text(base.text).split('\n')):
        heading = plain_title(header['title'])
        seen[heading] += 1
        anchors[header['line_idx'] + 1] = heading + (f'_{seen[heading]}' if seen[heading] > 1 else '')
    findings = analyze(base.text)
    archived = declared_closed(base.text)
    for issue in findings:
        if archived and (issue['type'] == 'no_itog' or
                         issue['type'] == 'sub_itog_no_main' and issue.get('main_struck')):
            issue['type'] = 'archive_open'
    if navigation_error:
        findings.append(dict(type='navigation', line=1, title=NAVIGATION, reason=navigation_error))
    items = []
    for issue in findings:
        heading = issue.get('title', '')
        guidance = issue.get('reason') or fix_guidance(issue)
        label = CHECKS.get(issue['type'], 'Оформление навигации')
        if issue['type'] == 'archive_open':
            label = 'Незачёркнутая номинация в закрытом году'
            guidance = 'Год отмечен closed=1, но у номинации нет отдельного окончательного «Итога». Проверьте решение в обсуждении и оформите итог; при возобновлении обсуждения снимите closed=1.'
        if issue['type'] == 'service_wrong_level':
            guidance = 'Вложите служебный раздел в номинацию: заголовок третьего уровня или глубже.'
        items.append(dict(issue, page=base.title, month=base.title.rsplit('/', 1)[-1],
            label=label,
            section='nuances' if issue['type'] in NUANCES else 'problems',
            display_title=plain_title(heading), detail=issue.get('reason') or issue_detail(issue),
            fix=guidance,
            url=wiki_url(base.title, '' if issue['type'] == 'navigation' else anchors.get(issue['line'], heading)),
            revision=base.revision, checked_at=checked_at, origin='wiki'))
    return dict(title=base.title, revision=base.revision, checked_at=checked_at,
                items=items, nominations=len(nominations(base.text)), closed=closed,
                pending=candidate != base.text and not excluded, excluded=excluded)
