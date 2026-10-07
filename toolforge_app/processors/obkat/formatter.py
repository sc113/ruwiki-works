"""Formatting and completion markers for category discussions."""


import re


def is_header(line):
    """Проверяет, является ли строка заголовком"""
    return bool(re.match(r'^=+\s*.+\s*=+\s*$', line.strip()))


def get_header_level(line):
    """Возвращает уровень заголовка"""
    match = re.match(r'^(=+)', line.strip())
    return len(match.group(1)) if match else 0


def get_header_title(line):
    """Извлекает текст заголовка без тегов ="""
    match = re.match(r'^=+\s*(.+?)\s*=+\s*$', line.strip())
    return match.group(1) if match else ''


def is_struck(title):
    """Проверяет, зачёркнут ли заголовок"""
    return bool(re.search(r'<s>.*</s>', title, re.I))


def is_date_header(title):
    """Проверяет, является ли заголовок датой"""
    clean = re.sub(r'<[^>]+>', '', title).strip()
    months = r'(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)'
    return bool(re.search(rf'^\d+\s+{months}', clean, re.I))


def is_final_itog(title):
    """Проверяет, является ли заголовок финальным итогом (только точное 'Итог')"""
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return clean == 'итог'


def is_service_header(title):
    """Проверяет, является ли заголовок служебным (не зачёркивается)"""
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    patterns = [
        r'^по (всем|обеим|обоим)',
        r'^к итогу',
        r'^технические',
        r'^миниопрос',
        r'^предлагается',
        r'^предложение',
        r'^комментарий',
        r'^вопрос',
        r'^новый вопрос',
        r'^резюме',
        r'^попытка сдвинуть',
        r'^попытка сдвинуться',
        r'^посмотрим правила',
        r'^продолжение',
        r'^сводка вариантов',
        r'^вариации терминологии',
        r'^названия категорий',
    ]
    return any(re.search(p, clean) for p in patterns)


def is_any_itog_header(title):
    """Проверяет, содержит ли заголовок слово итог (любые вариации)"""
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    # Ловим: итог, к итогу, предварительный итог, не итог, неитог, предытог, оспоренный итог и т.д.
    return bool(re.search(r'\bитог\b|\bнеитог\b|\bпредытог\b', clean))


def strike_header(line):
    """Добавляет зачёркивание к заголовку"""
    match = re.match(r'^(=+)\s*(.+?)\s*(=+)\s*$', line.strip())
    if not match:
        return line
    prefix = match.group(1)
    title = match.group(2)
    suffix = match.group(3)
    # Не зачёркиваем если уже зачёркнуто
    if is_struck(title):
        return line
    return f"{prefix} <s>{title}</s> {suffix}"


def parse_headers(lines):
    """Парсит все заголовки из строк вики-текста."""
    headers = []
    for i, line in enumerate(lines):
        if is_header(line):
            level = get_header_level(line)
            title = get_header_title(line)
            headers.append({
                'line_idx': i,
                'level': level,
                'title': title,
                'struck': is_struck(title),
                'is_date': is_date_header(title),
                'is_itog': is_final_itog(title),
                'is_service': is_service_header(title),
                'is_any_itog': is_any_itog_header(title),
            })
    return headers


def find_nominations_with_itog(headers):
    """
    Находит номинации с итогом.
    Возвращает set индексов заголовков, которые нужно зачеркнуть.
    """
    to_strike = set()

    for idx, h in enumerate(headers):
        # Пропускаем даты, служебные, итоги
        if h['is_date'] or h['is_service'] or h['is_any_itog']:
            continue

        level = h['level']
        already_struck = h['struck']

        # Ищем итог на уровне level+1 до следующего заголовка того же или меньшего уровня
        has_direct_itog = False
        sub_nominations = []  # подноминации на уровне level+1
        sub_with_itog = []    # подноминации с итогом

        for j in range(idx + 1, len(headers)):
            next_h = headers[j]
            if next_h['level'] <= level:
                break

            if next_h['level'] == level + 1:
                if next_h['is_itog'] and not next_h['struck']:
                    has_direct_itog = True
                elif not next_h['is_service'] and not next_h['is_any_itog']:
                    # Это подноминация
                    sub_nominations.append(j)
                    # Проверяем есть ли у неё итог на уровне level+2
                    for k in range(j + 1, len(headers)):
                        sub_next = headers[k]
                        if sub_next['level'] <= level + 1:
                            break
                        if sub_next['level'] == level + 2 and sub_next['is_itog'] and not sub_next['struck']:
                            sub_with_itog.append(j)
                            break

        if has_direct_itog:
            # Номинация закрыта — зачёркиваем её (если ещё не) и подразделы
            # НО не зачёркиваем: Итог, служебные, вариации итогов, подразделы Итога
            if not already_struck:
                to_strike.add(idx)

            # Проходим по подразделам, отслеживая находимся ли мы внутри секции "Итог"
            inside_itog_section = False
            itog_section_level = None

            for j in range(idx + 1, len(headers)):
                next_h = headers[j]
                if next_h['level'] <= level:
                    break

                # Если встретили "Итог" — начинаем секцию итога
                if next_h['is_itog'] and next_h['level'] == level + 1:
                    inside_itog_section = True
                    itog_section_level = next_h['level']
                    continue

                # Если мы внутри секции итога и встретили заголовок того же или меньшего уровня — выходим
                if inside_itog_section and itog_section_level and next_h['level'] <= itog_section_level:
                    inside_itog_section = False
                    itog_section_level = None

                # Не зачёркиваем подразделы Итога
                if inside_itog_section:
                    continue

                # Не зачёркиваем: итоги, служебные, вариации итогов, уже зачёркнутые
                if next_h['is_itog'] or next_h['struck'] or next_h['is_service'] or next_h['is_any_itog']:
                    continue

                to_strike.add(j)

        elif sub_nominations and sub_with_itog:
            # Есть подноминации, часть с итогом — зачёркиваем только те что с итогом
            # Но не зачёркиваем основную номинацию
            for sub_idx in sub_with_itog:
                to_strike.add(sub_idx)

    return to_strike


