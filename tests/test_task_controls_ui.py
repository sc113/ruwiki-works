from html.parser import HTMLParser
import pytest

from toolforge_app.web import create_app
from toolforge_app.processors import get_task
from test_web import set_session


class Controls(HTMLParser):
    def __init__(self, target):
        super().__init__()
        self.target, self.scope, self.launches, self.actions = target, None, [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'article' and attrs.get('aria-label') == self.target:
            self.scope = 'article'
        if tag == 'section' and 'processing-block' in attrs.get('class', '') and self.target == 'processing':
            self.scope = 'section'
        if not self.scope:
            return
        if tag == 'form' and attrs.get('action', '').endswith('/run'):
            self.launches.append(attrs['action'])
        if tag == 'input' and attrs.get('name') == 'action':
            self.actions.add(attrs['value'])

    def handle_endtag(self, tag):
        if tag == self.scope:
            self.scope = None


@pytest.mark.parametrize('slug,path', [('obkat', '/processors/obkat'),
    ('maintenance-rq', '/processors/maintenance?task=maintenance-rq'),
    ('translations-talk', '/processors/translations?task=translations-talk')])
def test_running_action_offers_pause_and_stop_without_another_launch(settings, store, slug, path):
    store.start_run('full', processor=slug)
    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    home = Controls(get_task(slug).title)
    home.feed(client.get('/').get_data(as_text=True))
    assert not home.launches and home.actions == {'pause', 'stop'}
    page = Controls('processing')
    page.feed(client.get(path).get_data(as_text=True))
    assert not page.launches and page.actions == {'pause', 'stop'}
