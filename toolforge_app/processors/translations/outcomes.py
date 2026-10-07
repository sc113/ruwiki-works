"""Translation skips are informational; conflicts and execution errors need attention."""
from .history import LEGACY_REASONS

LABELS = {
    'creation-comment-empty': 'Комментарий к созданию статьи пуст. Проверьте СО или укажите язык и оригинал вручную.',
    'comment-source-missing': 'В комментарии к созданию нет ссылки на оригинал. Данные могут быть на СО.',
    'hidden-creation-comment': 'Комментарий к первой правке скрыт. Проверьте источник перевода на СО.',
    'talk-missing': 'Страница обсуждения отсутствует. Укажите язык и оригинал в шаблоне статьи.',
    'talk-template-missing': 'На СО нет шаблона с данными перевода. Найдите источник и заполните язык и оригинал.',
    'talk-malformed': 'Проверьте шаблон на СО: нужны корректный код языка и название оригинала в параметрах 1 и 2.',
    'ambiguous-source': 'Найдено несколько разных оригиналов. Уточните источник для каждого шаблона вручную.',
    'source-conflict': 'Найденные данные отличаются от уже заполненных. Сверьте источник, язык и оригинал вручную.',
    'invalid-language': 'Код языка не соответствует иноязычному разделу Википедии. Проверьте источник.',
    'invalid-original': 'Название оригинала содержит неподдерживаемую разметку. Укажите точное название статьи.',
    'duplicate-parameters': 'В шаблоне повторяются параметры. Уберите дубликаты после проверки значений.',
    'translation-template-missing': 'Целевой шаблон не найден в статье. Проверьте состав отслеживающей категории.',
    'bot-excluded': 'Страница запрещает работу бота; заполните параметры вручную.',
    'page-in-use': 'Статья сейчас редактируется участником; проверка будет повторена в следующий запуск.',
    'article-missing': 'Статья отсутствует.',
    'redirect': 'Страница является перенаправлением.',
    'network': 'Не удалось получить данные Википедии.',
    'editconflict': 'Статья изменилась во время обработки; будет проверена в следующий ежедневный запуск.',
    'talk-changed': 'СО изменилась во время обработки; данные будут прочитаны заново в следующий запуск.',
}
SOURCE_NOTICES = {'creation-comment-empty', 'comment-source-missing', 'hidden-creation-comment', 'talk-missing',
                  'talk-template-missing', 'talk-malformed', 'ambiguous-source', 'invalid-language', 'invalid-original',
                  'translation-template-missing', 'bot-excluded'}

SKIP_LABELS = {
    'creation-comment-empty': 'Пустой комментарий к созданию статьи',
    'comment-source-missing': 'В комментарии к созданию нет ссылки на оригинал',
    'hidden-creation-comment': 'Комментарий к первой правке скрыт',
    'talk-missing': 'Страница обсуждения отсутствует',
    'talk-template-missing': 'На СО нет шаблона с данными перевода',
    'talk-malformed': 'На СО нет полных данных о языке и оригинале',
    'invalid-language': 'Не распознан код языка оригинала',
    'invalid-original': 'Не распознано название оригинала',
    'translation-template-missing': 'Целевой шаблон отсутствует',
    'bot-excluded': 'На странице запрещена работа бота',
}
LEGACY_SKIP_LABELS = {LEGACY_REASONS[status]: label for status, label in {
    'комментарий имеет другой формат': 'В комментарии к созданию нет распознанной ссылки на оригинал',
    'комментарий не найден': 'Комментарий к созданию не найден',
    'страница обсуждения не существует': 'Страница обсуждения отсутствует',
    'шаблон страницы обсуждения не найден': 'На СО нет шаблона с данными перевода',
    'шаблон на странице обсуждения не содержит необходимые параметры': 'На СО нет полных данных о языке и оригинале',
}.items()}
SKIP_REASON_TEXT = {LABELS[key]: value for key, value in SKIP_LABELS.items()} | LEGACY_SKIP_LABELS


def is_skipped(item):
    outcome, reason = item.get('outcome'), item.get('reason', '')
    if outcome == 'legacy' or outcome in SKIP_LABELS:
        return True
    if outcome == 'unchanged':
        return not reason
    # Compatibility with older result snapshots that contained only a reason.
    return not outcome and reason in SKIP_REASON_TEXT


def skip_reason(item):
    reason = item.get('reason', '')
    return SKIP_LABELS.get(item.get('outcome')) or SKIP_REASON_TEXT.get(reason) or reason or 'Шаблоны не изменены'
