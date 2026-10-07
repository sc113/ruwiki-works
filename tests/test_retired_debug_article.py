"""Old single-article settings cannot silently restrict recurring service work."""
import pytest

from toolforge_app.processors.daily import TASK_SLUGS, fields, form_value, get_config
from toolforge_app.processors import get_task
from toolforge_app.web import create_app
from test_web import set_session


@pytest.mark.parametrize('slug', TASK_SLUGS)
def test_stored_debug_article_is_ignored_and_cannot_be_enabled_on_site(settings, store, slug):
    original = get_config(store, slug)
    store.set_state(slug + ':config', dict(original, debug_article='Только эта статья'))
    assert get_config(store, slug) == original

    client = create_app(settings, store).test_client()
    set_session(client, 'admin')
    html = client.get('/processors/' + get_task(slug).processor + '?task=' + slug).get_data(as_text=True)
    assert 'name="debug_article"' not in html and 'Отладочная статья' not in html

    # Extra submitted fields cannot bring back the removed restriction.
    data = {key: str(form_value(original, key, kind)) for key, _, kind, _ in fields(slug) if kind != 'bool'}
    data.update({key: 'on' for key, _, kind, _ in fields(slug) if kind == 'bool' and original[key]})
    data.update(csrf='csrf-test', debug_article='Другая статья')
    response = client.post('/admin/tasks/' + slug + '/settings', data=data)
    assert response.status_code == 302
    assert get_config(store, slug) == original
