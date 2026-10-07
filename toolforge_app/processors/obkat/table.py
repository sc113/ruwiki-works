"""Summary table of open category discussion nominations."""


import re


from collections import defaultdict


ENABLE_DATE_LINKS = True


def is_header(line):
    return bool(re.match(r'^=+\s*.+\s*=+\s*$', line.strip()))


def get_header_level(line):
    match = re.match(r'^(=+)', line.strip())
    return len(match.group(1)) if match else 0


def get_header_title(line):
    match = re.match(r'^=+\s*(.+?)\s*=+\s*$', line.strip())
    return match.group(1) if match else ''


def is_struck(title):
    return bool(re.search(r'<s>.*</s>', title, re.I))


def is_date_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip()
    months = r'(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)'
    return bool(re.search(rf'^\d+\s+{months}', clean, re.I))


def is_final_itog(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return clean == 'итог'


def is_any_itog_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    return bool(re.search(r'\bитог\b|\bнеитог\b|\bпредытог\b', clean))


def is_service_header(title):
    clean = re.sub(r'<[^>]+>', '', title).strip().lower()
    patterns = [
        r'^по (всем|обеим|обоим)',
        r'^к итогу',
        r'^технические',
        r'^миниопрос',
        r'^предлагается',
        r'^комментарий',
        r'^вопрос',
        r'^резюме',
    ]
    return any(re.search(p, clean) for p in patterns)


def extract_open_nominations(content):
    """Извлекает открытые номинации из вики-текста."""
    lines = content.split('\n')

    # Парсим заголовки
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

    # Собираем открытые номинации по датам
    nominations_by_date = defaultdict(list)
    current_date = None

    for idx, h in enumerate(headers):
        if h['level'] == 2 and h['is_date']:
            current_date = h['title']
            continue

        # Номинация уровня 3 (не дата, не служебная, не итог)
        if h['level'] == 3 and not h['is_date'] and not h['is_service'] and not h['is_any_itog']:
            # Проверяем есть ли итог
            has_itog = False
            for j in range(idx + 1, len(headers)):
                next_h = headers[j]
                if next_h['level'] <= 3:
                    break
                if next_h['level'] == 4 and next_h['is_itog'] and not next_h['struck']:
                    has_itog = True
                    break

            # Если нет итога и не зачёркнута — открытая номинация
            if not has_itog and not h['struck'] and current_date:
                nominations_by_date[current_date].append(h['title'])

    return nominations_by_date


MONTHS_RU = {
    '01': 'Январь', '02': 'Февраль', '03': 'Март', '04': 'Апрель',
    '05': 'Май', '06': 'Июнь', '07': 'Июль', '08': 'Август',
    '09': 'Сентябрь', '10': 'Октябрь', '11': 'Ноябрь', '12': 'Декабрь'
}


def year_month_to_page_name(year_month):
    """Преобразует 2024-07 в 'Июль 2024'."""
    year, month = year_month.split('-')
    return f'{MONTHS_RU[month]} {year}'


def format_date_for_wiki(date_title, year_month):
    """Форматирует дату для вики-разметки."""
    clean_date = re.sub(r'<[^>]+>', '', date_title).strip()

    if ENABLE_DATE_LINKS:
        page_name = year_month_to_page_name(year_month)
        return f"'''[[Википедия:Обсуждение категорий/{page_name}#{clean_date}|{clean_date}]]'''"
    else:
        return clean_date


def generate_wiki_table(pages):
    """Генерирует таблицу в формате вики."""
    # Собираем все номинации по месяцам
    all_nominations = defaultdict(lambda: defaultdict(list))

    for year_month, content in sorted(pages.items()):
        nominations = extract_open_nominations(content)
        for date, noms in nominations.items():
            all_nominations[year_month][date].extend(noms)

    # Генерируем вики-разметку
    output = []
    output.append('{{Википедия:Обсуждение категорий/Обсуждаемые категории}}')

    # Сортируем месяцы по убыванию
    sorted_months = sorted(all_nominations.keys(), reverse=True)

    for month in sorted_months:
        dates = all_nominations[month]
        if not dates:
            continue

        output.append(f'{{{{Википедия:Обсуждение категорий/Месяц|{month}|')

        # Сортируем даты по убыванию (по дню)
        def get_day(date_str):
            match = re.search(r'^(\d+)', date_str)
            return int(match.group(1)) if match else 0

        sorted_dates = sorted(dates.keys(), key=get_day, reverse=True)

        for date in sorted_dates:
            noms = dates[date]
            if not noms:
                continue

            formatted_date = format_date_for_wiki(date, month)
            output.append(f'* {formatted_date}')

            for nom in noms:
                # Убираем теги <s> если есть
                clean_nom = re.sub(r'</?s>', '', nom)
                output.append(f'** {clean_nom}')

        output.append('}}')
        output.append('')

    return '\n'.join(output)
