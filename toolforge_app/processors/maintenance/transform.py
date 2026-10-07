"""Pure wikitext updates; history and redirects are supplied by the runner."""
import re
from collections import Counter

import mwparserfromhell
from mwparserfromhell.nodes import Template

from .history import normalize, template_occurrences


def clean_value(value):
    return re.sub(r"<!--.*?-->", "", str(value), flags=re.S).strip().casefold()


def alias_map(definitions):
    return {normalize(alias): item["name"] for item in definitions for alias in item["aliases"]}


def update_dates(base, definitions, config, history, event):
    # RQ containers belong to their separate conversion/unwrap tasks.
    definitions = [item for item in definitions if normalize(item["name"]) != normalize("Rq")]
    code = mwparserfromhell.parse(base.text)
    aliases = alias_map(definitions)
    occurrences = template_occurrences(code)
    counts = Counter(aliases[normalize(t.name)] for t, _ in occurrences if normalize(t.name) in aliases)
    sections = {normalize(name) for name in config["section_templates"]}
    changes, notes = [], []
    for template, heading in occurrences:
        name = aliases.get(normalize(template.name))
        if not name:
            continue
        date_params = [p for p in template.params if str(p.name).strip().casefold() in {"date", "дата"}]
        if any(str(p.value).strip() for p in date_params):
            event("has_date", "Дата уже указана", template=name, section=heading)
            continue
        definition = next(item for item in definitions if item["name"] == name)
        scoped = any(normalize(alias) in sections for alias in definition["aliases"]) or counts[name] > 1 or template.has("раздел")
        found = history.find(definition["aliases"], heading if scoped else None)
        if not found:
            notes.append("Не удалось определить дату установки «" + name + "»")
            event("date_missing", notes[-1], template=name, section=heading)
            continue
        previous = str(template.name).strip()
        # Preserve first-letter capitalization and all unrelated parameter nodes.
        template.name = name if previous[:1].isupper() else name[:1].lower() + name[1:]
        for param in list(template.params):
            if str(param.name).strip() == "1" and re.fullmatch(r"(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{1,2}-\d{1,2})", str(param.value).strip()):
                template.remove(param)
        template.add(str(date_params[0].name) if date_params else "дата", found["date"])
        changes.append(dict(template=name, previous=previous, date=found["date"], revision=found["revision"], section=heading))
    return str(code), changes, notes


def update_rq(base, definitions, rq_definition, config, history, event):
    code = mwparserfromhell.parse(base.text)
    rq_aliases = {normalize(name) for name in rq_definition["aliases"]}
    occurrences = [(t, s) for t, s in template_occurrences(code) if normalize(t.name) in rq_aliases]
    mapping = config["rq_param_templates"]
    skip = {value.casefold() for value in config["rq_skip_params"]}
    if any(clean_value(p.value) in skip for t, _ in occurrences for p in t.params if str(p.name).strip().isdigit()):
        note = "Статья пропущена: RQ содержит запрещённый параметр"
        event("rq_skipped", note)
        return base.text, [], [note]
    changes, notes = [], []
    targets = {item["name"]: item for item in definitions}
    lookup = alias_map(definitions)
    for template, heading in occurrences:
        known = [(p, clean_value(p.value)) for p in template.params if str(p.name).strip().isdigit() and clean_value(p.value) in mapping]
        if not known:
            continue
        prepared, comments, removable = {}, {}, []
        for param, value in known:
            target = lookup.get(normalize(mapping[value]), mapping[value])
            definition = targets[target]
            equivalent_params = [p for p, name in mapping.items() if lookup.get(normalize(name), name) == target]
            scoped = len(occurrences) > 1 or template.has("раздел") or any(
                normalize(alias) in {normalize(s) for s in config["section_templates"]}
                for alias in definition["aliases"])
            context = heading if scoped else None
            found = history.find([], context, rq_aliases=rq_definition["aliases"], rq_params=equivalent_params)
            standalone = history.find(definition["aliases"], context, require_current=False)
            if standalone and (not found or standalone["date"] < found["date"]):
                found = standalone
            if not found:
                note = "Не удалось определить дату параметра RQ «" + value + "»"
                notes.append(note)
                event("date_missing", note)
                continue
            if target not in prepared or (found["date"], found["revision"]) < (prepared[target]["date"], prepared[target]["revision"]):
                prepared[target] = dict(found, parameter=value)
            comments.setdefault(target, []).extend(re.findall(r"<!--.*?-->", str(param.value), flags=re.S))
            removable.append(param)
        if not removable:
            continue
        existing = {lookup.get(normalize(t.name)): t for p in template.params if str(p.name).strip().isdigit()
                    for t in p.value.filter_templates() if lookup.get(normalize(t.name))}
        inner, unknown, named = [], [], []
        for param in template.params:
            if any(param is item for item in removable):
                continue
            if not str(param.name).strip().isdigit():
                named.append(param)
            elif param.value.filter_templates():
                inner.append(str(param.value).strip())
            else:
                unknown.append(str(param.value))
        for target, found in sorted(prepared.items(), key=lambda item: (item[1]["date"], item[0])):
            suffix = "".join(comments.get(target, []))
            if target not in existing:
                inner.append("{{" + target + "|дата=" + found["date"] + "}}" + suffix)
            elif suffix:
                inner.append(suffix)
            existing_date = next((str(p.value).strip() for p in existing[target].params
                                  if str(p.name).strip().casefold() in {"дата", "date"}), None) if target in existing else None
            changes.append(dict(template=target, previous=str(template.name).strip(),
                                date=existing_date if target in existing else found["date"],
                                action="removed_parameter" if target in existing else "inserted_template",
                                revision=found["revision"], parameter=found["parameter"], section=heading))
        replacement = Template("Rq" if str(template.name).strip()[:1].isupper() else "rq")
        for param in named:
            # topic is an ordinary preserved parameter; there is no normalization
            # or transfer to talk pages in this task.
            replacement.add(param.name, param.value, showkey=param.showkey, preserve_spacing=False)
        replacement.add("1", "\n" + "\n".join(inner) + "\n", showkey=False, preserve_spacing=False)
        for index, value in enumerate(unknown, 2):
            replacement.add(str(index), value, showkey=True, preserve_spacing=False)
        # Removing the container is exclusively the separate unwrap task.
        code.replace(template, replacement)
        event("rq_converted", "Параметры RQ заменены", parameters=[value for _, value in known], preserved_unknown=len(unknown))
    return str(code), changes, notes