def format_text(content, spacing=False):
    """Форматирует вики-текст; обычный режим сохраняет исходные отступы."""

    # Замены
    content = content.replace('{{ВПОК-Навигация}}', '{{ОБК-Навигация}}')
    content = re.sub(r'\|closed\b', '|закрыто', content)

    lines = content.split('\n')

    # Форматирование заголовков
    for i, line in enumerate(lines):
        if is_header(line):
            # К: → Категория: (регистронезависимо для "к:")
            line = re.sub(r'\[\[:К:', '[[:Категория:', line)
            line = re.sub(r'\[\[:к:', '[[:Категория:', line)
            # категория: → Категория: (с маленькой буквы)
            line = re.sub(r'\[\[:категория:',
                          '[[:Категория:', line, flags=re.I)

            # Добавляем пробелы вокруг текста заголовка только если включена опция
            if spacing:
                # ==Текст== → == Текст ==
                line = re.sub(r'^(=+)([^ =])', r'\1 \2', line)
                line = re.sub(r'([^ =])(=+)\s*$', r'\1 \2', line)

            lines[i] = line

    # Парсим заголовки
    headers = parse_headers(lines)

    # Находим что нужно зачеркнуть
    to_strike = find_nominations_with_itog(headers)

    # Применяем зачёркивание
    for idx in to_strike:
        h = headers[idx]
        line_idx = h['line_idx']
        # Не зачёркиваем даты
        if not h['is_date']:
            lines[line_idx] = strike_header(lines[line_idx])

    # Форматирование пустых строк (только если включена опция)
    if spacing:
        # Правила:
        # - После {{ОБК-Навигация}} ВСЕГДА пустая строка
        # - Перед заголовком пустая строка, если перед ним был текст
        # - Между заголовками пустой строки нет
        # - После заголовка текст идёт сразу (без пустой строки)
        # - Две пустых строки подряд не допускаются
        result = []
        prev_type = None  # 'header', 'empty', 'nav', 'text'

        for i, line in enumerate(lines):
            stripped = line.strip()
            is_empty = stripped == ''
            is_hdr = is_header(line)
            is_nav = stripped in ['{{ОБК-Навигация}}', '{{ВПОК-Навигация}}']

            if is_empty:
                # Пустая строка после навигации — пропускаем (добавим при следующем элементе)
                if prev_type == 'nav':
                    pass
                # После заголовка пустую строку пропускаем (текст идёт сразу)
                elif prev_type == 'header':
                    pass  # Не добавляем, prev_type остаётся 'header'
                # После текста — добавляем (если ещё не было пустой)
                elif prev_type != 'empty':
                    result.append('')
                    prev_type = 'empty'
                # Две пустых подряд — пропускаем (prev_type уже 'empty')
            elif is_hdr:
                # После навигации ВСЕГДА добавляем пустую строку
                if prev_type == 'nav':
                    result.append('')
                # Перед заголовком пустая строка нужна только если был текст
                elif prev_type == 'text':
                    result.append('')
                result.append(line)
                prev_type = 'header'
            elif is_nav:
                result.append(line)
                prev_type = 'nav'
            else:
                # Обычный текст
                # После навигации ВСЕГДА добавляем пустую строку
                if prev_type == 'nav':
                    result.append('')
                result.append(line)
                prev_type = 'text'

        # Убираем пустые строки в конце
        while result and result[-1].strip() == '':
            result.pop()

        return '\n'.join(result) + '\n'
    else:
        # Если форматирование отключено, возвращаем как есть
        return '\n'.join(lines)
