"""Read-only check of active category template definitions; never queues edits."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolforge_app.config import Settings
from toolforge_app.storage import Store
from toolforge_app.wiki import WikiClient, WikiError
from toolforge_app.processors.maintenance.inventory import Catalogue

settings = Settings.from_env()
settings.wiki_write = False
store = Store(settings.database_url)
wiki = WikiClient(settings)
catalogue = Catalogue(wiki, store)
inventory = store.get_state('maintenance-dates:inventory', {})
for category in inventory.get('categories', []):
    if category['count']:
        names = [d['name'] for d in catalogue.category_templates(category['title'])]
        print(category['title'] + ': ' + ', '.join(names))
rq = catalogue.aliases('Rq')
print('RQ canonical: ' + rq['name'] + '; aliases: ' + str(len(rq['aliases'])))
if inventory.get('articles'):
    title = next(iter(inventory['articles']))
    base = wiki.fetch(title)
    try:
        records = wiki.history_index(base.title, base.revision, limit=5000)
        first_text = wiki.historical_text(base.title, records[0]['id'])
        print('History API sample: ' + str(len(records)) + ' revisions; base matches latest; first text length ' + str(len(first_text)))
    except WikiError as exc:
        print('History sample unavailable: ' + exc.code)
print('Read-only definitions check complete; no processing runs or edits.')
