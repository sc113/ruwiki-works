"""Small Action API client with conflict detection and explicit write gating."""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
import mwparserfromhell

from .processors.obkat.report import TABLE_TITLE, title_month


class WikiError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# These failures affect the session or service, rather than one article.
# Stop the pass instead of issuing the same failing request for every title.
RUN_ERRORS = frozenset({
    "network", "edit-outcome-unknown", "maxlag", "ratelimited", "readonly",
    "credentials-missing", "credentials-unreadable", "writes-disabled", "login-failed", "unexpected-bot-identity",
    "assertuserfailed", "assertnameduserfailed", "assertbotfailed", "badtoken",
    "notloggedin", "blocked", "autoblocked", "permissiondenied", "writeapidenied",
})

IN_USE_TEMPLATES = frozenset({'редактирую', 'пишу', 'l', 'wip', 'in use'})


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Revision:
    title: str
    revision: int = 0
    edited_at: float = 0
    text: str = ""
    missing: bool = False
    user: str = ""


class WikiClient:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.base_settings = settings
        self.store = store
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent
        self.logged_in = False
        self.last_edit_at = 0.0
        self.request_guard = lambda: None
        self.api_backoff = {}

    def _wait_until(self, deadline):
        # Renew the execution lease and notice pause/stop during API waits.
        while deadline > time.time():
            self.request_guard()
            time.sleep(min(5, max(0, deadline - time.time())))
        self.request_guard()

    def _backoff(self):
        return self.store.get_state("wiki:api_backoff", {}) if self.store else self.api_backoff

    def _respect_backoff(self):
        while True:
            backoff = self._backoff()
            # A long server-requested pause must not occupy the executor indefinitely.
            if backoff.get("until", 0) - time.time() > 60:
                raise WikiError(backoff["code"])
            self._wait_until(backoff.get("until", 0))
            if self._backoff().get('until', 0) <= time.time():
                return

    def _defer(self, code, attempt, response=None):
        delay = 5 * 2 ** attempt
        if response is not None:
            value = response.headers.get("Retry-After", "")
            try:
                retry_after = float(value)
            except ValueError:
                try:
                    retry_after = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    retry_after = 0
            delay = max(delay, retry_after)
        backoff = dict(until=time.time() + delay, code=code)
        if self.store:
            self.store.defer_api(code, backoff['until'])
        else:
            if self.api_backoff.get('until', 0) < backoff['until']:
                self.api_backoff = backoff
        if attempt == 2 or delay > 60:
            raise WikiError(code)

    def _throttle_edit(self):
        last = self.store.get_state("wiki:last_edit_attempt", 0) if self.store else self.last_edit_at
        if last:
            self._wait_until(last + self.settings.edit_interval)
        self.request_guard()
        self.last_edit_at = time.time()
        if self.store:
            self.store.set_state("wiki:last_edit_attempt", self.last_edit_at)

    def request(self, params, post=False):
        params = {"format": "json", "formatversion": 2, "maxlag": 5, **params}
        editing = params.get("action") == "edit"
        for attempt in range(3):
            self.request_guard()
            self._respect_backoff()
            if editing:
                self._throttle_edit()
                self._respect_backoff()
            try:
                response = (self.session.post(self.settings.api_url, data=params, timeout=40)
                            if post else self.session.get(self.settings.api_url, params=params, timeout=40))
                if response.status_code == 429:
                    self._defer("ratelimited", attempt, response)
                    continue
                if response.status_code in {502, 503, 504}:
                    # A gateway error can hide an already committed edit. The
                    # next job must fetch a fresh revision before trying again.
                    self._defer("edit-outcome-unknown" if editing else "network",
                                2 if editing else attempt, response)
                    continue
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError):
                self._defer("edit-outcome-unknown" if editing else "network", 2 if editing else attempt)
                continue
            if "error" in data:
                code = data["error"].get("code", "api")
                if code in {"maxlag", "ratelimited", "readonly"}:
                    self._defer(code, attempt, response)
                    continue
                if code in {"assertuserfailed", "assertnameduserfailed", "assertbotfailed", "notloggedin", "badtoken"}:
                    self.logged_in = False
                raise WikiError(code)
            return data
        raise WikiError("network")

    def revisions(self, titles, content=False):
        result = []
        for offset in range(0, len(titles), 20):
            data = self.request({"action": "query", "prop": "revisions", "titles": "|".join(titles[offset:offset + 20]),
                "rvprop": "ids|timestamp|user" + ("|content" if content else ""), "rvslots": "main"}, post=True)
            for page in data["query"]["pages"]:
                if page.get("missing") or page.get("invalid"):
                    result.append(Revision(page["title"], missing=True))
                    continue
                if "redirect" in page:
                    # No redirect resolution: a monthly discussion can never write to a target outside the scope.
                    raise WikiError("redirect")
                if not page.get("revisions"):
                    raise WikiError("hidden-revision")
                revision = page["revisions"][0]
                if content and "content" not in revision.get("slots", {}).get("main", {}):
                    raise WikiError("hidden-content")
                result.append(Revision(page["title"], revision["revid"], epoch(revision["timestamp"]),
                    revision.get("slots", {}).get("main", {}).get("content", ""), user=revision.get("user", "")))
        return result

    def fetch(self, title):
        return self.revisions([title], content=True)[0]

    def changes(self, start, end):
        params = {"action": "query", "list": "recentchanges", "rcnamespace": 4,
            "rcprop": "title|ids|timestamp|user", "rctype": "edit|new",
            "rcdir": "newer", "rcstart": timestamp(start), "rcend": timestamp(end), "rclimit": 500}
        while True:
            data = self.request(params)
            yield from data["query"]["recentchanges"]
            if "continue" not in data:
                break
            params.update(data["continue"])

    def category_members(self, title, *, kind="page", namespace=0, start_prefix=""):
        params = {"action": "query", "list": "categorymembers", "cmtitle": title,
                  "cmtype": kind, "cmnamespace": namespace, "cmlimit": 500, "cmprop": "ids|title"}
        if start_prefix:
            params.update(cmsort="sortkey", cmstartsortkeyprefix=start_prefix)
        while True:
            data = self.request(params)
            yield from data["query"]["categorymembers"]
            if "continue" not in data:
                break
            params.update(data["continue"])

    def template_aliases(self, name):
        title = "Шаблон:" + name
        params = {"action": "query", "titles": title, "redirects": 1, "prop": "redirects",
                  "rdnamespace": 10, "rdlimit": 500}
        aliases, canonical = {name}, None
        while True:
            data = self.request(params, post=True)
            for page in data["query"]["pages"]:
                if page.get("missing") or page.get("invalid"):
                    raise WikiError("template-missing")
                canonical = page["title"].split(":", 1)[1]
                aliases.add(canonical)
                aliases.update(r["title"].split(":", 1)[1] for r in page.get("redirects", []))
            if "continue" not in data:
                break
            params.update(data["continue"])
        return {"name": canonical, "aliases": sorted(aliases)}

    def template_articles(self, name):
        """Namespace-0 transclusions; callers scan the template and all its aliases."""
        params = {'action': 'query', 'list': 'embeddedin', 'eititle': 'Шаблон:' + name,
                  'einamespace': 0, 'eifilterredir': 'nonredirects', 'eilimit': 500}
        while True:
            data = self.request(params)
            yield from data['query']['embeddedin']
            if 'continue' not in data:
                break
            params.update(data['continue'])

    def history_index(self, title, revision, limit=0):
        params = {"action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp",
                  "rvdir": "newer", "rvendid": revision, "rvlimit": 500}
        result = []
        while True:
            data = self.request(params, post=True)
            for page in data["query"]["pages"]:
                for entry in page.get("revisions", []):
                    if "timestamp" not in entry:
                        raise WikiError("hidden-history")
                    result.append({"id": entry["revid"], "timestamp": entry["timestamp"]})
                    if limit and len(result) > limit:
                        raise WikiError("revision-limit")
            if "continue" not in data:
                break
            params.update(data["continue"])
        if not result or result[-1]["id"] != revision:
            raise WikiError("incomplete-history")
        return result

    def historical_text(self, title, revision):
        data = self.request({"action": "query", "prop": "revisions", "revids": revision,
                             "rvprop": "ids|timestamp|content", "rvslots": "main"}, post=True)
        for page in data["query"]["pages"]:
            if page.get("title") != title:
                raise WikiError("history-title-mismatch")
            for entry in page.get("revisions", []):
                if entry["revid"] == revision:
                    content = entry.get("slots", {}).get("main", {}).get("content")
                    if content is None:
                        raise WikiError("hidden-history")
                    return content
        raise WikiError("incomplete-history")

    def creation_comment(self, title, revision):
        data = self.request({"action": "query", "prop": "revisions", "titles": title,
            "rvprop": "ids|timestamp|comment", "rvdir": "newer", "rvlimit": 1, "rvendid": revision}, post=True)
        for page in data["query"]["pages"]:
            entries = page.get("revisions", [])
            if not entries or "comment" not in entries[0]:
                raise WikiError("hidden-creation-comment")
            return entries[0]
        raise WikiError("incomplete-history")

    def wikipedia_languages(self):
        data = self.request({"action": "query", "meta": "siteinfo", "siprop": "interwikimap"})
        result = set()
        for item in data["query"]["interwikimap"]:
            host = urlsplit(item.get("url", "")).hostname or ""
            if host.endswith(".wikipedia.org") and item.get("language") and item["prefix"] != "ru":
                result.add(item["prefix"].lower())
        if not result:
            raise WikiError("language-map-missing")
        return result

    def login(self):
        from .runtime import writes_enabled
        if not writes_enabled(self.base_settings, self.store):
            raise WikiError("writes-disabled")
        if self.store:
            from .connections import effective_settings
            current = effective_settings(self.base_settings, self.store, names=('bot',))
            if (current.bot_login, current.bot_password) != (self.settings.bot_login, self.settings.bot_password):
                self.session.cookies.clear()
                self.logged_in = False
            self.settings = current
        if not self.settings.bot_login or not self.settings.bot_password or not self.settings.bot_username:
            raise WikiError("credentials-missing")
        if self.logged_in:
            return
        token = self.request({"action": "query", "meta": "tokens", "type": "login"})["query"]["tokens"]["logintoken"]
        data = self.request({"action": "login", "lgname": self.settings.bot_login,
            "lgpassword": self.settings.bot_password, "lgtoken": token}, post=True)
        if data.get("login", {}).get("result") != "Success":
            raise WikiError("login-failed")
        identity = self.request({"action": "query", "meta": "userinfo"})["query"]["userinfo"]
        if identity.get("name") != self.settings.bot_username or identity.get("anon"):
            raise WikiError("unexpected-bot-identity")
        self.logged_in = True

    def edit(self, base, text, summary):
        month = title_month(base.title)
        if base.title != TABLE_TITLE and (not month or month < self.settings.start_month):
            raise WikiError("outside-scope")
        return self._save(base, text, summary, minor=False)

    def edit_article(self, base, text, summary, allowed_titles):
        # The caller supplies the namespace-0 category snapshot for this run.
        if base.title not in allowed_titles:
            raise WikiError("outside-scope")
        return self._save(base, text, summary, minor=True)

    def _save(self, base, text, summary, *, minor):
        require_bot_permission(base.text, self.settings.bot_username)
        self.login()
        token = self.request({"action": "query", "meta": "tokens"})["query"]["tokens"]["csrftoken"]
        data = self.request({"action": "edit", "title": base.title, "text": text,
            "summary": summary, "token": token, "baserevid": base.revision,
            "basetimestamp": timestamp(base.edited_at), "starttimestamp": timestamp(time.time()),
            "nocreate": 1, "assert": "user", "assertuser": self.settings.bot_username, "bot": 1,
            "minor" if minor else "notminor": 1, "watchlist": "nochange"}, post=True)
        edit = data.get("edit", {})
        if edit.get("result") != "Success":
            raise WikiError("captcha" if "captcha" in edit else "edit-failed")
        return edit.get("newrevid", base.revision)


def bot_may_edit(text, username):
    """Preserve the bot exclusion protocol enforced by the old Pywikibot uploader."""
    return not bot_edit_block(text, username)


def require_bot_permission(text, username):
    reason = bot_edit_block(text, username)
    if reason:
        raise WikiError(reason)


def bot_edit_block(text, username):
    username = username.casefold().replace("_", " ")
    for template in mwparserfromhell.parse(text).filter_templates():
        name = str(template.name).strip().replace("_", " ").casefold()
        if ':' in name and name.split(':', 1)[0].strip() in {'шаблон', 'template'}:
            name = name.split(':', 1)[1].strip()
        if name in IN_USE_TEMPLATES:
            return 'page-in-use'
        if name in {"nobots", "нет ботов"}:
            return 'bot-excluded'
        if name not in {"bots", "боты"}:
            continue
        params = {str(p.name).strip().casefold(): str(p.value).strip().casefold() for p in template.params}
        allow = {p.strip().replace("_", " ") for p in params.get("allow", "all").split(",")}
        deny = {p.strip().replace("_", " ") for p in params.get("deny", "none").split(",")}
        if "all" in deny or username in deny or ("all" not in allow and username not in allow):
            return 'bot-excluded'
    return ''
