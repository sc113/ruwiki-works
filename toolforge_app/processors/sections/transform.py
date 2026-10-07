"""Inspect parsed sections without rendering templates or changing their parameters."""
import mwparserfromhell as mw
from mwparserfromhell import nodes


def normalize(name):
    value = ' '.join(str(name).replace('_', ' ').strip().casefold().split())
    for prefix in ('шаблон:', 'template:'):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


def is_content(node, ignored):
    if isinstance(node, (nodes.Comment, nodes.Heading)):
        return False
    if isinstance(node, nodes.Text):
        return bool(str(node).strip())
    if isinstance(node, nodes.Template):
        return normalize(node.name) not in ignored
    if isinstance(node, nodes.Tag) and str(node.tag).strip().casefold() == 'ref':
        return False
    if isinstance(node, nodes.Wikilink):
        title = str(node.title).strip().casefold()
        # Colon-prefixed file/category links are visible links, hence content.
        return not title.startswith(('категория:', 'category:', 'файл:', 'file:', 'изображение:', 'image:'))
    if isinstance(node, nodes.HTMLEntity):
        return bool(node.normalize().strip())
    return True


def switch_templates(text, slug, source, replacement, ignored_definitions):
    code = mw.parse(text)
    source_names = {normalize(name) for name in source['aliases']}
    replacement_names = {normalize(name) for name in replacement['aliases']}
    ignored = source_names | {normalize(name) for definition in ignored_definitions for name in definition['aliases']}
    # Each template belongs to the direct content of exactly one section.
    sections = [dict(title='Вводный раздел', level=0, parent=None, nodes=[])]
    stack = [0]
    for node in code.nodes:
        if isinstance(node, nodes.Heading):
            while len(stack) > 1 and sections[stack[-1]]['level'] >= node.level:
                stack.pop()
            sections.append(dict(title=str(node.title).strip(), level=node.level,
                                 parent=stack[-1] if len(stack) > 1 else None, nodes=[]))
            stack.append(len(sections) - 1)
        else:
            sections[stack[-1]]['nodes'].append(node)
    # Determine emptiness before any renames so one replacement cannot influence another.
    for section in sections:
        section['direct_content'] = any(is_content(node, ignored) for node in section['nodes'])
        section['child_content'] = False
    for section in reversed(sections):
        if section['parent'] is not None:
            sections[section['parent']]['child_content'] |= section['direct_content'] or section['child_content']
    changes, issues, diagnostics = [], [], []
    for section in sections:
        targets = [node for node in section['nodes'] if isinstance(node, nodes.Template) and normalize(node.name) in source_names]
        empty = not (section['direct_content'] or section['child_content'])
        diagnostics.append(dict(section=section['title'], empty=empty, direct_content=section['direct_content'],
                                child_content=section['child_content'], targets=len(targets)))
        if not targets:
            continue
        if any(isinstance(node, nodes.Template) and normalize(node.name) in replacement_names for node in section['nodes']):
            issues.append(dict(section=section['title'], code='conflicting-markers',
                               reason='В одном разделе стоят оба шаблона. Оставьте подходящий шаблон вручную.'))
            continue
        should_replace = not empty if slug == 'sections-empty-to-fill' else empty
        if not should_replace:
            continue
        reason = 'Без текстового содержимого' if empty else 'Есть текстовое содержимое' if section['direct_content'] else 'Есть непустые подразделы'
        for node in targets:
            old_name = str(node.name)
            name = replacement['name'] if old_name.strip()[0].isupper() else replacement['name'].lower()
            # Preserve outer whitespace, all parameters (including dates), and the rest of the page.
            node.name = old_name[:len(old_name) - len(old_name.lstrip())] + name + old_name[len(old_name.rstrip()):]
            changes.append(dict(action='section_switch', template=old_name.strip(), replacement=name,
                                section=section['title'], reason=reason))
    return str(code), changes, issues, diagnostics
