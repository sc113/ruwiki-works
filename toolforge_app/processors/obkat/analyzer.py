"""Structural checks for category discussion nominations."""


import re


from collections import defaultdict


SERVICE_PATTERNS = [
    r'^по (всем|обеим|обоим)',
    r'^к итогу',
    r'^технические',
    r'^миниопрос',
    r'^предлагается',
    r'^комментарий',
    r'^вопрос',
    r'^резюме',
]


def is_final_itog(title):
    """Проверяет, является ли заголовок финальным итогом (только точное 'Итог')"""
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return clean == 'итог'


def is_any_itog_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return bool(re.search(r'\bитог\b|\bнеитог\b|\bпредытог\b', clean))


def is_date_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip()
    months = r'(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)'
    return bool(re.search(rf'^\d+\s+{months}', clean, re.I))


def parse_date(title):
    """Извлекает день из заголовка даты"""
    clean = re.sub(r'<[^>]+>', '', title).strip()
    match = re.search(r'^(\d+)', clean)
    return int(match.group(1)) if match else 0


def is_group_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return bool(re.search(r'^по (всем|обеим|обоим)', clean))


def is_service_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return any(re.search(p, clean) for p in SERVICE_PATTERNS)


def nomination_categories(title):
    """Category subjects, excluding the destination of a simple rename."""
    category_pattern = re.compile(r'\[\[:Категория:([^\]|]+)(?:\|[^\]]*)?\]\]')
    # Arrows inside a category name or a link label do not mark a rename.
    outside_links = re.sub(r'\[\[[^\]]*\]\]', lambda match: ' ' * len(match[0]), title)
    arrows = list(re.finditer(r'→|⇒|->|&rarr;|&#(?:8594|x2192);', outside_links, re.I))
    # Keep complex headings conservative when their grouping is ambiguous.
    end = arrows[0].start() if len(arrows) == 1 else len(title)
    return {match[1].strip().lower() for match in category_pattern.finditer(title) if match.start() < end}


