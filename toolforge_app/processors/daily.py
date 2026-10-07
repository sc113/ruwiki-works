"""Providers for the shared daily-action pages and controls."""
from .maintenance import TASK_SLUGS as MAINTENANCE_TASKS
from .maintenance import config as maintenance_config
from .maintenance.inventory import report as maintenance_report
from .translations import TASK_SLUGS as TRANSLATION_TASKS
from .translations import config as translation_config
from .translations.inventory import report as translation_report
from .sections import TASK_SLUGS as SECTION_TASKS
from .sections import config as section_config
from .sections.inventory import report as section_report

TASK_SLUGS = (*MAINTENANCE_TASKS, *TRANSLATION_TASKS, *SECTION_TASKS)


def config_module(slug):
    if slug in MAINTENANCE_TASKS:
        return maintenance_config
    if slug in TRANSLATION_TASKS:
        return translation_config
    if slug in SECTION_TASKS:
        return section_config
    raise ValueError('Unknown daily action')


def get_config(store, slug):
    return config_module(slug).get_config(store, slug)


def fields(slug):
    return config_module(slug).fields(slug)


def parse_form(form, slug):
    return config_module(slug).parse_form(form, slug)


def form_value(config, key, kind):
    return maintenance_config.form_value(config, key, kind)


def report(store, slug, config=None):
    provider = maintenance_report if slug in MAINTENANCE_TASKS else section_report if slug in SECTION_TASKS else translation_report
    from ..issues import annotate_report
    return annotate_report(store, slug, provider(store, slug, config))
