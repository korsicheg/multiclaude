"""Slack's Web API, for the slack add-in: one Slack app (the bot) in the owner's workspace. Each
person DMs it and gets their own assistant, as on Telegram. The Web API uses the bot token. What
people send reaches the VM through the relay (relay/src/slack.js, router/relay_entrance.py).
state/slack.json, readable by the owner only, holds {"bot_token": "xoxb-…", "signing_secret",
"client_id", "client_secret"}; the app's Basic Information page shows the last three.

A bot can't delete what a person wrote, so /clear deletes their own messages only once they let
it: a "Sign in with Slack" link (sign_in_link) asks them for chat:write, and Slack sends them
back to the relay, which passes the code on; sign_in trades it for their user token, kept in
state/slack-people.json (owner only).

People are matched by their Slack user id, "slack_id" in users.json. Messages are Markdown,
sent with chat.postMessage's markdown_text."""
import hashlib
import hmac
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from multiclaude import jsonfile, paths
from multiclaude.log import log
from multiclaude.router import relay_entrance
from multiclaude.telegram import split_text

ID_FIELD = "slack_id"  # in a person's users.json entry: their Slack user id, set by `assistant slack-link`
TOKENS = paths.STATE / "slack.json"
DMS = paths.STATE / "slack-dms.json"  # Slack user id -> the id of their DM with the bot
PEOPLE = paths.STATE / "slack-people.json"  # Slack user id -> their user token, once they signed in
API = "https://slack.com/api/"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
SIGN_IN_PATH = "/slack/oauth"  # on the relay
SIGN_IN_SCOPE = "chat:write"  # what chat.delete needs to delete a person's own messages
MARKDOWN_LIMIT = 12000  # chat.postMessage's markdown_text
TIMEOUT = 20
RETRIES_AFTER_LIMIT = 2  # a rate-limited call waits as long as Slack says, this often
HISTORY_PAGE = 200  # messages per page of conversations.history and conversations.replies


class SlackError(Exception):
    pass


def tokens():
    """What state/slack.json holds (see above), or {} when Slack isn't set up."""
    saved = jsonfile.read_json(TOKENS, {})
    return saved if isinstance(saved, dict) and saved.get("bot_token") else {}


def slack_id(person):
    """A person's Slack user id (a users.User or a router Session), "" when they have none."""
    return str(person.entry.get(ID_FIELD) or "")


def configured():
    """Whether Slack can reach the VM: the app's bot token, and the relay it sends everything to."""
    return bool(tokens() and relay_entrance.settings())


