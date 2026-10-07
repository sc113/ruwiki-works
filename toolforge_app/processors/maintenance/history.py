"""Revision-based dates with lazy content loading and section-aware matching."""
import re
from collections import OrderedDict
from datetime import datetime
from difflib import SequenceMatcher

import mwparserfromhell

from ...wiki import WikiError


def normalize(name):
    name = re.sub(r"^(?:Шаблон|Template):", "", str(name).strip(), flags=re.I)
    name = " ".join(name.replace("_", " ").split())
    return name[:1].casefold() + name[1:]


def section_match(left, right):
    if left == right:
        return True
    def clean(value):
        value = mwparserfromhell.parse(value).strip_code().casefold()
        return " ".join(re.findall(r"\w+", value))
    left, right = clean(left), clean(right)
    return bool(left and right) and SequenceMatcher(None, left, right).ratio() >= .7


def template_occurrences(code):
    """Return live parser nodes; headings, comments and nowiki are not edited."""
    result = []
    for section in code.get_sections(flat=True, include_lead=True, include_headings=True):
        headings = section.filter_headings(recursive=False)
        heading = str(headings[0].title).strip() if headings else ""
        heading_templates = {id(t) for h in headings for t in h.title.filter_templates()}
        result.extend((template, heading) for template in section.filter_templates()
                      if id(template) not in heading_templates)
    return result


class History:
    def __init__(self, wiki, base, config, event):
        self.wiki, self.base, self.config, self.event = wiki, base, config, event
        self.index = None
        self.cache = OrderedDict()

    def records(self):
        if self.index is None:
            self.index = self.wiki.history_index(self.base.title, self.base.revision, self.config["max_revisions"])
            self.event("history", f"Ревизий в истории: {len(self.index)}", search_mode=self.config["search_mode"])
        return self.index

    def snapshot(self, position):
        if position not in self.cache:
            entry = self.records()[position]
            text = self.base.text if entry["id"] == self.base.revision else self.wiki.historical_text(self.base.title, entry["id"])
            self.cache[position] = template_occurrences(mwparserfromhell.parse(text))
            if len(self.cache) > 8:
                self.cache.popitem(last=False)
        self.cache.move_to_end(position)
        return self.cache[position]

    def find(self, aliases, section=None, *, rq_aliases=None, rq_params=None, require_current=True):
        names = {normalize(name) for name in aliases}
        rq_names = {normalize(name) for name in rq_aliases or []}
        params = set(rq_params or [])
        memo = {}
        def present(position):
            if position not in memo:
                matches = []
                for template, heading in self.snapshot(position):
                    if section is not None and not section_match(section, heading):
                        continue
                    name = normalize(template.name)
                    if name in names:
                        matches.append((str(template.name).strip(), heading))
                    elif name in rq_names:
                        for param in template.params:
                            value = re.sub(r"<!--.*?-->", "", str(param.value), flags=re.S).strip().casefold()
                            if str(param.name).strip().isdigit() and value in params:
                                matches.append((value, heading))
                                break  # Synonyms in one RQ are one occurrence.
                if section is not None and len({heading for _, heading in matches}) > 1:
                    raise WikiError("ambiguous-section-history")
                if section is not None and len(matches) > 1:
                    raise WikiError("ambiguous-template-occurrence")
                memo[position] = matches[0] if matches else None
                if self.config["debug_output"]:
                    self.event("history_probe", "Проверена историческая версия", revision=self.records()[position]["id"],
                               found=bool(matches), section=section)
            return memo[position]

        records = self.records()
        end = len(records) - 1
        if require_current and not present(end):
            return None
        mode, found = self.config["search_mode"], None
        if mode == 1:
            while end >= 0 and not present(end):
                end -= 1
            while end >= 0 and present(end):
                found = end
                end -= 1
        elif mode == 2:
            found = next((i for i in range(len(records)) if present(i)), None)
        else:
            # A binary candidate is verified against earlier revisions: templates
            # can disappear and return, so a monotonic predicate cannot be assumed.
            low, high = 0, len(records) - 1
            while low <= high:
                middle = (low + high) // 2
                if present(middle):
                    found, high = middle, middle - 1
                else:
                    low = middle + 1
            bound = found + 1 if found is not None else len(records)
            found = next((i for i in range(bound) if present(i)), None)
        if found is None:
            return None
        variant, heading = present(found)
        result = {"date": datetime.fromisoformat(records[found]["timestamp"].replace("Z", "+00:00")).date().isoformat(),
                  "revision": records[found]["id"], "variant": variant, "section": heading}
        self.event("date_found", f"Дата установки: {result['date']}", **result)
        return result
