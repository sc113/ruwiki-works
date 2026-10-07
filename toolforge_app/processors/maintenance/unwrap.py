"""Remove RQ wrappers without dating or converting the contained templates."""
import mwparserfromhell

from .history import normalize, template_occurrences


def unwrap_rq(base, rq_definition, config, event):
    code = mwparserfromhell.parse(base.text)
    aliases = {normalize(name) for name in rq_definition["aliases"]}
    occurrences = [(template, heading) for template, heading in template_occurrences(code)
                   if normalize(template.name) in aliases]
    changes, notes = [], []
    # Inner wrappers go first; parent parser nodes then contain their result.
    for template, heading in reversed(occurrences):
        original = str(template.name).strip()
        nonempty = [param for param in template.params if str(param.value).strip()]
        named = [param for param in template.params if not str(param.name).strip().isdigit()]
        numeric = [param for param in nonempty if str(param.name).strip().isdigit()]
        reason = None
        if named:
            reason = "Есть именованные параметры RQ: " + ", ".join(str(p.name).strip() for p in named)
        elif len(numeric) != 1:
            reason = "Нужен один заполненный позиционный параметр RQ; найдено: " + str(len(numeric))
        elif str(numeric[0].name).strip() != "1":
            reason = "Вложенный шаблон должен находиться в первом параметре RQ"
        if reason:
            notes.append(reason)
            event("unwrap_skipped", reason, template=original, section=heading)
            continue
        value = numeric[0].value
        # A helper inside a problem template is part of that template's value,
        # not another problem. Count only templates at the value's top level.
        nested = value.filter_templates(recursive=False)
        if len(nested) != 1:
            reason = "В первом параметре RQ найдено шаблонов: " + str(len(nested)) + "; нужен ровно один"
        elif normalize(nested[0].name) in aliases or str(nested[0].name).strip().startswith("#"):
            reason = "Внутри RQ нет единственного самостоятельного шаблона проблемы"
        if reason:
            notes.append(reason)
            event("unwrap_skipped", reason, template=original, section=heading)
            continue
        inner = nested[0]
        date = next((str(p.value).strip() for p in inner.params
                     if str(p.name).strip().casefold() in {"дата", "date"}), None)
        replacement = str(value).strip()
        if config["debug_output"]:
            event("unwrap_check", "Проверена обёртка RQ", before=str(template), after=replacement, section=heading)
        code.replace(template, replacement)
        changes.append(dict(template=str(inner.name).strip(), previous=original, date=date,
                            action="unwrapped", section=heading))
        event("rq_unwrapped", "Убрана обёртка RQ; вложенный шаблон сохранён", template=str(inner.name).strip(), section=heading)
    return str(code), changes, notes