def analyze_text(content):
    lines = content.split('\n')

    header_pattern = re.compile(r'^(=+)\s*(.+?)\s*=+\s*$')
    issues = []
    all_headers = []

    for i, line in enumerate(lines, 1):
        match = header_pattern.match(line.strip())
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        if level < 2:
            continue
        is_struck = bool(re.search(r'<s>.*</s>', title, re.I))
        all_headers.append((level, i, title, is_struck))

    # ПРОВЕРКА: Незакрытые теги <s> в заголовках
    for i, line in enumerate(lines, 1):
        # Проверяем только строки с заголовками
        if re.match(r'^=+\s*.+\s*=+\s*$', line.strip()):
            s_open = len(re.findall(r'<s>', line, re.I))
            s_close = len(re.findall(r'</s>', line, re.I))
            if s_open != s_close:
                issues.append({
                    'line': i,
                    'type': 'unclosed_tag',
                    'title': line.strip(),
                    'open': s_open,
                    'close': s_close
                })

    # ПРОВЕРКА: Разные уровни тегов в начале и конце заголовка (=== текст ====)
    for i, line in enumerate(lines, 1):
        match = re.match(r'^(=+)\s*.+?\s*(=+)\s*$', line.strip())
        if match:
            start_level = len(match.group(1))
            end_level = len(match.group(2))
            if start_level != end_level:
                issues.append({
                    'line': i,
                    'type': 'mismatched_level',
                    'title': line.strip(),
                    'start': start_level,
                    'end': end_level
                })

    # ПРОВЕРКА: Пропуск уровней заголовков
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if idx > 0:
            prev_level = all_headers[idx - 1][0]
            if level > prev_level + 1:
                issues.append({
                    'line': line_num,
                    'type': 'level_skip',
                    'level': level,
                    'prev_level': prev_level,
                    'title': title
                })

    # ПРОВЕРКА: Неправильный порядок дат (должны идти по убыванию)
    dates = [(i, h) for i, h in enumerate(all_headers)
             if h[0] == 2 and is_date_header(h[2])]
    for i in range(len(dates) - 1):
        curr_day = parse_date(dates[i][1][2])
        next_day = parse_date(dates[i + 1][1][2])
        if next_day > curr_day:
            issues.append({
                'line': dates[i + 1][1][1],
                'type': 'date_order',
                'title': dates[i + 1][1][2],
                'prev_day': curr_day,
                'curr_day': next_day
            })

    # ПРОВЕРКА: Дублирующиеся итоги подряд (не зачёркнутые)
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if is_final_itog(title) and not is_struck and idx > 0:
            prev_level, prev_line, prev_title, prev_struck = all_headers[idx - 1]
            if prev_level == level and is_final_itog(prev_title) and not prev_struck:
                issues.append({
                    'line': line_num,
                    'type': 'duplicate_itog',
                    'title': title,
                    'prev_line': prev_line
                })

    # ПРОВЕРКА: Пустые номинации (заголовок без текста до следующего заголовка)
    # Не считаем пустыми, если после идёт подзаголовок (групповая номинация)
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level == 3 and not is_date_header(title) and not is_service_header(title) and not is_any_itog_header(title):
            if idx + 1 < len(all_headers):
                next_level, next_line, _, _ = all_headers[idx + 1]
                if next_level > level:
                    continue  # Групповая номинация с подзаголовками
                text_between = ''.join(lines[line_num:next_line - 1]).strip()
                if len(text_between) < 10:
                    issues.append({
                        'line': line_num,
                        'type': 'empty_nomination',
                        'title': title
                    })

    # ПРОВЕРКА: Номинации без итога (уровень 3, не зачёркнуты, нет итога ниже)
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level == 3 and not is_struck and not is_date_header(title) and not is_service_header(title) and not is_any_itog_header(title):
            # Ищем итог на уровне 4 до следующей номинации/даты
            has_itog = False
            for j in range(idx + 1, len(all_headers)):
                next_level, _, next_title, _ = all_headers[j]
                if next_level <= 3:
                    break
                if next_level == 4 and is_final_itog(next_title):
                    has_itog = True
                    break
            if not has_itog:
                issues.append({
                    'line': line_num,
                    'type': 'no_itog',
                    'title': title
                })

    # ПРОВЕРКА: Подитоги без основного итога
    # Проблема: есть подноминации без итогов, но есть подитоги для других подноминаций
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level == 3 and not is_date_header(title) and not is_service_header(title) and not is_any_itog_header(title):
            # Это номинация уровня 3, собираем подноминации и их итоги
            sub_nominations = []  # подноминации уровня 4
            sub_itogs = []  # итоги уровня 5
            has_main_itog = False

            for j in range(idx + 1, len(all_headers)):
                next_level, next_line, next_title, next_struck = all_headers[j]
                if next_level <= 3:
                    break
                if next_level == 4:
                    if is_final_itog(next_title) and not next_struck:
                        has_main_itog = True
                    elif not is_service_header(next_title) and not is_any_itog_header(next_title):
                        sub_nominations.append(
                            (next_line, next_title, next_struck))
                elif next_level == 5 and is_final_itog(next_title) and not next_struck:
                    sub_itogs.append(next_line)

            # Проблема: есть подитоги уровня 5, но нет основного итога уровня 4
            # Это может быть в двух случаях:
            # 1. Главная номинация НЕ зачёркнута, есть открытые подноминации
            # 2. Главная номинация зачёркнута, но есть незачёркнутые подноминации (противоречие)
            if sub_nominations and sub_itogs and not has_main_itog:
                open_subs = [s for s in sub_nominations if not s[2]]
                # Случай 1: номинация открыта, есть открытые подноминации
                if not is_struck and open_subs:
                    issues.append({
                        'line': line_num,
                        'type': 'sub_itog_no_main',
                        'title': title,
                        'total_subs': len(sub_nominations),
                        'open_subs': len(open_subs),
                        'with_itog': len(sub_itogs)
                    })
                # Случай 2: номинация зачёркнута, но есть незачёркнутые подноминации
                elif is_struck and open_subs:
                    issues.append({
                        'line': line_num,
                        'type': 'sub_itog_no_main',
                        'title': title,
                        'total_subs': len(sub_nominations),
                        'open_subs': len(open_subs),
                        'with_itog': len(sub_itogs),
                        'main_struck': True  # флаг что главная номинация зачёркнута
                    })

    # ПРОВЕРКА: Дублирующиеся номинации (одна категория обсуждается несколько раз)
    nominations_by_cat = defaultdict(list)
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level == 3 and not is_date_header(title) and not is_service_header(title) and not is_any_itog_header(title):
            cats = nomination_categories(title)
            for cat in cats:
                nominations_by_cat[cat].append(
                    (line_num, title, is_struck))

    for cat, noms in nominations_by_cat.items():
        # Если категория упоминается более одного раза в РАЗНЫХ незачёркнутых номинациях
        open_noms = [n for n in noms if not n[2]]
        # Убираем дубликаты по номеру строки
        unique_open_noms = list({n[0]: n for n in open_noms}.values())
        if len(unique_open_noms) > 1:
            for line_num, title, _ in unique_open_noms[1:]:
                issues.append({
                    'line': line_num,
                    'type': 'duplicate_nomination',
                    'title': title,
                    'first_line': unique_open_noms[0][0]
                })

    # ПРОВЕРКА: Категория упоминается несколько раз в заголовках за один день
    # Собираем номинации по датам
    current_date_line = None
    nominations_by_date = defaultdict(list)
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level == 2 and is_date_header(title):
            current_date_line = line_num
        elif level == 3 and current_date_line and not is_service_header(title) and not is_any_itog_header(title):
            cats = nomination_categories(title)
            for cat in cats:
                nominations_by_date[(current_date_line, cat)].append(
                    (line_num, title, is_struck))

    for (date_line, cat), noms in nominations_by_date.items():
        # Если категория упоминается более одного раза за один день
        if len(noms) > 1:
            # Проверяем, что это разные строки
            unique_lines = set(n[0] for n in noms)
            if len(unique_lines) > 1:
                for line_num, title, _ in noms[1:]:
                    issues.append({
                        'line': line_num,
                        'type': 'same_day_duplicate',
                        'title': title,
                        'first_line': noms[0][0],
                        'date_line': date_line
                    })

    # ПРОВЕРКА: "По всем" / "По обоим" относится только к одной подноминации
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if level >= 4 and is_group_header(title):
            # Это "По всем" на уровне 4+, считаем сколько подноминаций перед ним на том же уровне
            count_same_level = 0
            for j in range(idx - 1, -1, -1):
                prev_level, prev_line, prev_title, _ = all_headers[j]
                if prev_level < level:
                    break  # Вышли на уровень выше
                if prev_level == level:
                    if not is_service_header(prev_title) and not is_any_itog_header(prev_title):
                        count_same_level += 1
            if count_same_level == 1:
                issues.append({
                    'line': line_num,
                    'type': 'group_for_one',
                    'title': title,
                    'count': count_same_level
                })
            elif count_same_level == 0:
                # "По всем" вложен в номинацию уровнем выше
                # Ищем непосредственного родителя на уровне level-1
                parent_title = None
                parent_idx = None
                for j in range(idx - 1, -1, -1):
                    prev_level, prev_line, prev_title, _ = all_headers[j]
                    if prev_level == level - 1:
                        if not is_date_header(prev_title) and not is_service_header(prev_title) and not is_any_itog_header(prev_title):
                            parent_title = prev_title
                            parent_idx = j
                        break
                    if prev_level < level - 1:
                        break

                # Считаем сколько номинаций на уровне level-1 между датой и этим "По всем"
                if parent_title:
                    siblings_count = 0
                    for j in range(idx - 1, -1, -1):
                        prev_level, prev_line, prev_title, _ = all_headers[j]
                        if prev_level < level - 1:
                            break
                        if prev_level == level - 1:
                            if is_date_header(prev_title):
                                break
                            if not is_service_header(prev_title) and not is_any_itog_header(prev_title):
                                siblings_count += 1

                    # Если "По всем" вложен в одну из нескольких номинаций — это ошибка
                    # (должен быть на том же уровне что и номинации, а не вложен в одну из них)
                    # siblings_count > 1 означает что есть несколько номинаций, но "По всем" вложен только в одну
                    if siblings_count > 1:
                        issues.append({
                            'line': line_num,
                            'type': 'group_for_one',
                            'title': title,
                            'count': siblings_count,
                            'parent': parent_title
                        })
                    elif siblings_count == 1:
                        # "По всем" вложен в единственную номинацию — это тоже ошибка
                        # (нет смысла в "По всем" для одной номинации)
                        issues.append({
                            'line': line_num,
                            'type': 'group_for_one',
                            'title': title,
                            'count': 1,
                            'parent': parent_title
                        })

    # ПРОВЕРКА: Подноминации не зачёркнуты при закрытой номинации
    # Логика как в format_wiki.py
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        # Пропускаем даты, служебные, итоги
        if is_date_header(title) or is_service_header(title) or is_any_itog_header(title):
            continue

        # Ищем итог на уровне level+1 до следующего заголовка того же или меньшего уровня
        # Итог на более глубоком уровне закрывает только подноминацию, а не всю номинацию
        has_direct_itog = False

        for j in range(idx + 1, len(all_headers)):
            next_level, next_line, next_title, next_struck = all_headers[j]
            if next_level <= level:
                break
            if next_level == level + 1 and is_final_itog(next_title) and not next_struck:
                has_direct_itog = True
                break

        # Также проверяем случай когда итог под "По всем" (неправильный уровень, но всё равно закрывает)
        has_itog_under_group = False
        if not has_direct_itog:
            for j in range(idx + 1, len(all_headers)):
                next_level, next_line, next_title, next_struck = all_headers[j]
                if next_level <= level:
                    break
                # Ищем "По всем" на уровне level+1, а под ним итог на level+2
                if next_level == level + 1 and is_group_header(next_title):
                    for k in range(j + 1, len(all_headers)):
                        kh_level, kh_line, kh_title, kh_struck = all_headers[k]
                        if kh_level <= level + 1:
                            break
                        if kh_level == level + 2 and is_final_itog(kh_title) and not kh_struck:
                            has_itog_under_group = True
                            break
                    break

        if has_direct_itog or has_itog_under_group:
            # Номинация закрыта — проверяем что все подразделы зачёркнуты
            # НО не проверяем: Итог, служебные, вариации итогов, подразделы Итога
            inside_itog_section = False

            for j in range(idx + 1, len(all_headers)):
                next_level, next_line, next_title, next_struck = all_headers[j]
                if next_level <= level:
                    break

                # Если встретили "Итог" — начинаем секцию итога (её подразделы не проверяем)
                if is_final_itog(next_title) and next_level == level + 1:
                    inside_itog_section = True
                    continue

                # Если мы внутри секции итога и встретили заголовок того же или меньшего уровня — выходим
                if inside_itog_section and next_level <= level + 1:
                    inside_itog_section = False

                # Не проверяем подразделы Итога
                if inside_itog_section:
                    continue

                # Не проверяем: итоги, служебные, вариации итогов
                if is_final_itog(next_title) or is_service_header(next_title) or is_any_itog_header(next_title):
                    continue

                # Если подноминация не зачёркнута — ошибка
                if not next_struck:
                    issues.append({
                        'line': next_line,
                        'type': 'not_struck',
                        'title': next_title,
                        'parent_line': line_num,
                        'parent_title': title
                    })

    # Основные проверки итогов
    for idx, (level, line_num, title, is_struck) in enumerate(all_headers):
        if is_date_header(title) and is_struck:
            issues.append(
                {'line': line_num, 'type': 'struck_date', 'title': title})
            continue

        if is_service_header(title) and not is_any_itog_header(title):
            if level < 4:
                issues.append(
                    {'line': line_num, 'type': 'service_wrong_level', 'level': level, 'title': title})
            continue

        if not is_final_itog(title):
            continue

        parent = None
        same_level_candidate = None
        service_parent = None

        itog_parent = None  # Родитель-итог (Предвар. итог, Неитог и т.п.)

        for j in range(idx - 1, -1, -1):
            prev_level, prev_line, prev_title, prev_struck = all_headers[j]
            if prev_level == level - 1:
                if is_service_header(prev_title):
                    # Групповой заголовок ("По всем") — итог не должен быть вложен в него
                    service_parent = all_headers[j]
                    break
                if is_any_itog_header(prev_title) and not is_final_itog(prev_title):
                    # "Итог" вложен в "Предвар. итог" / "Неитог" — это ошибка
                    itog_parent = all_headers[j]
                    break
                if is_date_header(prev_title):
                    continue
                parent = all_headers[j]
                break
            elif prev_level == level and same_level_candidate is None:
                if not is_any_itog_header(prev_title) and not is_service_header(prev_title) and not is_date_header(prev_title):
                    same_level_candidate = all_headers[j]
            elif prev_level < level - 1:
                break

        if same_level_candidate is not None and parent is None and itog_parent is None:
            has_valid_group = False
            for k in range(idx - 1, -1, -1):
                ck_level, _, ck_title, _ = all_headers[k]
                if ck_level == level and is_group_header(ck_title) and level >= 4:
                    has_valid_group = True
                    break
                if ck_level < level:
                    break
            if not has_valid_group:
                issues.append({
                    'line': line_num, 'type': 'wrong_itog_level', 'level': level,
                    'title': title, 'parent_line': same_level_candidate[1],
                    'parent_title': same_level_candidate[2]
                })
                continue

        # ПРОВЕРКА: Итог вложен в другой итог (Предвар. итог, Неитог и т.п.)
        if itog_parent is not None:
            issues.append({
                'line': line_num, 'type': 'itog_under_itog', 'level': level,
                'title': title, 'parent_line': itog_parent[1],
                'parent_title': itog_parent[2]
            })
            continue

        # ПРОВЕРКА: Итог слишком глубоко вложен (пропущен уровень)
        # Ищем ближайшую номинацию (не служебную, не дату, не итог) на любом уровне выше
        if parent is None:
            nearest_nomination = None
            for j in range(idx - 1, -1, -1):
                prev_level, prev_line, prev_title, prev_struck = all_headers[j]
                if prev_level >= level:
                    continue
                if is_date_header(prev_title) or is_service_header(prev_title) or is_any_itog_header(prev_title):
                    if prev_level == 2 and is_date_header(prev_title):
                        break  # Дошли до даты — дальше не ищем
                    continue
                nearest_nomination = all_headers[j]
                break

            # Если нашли номинацию, но она на уровне < level-1, значит итог слишком глубоко
            if nearest_nomination is not None:
                nom_level = nearest_nomination[0]
                if nom_level < level - 1:
                    issues.append({
                        'line': line_num, 'type': 'wrong_itog_level', 'level': level,
                        'title': title, 'parent_line': nearest_nomination[1],
                        'parent_title': nearest_nomination[2],
                        'expected_level': nom_level + 1
                    })
                    continue

        if parent is None:
            if service_parent is not None:
                # Если итог под "По всем" — это ошибка уровня (итог должен быть на том же уровне)
                if is_group_header(service_parent[2]):
                    issues.append({
                        'line': line_num, 'type': 'wrong_itog_level', 'level': level,
                        'title': title, 'parent_line': service_parent[1],
                        'parent_title': service_parent[2],
                        'expected_level': service_parent[0]
                    })
                    # Также проверяем зачёркивание родительской номинации (выше "По всем")
                    service_level = service_parent[0]
                    for k in range(idx - 1, -1, -1):
                        kh_level, kh_line, kh_title, kh_struck = all_headers[k]
                        if kh_level < service_level:
                            if not is_date_header(kh_title) and not is_service_header(kh_title) and not is_any_itog_header(kh_title):
                                if not kh_struck:
                                    issues.append({
                                        'line': line_num, 'type': 'not_struck', 'title': title,
                                        'parent_line': kh_line, 'parent_title': kh_title
                                    })
                            break
                else:
                    issues.append({
                        'line': line_num, 'type': 'itog_under_service', 'title': title,
                        'parent_line': service_parent[1], 'parent_title': service_parent[2]
                    })
                continue
            issues.append(
                {'line': line_num, 'type': 'orphan_itog', 'title': title})
            continue

        if is_date_header(parent[2]):
            issues.append({
                'line': line_num, 'type': 'itog_under_date', 'title': title,
                'parent_line': parent[1], 'parent_title': parent[2]
            })
            continue

        # Проверка зачёркивания родителя теперь делается в отдельном блоке выше
        # (ПРОВЕРКА: Подноминации не зачёркнуты при закрытой номинации)

    return issues
