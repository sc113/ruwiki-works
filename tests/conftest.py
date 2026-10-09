import time
from datetime import datetime

import pytest

from toolforge_app.config import Settings
from toolforge_app.processors.obkat.report import TABLE_TITLE, page_title
from toolforge_app.storage import Store
from toolforge_app.wiki import Revision, WikiError

OPEN = "{{ОБК-Навигация}}\n== 5 октября 2026 ==\n=== Категория:Пример ===\nОбсуждение.\n"
CLOSED = OPEN + "==== Итог ====\nРешено.\n"


@pytest.fixture
def settings(tmp_path):
    settings = Settings(database_url="sqlite:///" + (tmp_path / "test.sqlite3").as_posix(),
        secret_key="test-secret", admin_username="admin", oauth_key="test-key", oauth_secret="test-secret")
    settings.start_month = datetime.now(settings.zone).strftime("%Y-%m")
    return settings


@pytest.fixture
def store(settings):
    result = Store(settings.database_url)
    yield result
    result.engine.dispose()


class FakeWiki:
    def __init__(self, settings):
        self.title = page_title(settings.start_month)
        self.data = {
            self.title: Revision(self.title, 10, time.time() - 3600, CLOSED),
            TABLE_TITLE: Revision(TABLE_TITLE, 20, time.time() - 3600, "Old index"),
        }
        self.edits = []
        self.conflict = False
        self.table_error = False
        self.poll_error = False

    def fetch(self, title):
        if title == TABLE_TITLE and self.table_error:
            raise WikiError("network")
        return self.data.get(title, Revision(title, missing=True))

    def revisions(self, titles, content=False):
        if self.poll_error:
            raise WikiError("network")
        return [self.fetch(title) for title in titles]

    def edit(self, base, text, summary):
        if self.conflict:
            raise WikiError("editconflict")
        assert self.data[base.title].revision == base.revision
        self.edits.append((base, text, summary))
        revision = base.revision + 1
        self.data[base.title] = Revision(base.title, revision, time.time(), text)
        return revision


@pytest.fixture
def wiki(settings):
    return FakeWiki(settings)