class SlackApi:
    def __init__(self, token, timeout=TIMEOUT):
        self.token = token
        self.timeout = timeout

    def call(self, method, **params):
        """A Web API method with a JSON body; its reply, or SlackError."""
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        return self.request(method, body, "application/json; charset=utf-8")

    def call_form(self, method, **params):
        """Methods that only take form fields, such as files.getUploadURLExternal."""
        body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        return self.request(method, body, "application/x-www-form-urlencoded")

    def request(self, method, body, content_type):
        headers = {"Content-Type": content_type}
        if self.token:  # oauth.v2.access takes the app's client id and secret instead
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(RETRIES_AFTER_LIMIT + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(API + method, body, headers),
                                            timeout=self.timeout) as response:
                    answer = json.loads(response.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES_AFTER_LIMIT:
                    time.sleep(int(e.headers.get("Retry-After", "1")))
                    continue
                raise SlackError(f"{method}: HTTP {e.code}") from e
            except (OSError, ValueError) as e:
                raise SlackError(f"{method}: {e}") from e
            if not answer.get("ok"):
                raise SlackError(f"{method}: {answer.get('error', 'failed')}")
            return answer
        raise SlackError(f"{method}: rate limited")

    def download(self, url, dest):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as out:
                while chunk := response.read(1 << 16):
                    out.write(chunk)
        except OSError as e:
            raise SlackError(f"download failed: {e}") from e

    def upload(self, channel, path, comment=None, thread=None):
        """A file posted in the channel (with comment as its message, Markdown not supported there)."""
        path = Path(path)
        data = path.read_bytes()
        slot = self.call_form("files.getUploadURLExternal", filename=path.name, length=len(data))
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        request = urllib.request.Request(slot["upload_url"], data, {"Content-Type": mime})
        try:
            with urllib.request.urlopen(request, timeout=300):
                pass
        except OSError as e:
            raise SlackError(f"uploading {path.name} failed: {e}") from e
        return self.call("files.completeUploadExternal", files=[{"id": slot["file_id"], "title": path.name}],
                         channel_id=channel, initial_comment=comment, thread_ts=thread)


def bot():
    found = tokens()
    if not found:
        raise SlackError(f"Slack isn't set up: no bot token in {TOKENS}")
    return SlackApi(found["bot_token"])


def dm_channel(user_id, api=None):
    """The id of the bot's DM with this Slack user, opened (and remembered) when needed."""
    known = jsonfile.read_json(DMS, {})
    known = known if isinstance(known, dict) else {}
    if user_id not in known:
        known[user_id] = (api or bot()).call("conversations.open", users=user_id)["channel"]["id"]
        jsonfile.write_json(DMS, known)
        os.chmod(DMS, 0o600)
    return known[user_id]


def post(channel, markdown, api=None, thread=None):
    """Markdown posted in the channel; the ts (message id) of its last piece."""
    api = api or bot()
    ts = None
    for piece in split_text(markdown, MARKDOWN_LIMIT):
        ts = api.call("chat.postMessage", channel=channel, markdown_text=piece, thread_ts=thread)["ts"]
    return ts


def sign_in_link(person):
    """The "Sign in with Slack" link that lets the bot delete this Slack user's own messages, or
    None while the app's client id and secret aren't saved, or there's no relay to come back to."""
    found, back = tokens(), sign_in_return()
    if not (found.get("client_id") and found.get("client_secret") and back):
        return None
    query = urllib.parse.urlencode({"client_id": found["client_id"], "user_scope": SIGN_IN_SCOPE,
                                    "redirect_uri": back, "state": sign_in_state(person, found)})
    return f"{AUTHORIZE}?{query}"


def sign_in(code, state):
    """Trade the code Slack gave someone coming back from sign_in_link for their user token, and
    keep it; their Slack user id. SlackError if it fails or the link wasn't made for them."""
    found = tokens()
    answer = SlackApi(None).call_form("oauth.v2.access", client_id=found.get("client_id"),
                                      client_secret=found.get("client_secret"), code=code,
                                      redirect_uri=sign_in_return())["authed_user"]
    person, token = answer["id"], answer["access_token"]
    if not hmac.compare_digest(state, sign_in_state(person, found)):
        SlackApi(token).call("auth.revoke")
        raise SlackError(f"a sign-in by {person} with a link made for someone else; their token is revoked")
    people = jsonfile.read_json(PEOPLE, {})
    jsonfile.write_json(PEOPLE, {**(people if isinstance(people, dict) else {}), person: token})
    os.chmod(PEOPLE, 0o600)
    return person


def sign_in_return():
    """Where Slack sends people after they sign in: the relay's page."""
    return relay_entrance.address(SIGN_IN_PATH)


def sign_in_state(person, found):
    """The state a sign-in link carries: only the VM, with the client secret, makes it for this person."""
    key = found["client_secret"].encode()
    return hmac.new(key, f"sign-in:{person}".encode(), hashlib.sha256).hexdigest()[:32]


def person_api(person):
    """The Web API as this Slack user, once they signed in; else None."""
    people = jsonfile.read_json(PEOPLE, {})
    token = people.get(person) if isinstance(people, dict) else None
    return SlackApi(token) if token else None


def wipe_dm(person, before, keep=()):
    """Delete what was said in the bot's DM with this Slack user before then (a Unix time), thread
    replies included, except the messages (ts) in keep: the bot's messages, and the person's own
    once they signed in (sign_in_link)."""
    try:
        api = bot()
        channel = dm_channel(person, api)
        messages = [m for m in dm_messages(api, channel, before) if m["ts"] not in keep]
    except SlackError as e:
        log(f"Slack: can't read the DM with {person} to empty it: {e}")
        return
    theirs = person_api(person)
    deletions = [(api if m.get("bot_id") else theirs, m["ts"]) for m in messages
                 if m.get("bot_id") or (theirs and m.get("user") == person)]
    delete_each(channel, deletions)


def delete_each(channel, deletions):
    """chat.delete for each (api, ts), as the one who wrote it; a failure is only logged."""
    failed = 0
    for api, ts in deletions:
        try:
            api.call("chat.delete", channel=channel, ts=ts)
        except SlackError as e:
            failed += 1
            log(f"Slack: {e}")
    log(f"Slack {channel}: deleted {len(deletions) - failed} of {len(deletions)} messages")


def dm_messages(api, channel, before):
    """Every message in the channel from before then, each followed by the replies in its thread."""
    latest = f"{before:.6f}"
    for message in history(api, "conversations.history", channel=channel, latest=latest):
        yield message
        if message.get("reply_count"):
            thread = history(api, "conversations.replies", channel=channel, ts=message["ts"], latest=latest)
            yield from (reply for reply in thread if reply["ts"] != message["ts"])


def history(api, method, **params):
    """Every message a paged history method returns, page after page."""
    cursor = None
    while True:
        page = api.call_form(method, limit=HISTORY_PAGE, cursor=cursor, **params)
        yield from page.get("messages", [])
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return


def with_code(text, copy):
    """text with each of copy it doesn't already hold added as code: Slack has no copy buttons,
    but its code is easy to copy."""
    return text + "".join(f"\n`{item}`" for item in copy if item not in text)


def question_blocks(question, options, choice=None):
    """A question with one button per option (action "ask:<n>"), or with the choice made."""
    blocks = [{"type": "markdown", "text": question}]
    if choice is not None:
        return blocks + [{"type": "markdown", "text": f"👉 {choice}"}]
    buttons = [{"type": "button", "action_id": f"ask:{n}", "value": str(n),
                "text": {"type": "plain_text", "text": option[:75], "emoji": True}} for n, option in enumerate(options)]
    return blocks + [{"type": "actions", "block_id": "ask", "elements": buttons}]


def asks_folder(channel):
    """Where `slack ask` keeps a channel's questions, next to tg's (paths.ASKS/<chat id>)."""
    return paths.ASKS / f"slack-{channel}"


def react(channel, ts, name):
    """A reaction on a message; a failure is only logged."""
    try:
        bot().call("reactions.add", channel=channel, timestamp=ts, name=name)
    except SlackError as e:
        log(f"Slack: reacting failed: {e}")
