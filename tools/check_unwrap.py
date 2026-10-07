"""Read-only verification on up to five articles in the unwrap category."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolforge_app.config import Settings
from toolforge_app.storage import Store
from toolforge_app.wiki import WikiClient, Revision
from toolforge_app.processors.maintenance.config import get_config
from toolforge_app.processors.maintenance.inventory import Catalogue
from toolforge_app.processors.maintenance.unwrap import unwrap_rq

settings = Settings.from_env()
settings.wiki_write = False
store = Store(settings.database_url)
slug = 'maintenance-rq-unwrap'
inventory = store.get_state(slug + ':inventory')
if not inventory:
    raise SystemExit('Refresh category counts first: python -m toolforge_app refresh-maintenance')
wiki = WikiClient(settings)
definition = Catalogue(wiki, store).aliases('Rq')
config = get_config(store, slug)
eligible = wrappers = checked = 0
for title in list(inventory['articles'])[:5]:
    base = wiki.fetch(title)
    text, changes, notes = unwrap_rq(base, definition, config, lambda *a, **kw: None)
    repeated = unwrap_rq(Revision(base.title, base.revision, base.edited_at, text), definition,
                         config, lambda *a, **kw: None)
    assert repeated[0] == text and not repeated[1], 'Non-idempotent unwrap'
    checked += 1
    eligible += bool(changes)
    wrappers += len(changes)
print(f'Read-only sample: {checked} articles, {eligible} eligible, {wrappers} RQ wrappers. No runs queued or wiki edits.')
