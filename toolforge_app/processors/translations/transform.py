"""Extract unambiguous provenance and fill missing template parameters."""
import re
from urllib.parse import parse_qs, unquote, urlsplit

import mwparserfromhell

from ...wiki import WikiError
from ..maintenance.history import normalize


def original_title(value):
    value = ' '.join(value.strip().replace('_', ' ').split())
    if not value or len(value) > 255 or any(c in value for c in '|{}[]<>\x00\r\n'):
        raise WikiError('invalid-original')
    return value


def source(lang, title, languages):
    lang = lang.strip().lower()
    if lang == 'ru' or lang not in languages:
        raise WikiError('invalid-language')
    return lang, original_title(title)


def parse_creation_comment(comment, languages):
    found = set()
    # Link targets identify the original; a displayed label may be translated.
    for match in re.finditer(r'\[\[:?([a-z][a-z0-9-]*):([^\]|]+)(?:\|[^\]]*)?\]\]', comment, re.I):
        if match[1].lower() in languages:
            found.add(source(match[1], match[2].split('#', 1)[0], languages))
    for match in re.finditer(r'(?:https?:)?//([a-z][a-z0-9-]*)\.(?:m\.)?wikipedia\.org/[^\s\]|<>]+', comment, re.I):
        if match[1].lower() not in languages:
            continue
        url = urlsplit(match[0] if not match[0].startswith('//') else 'https:' + match[0])
        title = unquote(url.path[len('/wiki/'):]) if url.path.startswith('/wiki/') else parse_qs(url.query).get('title', [''])[0]
        if title:
            found.add(source(match[1], title, languages))
    if len(found) > 1:
        raise WikiError('ambiguous-source')
    if not found:
        raise WikiError('comment-source-missing' if comment else 'creation-comment-empty')
    return found.pop()


def parse_talk(text, aliases, languages):
    found, malformed = set(), False
    for template in mwparserfromhell.parse(text).filter_templates():
        if normalize(template.name) not in aliases:
            continue
        params = {str(p.name).strip(): str(p.value).strip() for p in template.params}
        # Duplicate parameters are not a trustworthy source.
        if len(params) != len(template.params) or not params.get('1') or not params.get('2'):
            malformed = True
            continue
        try:
            found.add(source(params['1'], params['2'], languages))
        except WikiError:
            malformed = True
    if malformed:
        raise WikiError('talk-malformed')
    if len(found) > 1:
        raise WikiError('ambiguous-source')
    if not found:
        raise WikiError('talk-template-missing')
    return found.pop()


def update_templates(text, definitions, lang, original):
    code = mwparserfromhell.parse(text)
    aliases = {normalize(alias): item['name'] for item in definitions for alias in item['aliases']}
    changes, notes = [], []
    matched = 0
    for template in code.filter_templates():
        canonical = aliases.get(normalize(template.name))
        if not canonical:
            continue
        matched += 1
        old = str(template)
        params = {str(p.name).strip(): p for p in template.params}
        if len(params) != len(template.params):
            notes.append({'template': canonical, 'code': 'duplicate-parameters'})
            continue
        conflict = any(str(params[key].value).strip() and (
            str(params[key].value).strip().lower() != lang if key == 'язык'
            else str(params[key].value).strip().replace('_', ' ') != original)
            for key in ('язык', 'оригинал', '2') if key in params)
        if conflict:
            notes.append({'template': canonical, 'code': 'source-conflict'})
            continue
        # Parameter 1 marks a section, so it is preserved as-is.
        before = next((p for p in template.params if str(p.name).strip() in {'дата', 'date'}), None)
        if 'оригинал' in params:
            if not str(params['оригинал'].value).strip():
                params['оригинал'].value = original
        else:
            template.add('оригинал', original, before=before, preserve_spacing=False)
        if '2' in params:
            template.remove(params['2'])
        if 'язык' in params:
            if not str(params['язык'].value).strip():
                params['язык'].value = lang
        else:
            template.add('язык', lang, before=template.get('оригинал'), preserve_spacing=False)
        if str(template) != old:
            if 'date' in params and 'дата' not in params:
                params['date'].name = 'дата'
            changes.append(dict(template=canonical, language=lang, original=original, action='translation_source',
                                before=old, after=str(template)))
    if not matched:
        raise WikiError('translation-template-missing')
    return str(code), changes, notes
