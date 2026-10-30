"""Center window: a normal top-level GTK window holding one WebView (web/center.html).

Split in two so everything but the widgets runs without a display:
- `CenterBridge` validates the page's messages, answers its queries from the store, builds the
  payloads and routes copy / link / action / dismiss / read / delete to the injected callbacks.
- `CenterWindow` owns the Gtk.Window and WebView (built lazily on the first show) and wires the
  bridge to them.

JS -> Python: script message handler `nc`. Every message is an object with a `type`; the page is
treated as untrusted (ids must be ints, strings are length-capped, links are allow-listed).
  queries (carry an int `req`; answered with window.nc.reply(req, obj)):
    {type: "list", req, q, offset, limit, app, state, tag, unread_only} -> {items, has_more}
    {type: "get", req, id}      -> {notification: {...} | null}
    {type: "facets", req}       -> {apps: [...], tags: [...]}
    {type: "action", req, id, key}  -> {ok}   (false: unavailable, sender gone)
    {type: "dismiss", req, id}      -> {ok}
    {type: "runs", req, id}         -> {runs: [...]}  (the run log, newest first; never `cmd`)
    {type: "center_state", req}     -> {group_by, days, groups, apps}  (the saved view state)
  commands (no reply):
    {type: "mark_read", ids} | {type: "mark_all_read"} | {type: "delete", ids} | {type: "clear"}
    {type: "copy", id, what: "body" | "summary"} | {type: "link", href}
    {type: "button", id, button}   (a rule button; checked against the stored buttons, then
                                    `on_button(id, button)`)
    {type: "set_expand", days, groups, apps}   (the expand/collapse overrides, saved whole;
                                    validated, a bad message is dropped; see `CenterState`)
    {type: "set_group_by", mode: "day" | "app"}     (the list grouping, saved)
    {type: "image_copy" | "image_copy_path" | "image_open", id}   (image previews; validated and
                                    done by `notifyd.image_ui.ImageService`, which answers with
                                    window.nc.imageResult({id, kind, ok}))
  settings requests (the Settings panel; all carry `req`; answered {ok, ...} or {ok: false, errors}
  where `errors` maps a field (variable, config key, "style") to a message, "" for a general one;
  every one answers {ok: false, unavailable: true, errors: {"": ...}} while `settings` is None):
    {type: "settings_get", req}                       -> {ok, settings: SettingsService.get()}
    {type: "settings_save_theme", req, values}        -> {ok, theme}
    {type: "settings_preset", req, name}              -> {ok, values}   (a preview, saves nothing)
    {type: "settings_save_style", req, text}          -> {ok}           (text <= 256 KiB)
    {type: "settings_save_config", req, section, values} -> {ok, restart_required}
    {type: "settings_preview_sound", req, spec, volume}  -> {ok, resolved, error}
    {type: "settings_send_test", req, kind}           -> {ok, id}   (normal | critical | buttons)
  rules requests (the Rules tab; same reply style and unavailable handling; `index`/`to`/
  `position` are ints 0..9999, `data` an object of at most 64 KiB of JSON; the service validates
  the whole rules file again and a bad edit answers {ok: false, errors: {rule field path: msg}}):
    {type: "rules_get", req}                          -> {ok, rules, error, editable, path}
    {type: "rules_update", req, index, data, expect?} -> {ok, ...rules_get}   (`expect`: the rule's
                                          name as listed; a mismatch refuses a stale edit; the
                                          same for rules_delete / rules_move / rules_set_enabled)
    {type: "rules_add", req, data, position?}         -> {ok, ...rules_get}
    {type: "rules_delete", req, index}                -> {ok, ...rules_get}
    {type: "rules_move", req, index, to}              -> {ok, ...rules_get}
    {type: "rules_set_enabled", req, index, on}       -> {ok, ...rules_get}
    {type: "rules_test", req, data, sample, index?}   -> {ok, matched, result: {rule, full, ...}}
                                          (sample {app, summary, body, urgency, category, hints},
                                          strings <= 4 KiB; a dry run, nothing is executed)
    {type: "rules_get_source", req}                   -> {ok, text, path}
    {type: "rules_save_source", req, text}            -> {ok, ...rules_get}   (text <= 256 KiB)
  reply actions (sender-defined; validated and routed by `notifyd.reply_ui.ReplyRouter` to the
  `on_reply_*` callbacks; the outcome returns as window.nc.replyResult):
    {type: "reply_choice", id, choice} | {type: "reply_text", id, text}
    {type: "reply_confirm", id, token} | {type: "reply_cancel", id, token}
Python -> JS: window.nc.reply / added(item) / refresh() / navigate(hash) / setUserCss(text)
  / imageResult({id, kind, ok})
  / replyResult({id, kind, status, token?, reason?})

The body only ever reaches the page as sanitized runs (`body_runs`); the page uses text nodes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal as _signal
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from notifyd import icons
from notifyd.image_ui import KINDS as IMAGE_KINDS
from notifyd.image_ui import ImageService, register_scheme
from notifyd.model import Notification, State, button_web
from notifyd.opentrace import PAGE_PHASES, OpenTrace, glib_schedule
from notifyd.reply_ui import KINDS as REPLY_KINDS
from notifyd.reply_ui import ReplyRouter, bridge_attr, reply_of
from notifyd.sanitize import body_runs, safe_href
from notifyd.toasts import file_uri, resolve_icon, teardown_webview

RESTART_BIN = str(Path(__file__).resolve().parent.parent / "bin" / "notifyd")

log = logging.getLogger("notifyd.center")

APP_ID = "net.corbinmeier.NotificationCenter"
TITLE = "Notifications"
DEFAULT_SIZE = (900, 640)
DEFAULT_PAGE = 50
MAX_PAGE = 200
MAX_IDS = 10000
MAX_TEXT = 500  # search text and filter values
PREVIEW_CHARS = 240
ID_MAX = 2**32 - 1
STATES = frozenset(s.value for s in State)
ICON_SIZE = icons.SIZE_CENTER
DETAIL_ICON_SIZE = icons.SIZE_CENTER_DETAIL

COPY_WHAT = ("body", "summary")
MAX_RUNS = 200
MAX_TAIL = 8192  # the runner keeps 4 KB; this only bounds a bad row
DEFAULT_STATE_PATH = Path("~/.local/state/net.corbinmeier.notifications/center.json").expanduser()
GROUP_MODES = ("day", "app")
MAX_STATE_DAYS = 500  # caps on the saved overrides; the page prunes to these before saving
MAX_STATE_APPS = 500
MAX_STATE_GROUPS = 2000  # (day, app) pairs in total
DAY_KEY = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_STYLE_BYTES = 256 * 1024  # style.css text from the Settings panel
MAX_SETTING_STR = 512  # any one value string
MAX_SETTING_KEYS = 200  # entries in one values dict
SETTINGS_KINDS = (
    "settings_get",
    "settings_save_theme",
    "settings_preset",
    "settings_save_style",
    "settings_save_config",
    "settings_preview_sound",
    "settings_send_test",
)
RULES_KINDS = (
    "rules_get",
    "rules_update",
    "rules_add",
    "rules_delete",
    "rules_move",
    "rules_set_enabled",
    "rules_test",
    "rules_get_source",
    "rules_save_source",
)
MAX_RULE_JSON = 64 * 1024  # one rule's data from the Rules tab
MAX_RULES_INDEX = 9999
MAX_SAMPLE_STR = 4096  # a sample notification field in rules_test
MAX_SAMPLE_HINTS = 50
MAX_RULES_BYTES = 256 * 1024  # rules.toml text from the raw editor
CONFIG_SECTIONS = ("toasts", "shade", "history", "sound", "windows", "images")
TEST_KINDS = ("normal", "critical", "buttons")
SETTING_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
TIMED_OUT_MARK = "[timed out after"  # appended to the output tail by the runner


# ---- validation ------------------------------------------------------------------------------


def valid_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= ID_MAX


def valid_ids(v: Any) -> list[int] | None:
    """A non-empty-or-empty list of valid ids, or None when anything is off."""
    if not isinstance(v, list) or len(v) > MAX_IDS or not all(valid_id(i) for i in v):
        return None
    return list(v)


def _opt_str(v: Any) -> str | None:
    """'' for missing/None, the string for a short string, None for anything else (invalid)."""
    if v is None:
        return ""
    if isinstance(v, str) and len(v) <= MAX_TEXT:
        return v
    return None


@dataclass(frozen=True)
class ListQuery:
    q: str = ""
    offset: int = 0
    limit: int = DEFAULT_PAGE
    app: str = ""
    state: str = ""
    tag: str = ""
    unread_only: bool = False


def parse_list_query(msg: dict) -> ListQuery | None:
    """Validate the filters of a `list` message. None: reject the message."""
    q, app, state, tag = (_opt_str(msg.get(k)) for k in ("q", "app", "state", "tag"))
    if None in (q, app, state, tag) or (state and state not in STATES):
        return None
    offset, limit = msg.get("offset", 0), msg.get("limit", DEFAULT_PAGE)
    for v in (offset, limit):
        if not isinstance(v, int) or isinstance(v, bool):
            return None
    unread = msg.get("unread_only", False)
    if not isinstance(unread, bool):
        return None
    return ListQuery(
        q=q,
        offset=max(0, offset),
        limit=min(max(1, limit), MAX_PAGE),
        app=app,
        state=state,
        tag=tag,
        unread_only=unread,
    )


def _setting_str(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= MAX_SETTING_STR and "\0" not in v


def parse_setting_values(v: Any, *, scalars: bool) -> dict[str, Any] | None:
    """A {name: value} dict from the page: bounded, flat. Theme values are strings only; config
    values may also be bool / int / float / None (None: back to the default). None: reject."""
    if not isinstance(v, dict) or len(v) > MAX_SETTING_KEYS:
        return None
    for key, val in v.items():
        if not isinstance(key, str) or not SETTING_NAME.fullmatch(key.removeprefix("--")):
            return None
        if _setting_str(val):
            continue
        ok = scalars and (
            val is None
            or isinstance(val, bool)
            or (isinstance(val, int) and abs(val) <= 2**53)
            or (isinstance(val, float) and val == val and abs(val) < 1e15)
        )
        if not ok:
            return None
    return dict(v)


def valid_rule_data(v: Any) -> dict[str, Any] | None:
    """A rule table from the page: an object whose JSON is at most 64 KiB, else None."""
    if not isinstance(v, dict):
        return None
    try:
        raw = json.dumps(v, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return v if len(raw.encode("utf-8", "replace")) <= MAX_RULE_JSON else None


def rules_index(v: Any) -> int | None:
    ok = isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_RULES_INDEX
    return v if ok else None


def valid_sample(v: Any) -> dict[str, Any] | None:
    """The sample notification of rules_test: text fields <= 4 KiB, <= 50 scalar hints."""
    if not isinstance(v, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("app", "summary", "body", "urgency", "category"):
        val = v.get(key, "")
        if val is None:
            val = ""
        if not isinstance(val, str) or len(val) > MAX_SAMPLE_STR or "\0" in val:
            return None
        out[key] = val
    hints = v.get("hints")
    hints = {} if hints is None else hints
    if not isinstance(hints, dict) or len(hints) > MAX_SAMPLE_HINTS:
        return None
    clean: dict[str, Any] = {}
    for k, val in hints.items():
        if not isinstance(k, str) or len(k) > 256 or "\0" in k:
            return None
        scalar = isinstance(val, bool) or (isinstance(val, int) and abs(val) <= 2**53)
        scalar = scalar or (isinstance(val, float) and val == val and abs(val) < 1e15)
        if not (
            scalar or (isinstance(val, str) and len(val) <= MAX_SAMPLE_STR and "\0" not in val)
        ):
            return None
        clean[k] = val
    out["hints"] = clean
    return out


def settings_failure(exc: Exception) -> dict[str, Any]:
    """{ok: false, errors} for a failed settings call. A SettingsError carries `.errors`
    ({field: message}); anything else becomes one general message."""
    errors = getattr(exc, "errors", None)
    if isinstance(errors, dict) and errors:
        return {"ok": False, "errors": {str(k): str(m) for k, m in errors.items()}}
    return {"ok": False, "errors": {"": str(exc) or exc.__class__.__name__}}


def link_allowed(href: Any) -> str | None:
    """The cleaned href when it is a short http/https/mailto URL, else None."""
    if not isinstance(href, str) or len(href) > 4096:
        return None
    return safe_href(href)


# ---- saved view state ------------------------------------------------------------------------


def _bool_map(v: Any, key_ok: Callable[[str], bool], cap: int) -> dict[str, bool] | None:
    if not isinstance(v, dict) or len(v) > cap:
        return None
    if not all(isinstance(k, str) and key_ok(k) and isinstance(x, bool) for k, x in v.items()):
        return None
    return dict(v)


def _day_ok(k: str) -> bool:
    return DAY_KEY.fullmatch(k) is not None


def _app_ok(k: str) -> bool:
    return len(k) <= MAX_TEXT


def parse_expand(msg: dict) -> dict[str, Any] | None:
    """Validate the overrides of a `set_expand` message (or a loaded file). None: reject.

    days: {"YYYY-MM-DD": bool}   groups: {day: {app: bool}}   apps: {app: bool}
    """
    days = _bool_map(msg.get("days", {}), _day_ok, MAX_STATE_DAYS)
    apps = _bool_map(msg.get("apps", {}), _app_ok, MAX_STATE_APPS)
    raw = msg.get("groups", {})
    if days is None or apps is None or not isinstance(raw, dict) or len(raw) > MAX_STATE_DAYS:
        return None
    groups: dict[str, dict[str, bool]] = {}
    total = 0
    for day, per_app in raw.items():
        if not isinstance(day, str) or not _day_ok(day):
            return None
        m = _bool_map(per_app, _app_ok, MAX_STATE_GROUPS)
        total += len(m) if m is not None else 0
        if m is None or total > MAX_STATE_GROUPS:
            return None
        groups[day] = m
    return {"days": days, "groups": groups, "apps": apps}


class CenterState:
    """The center list's view state, kept in a small JSON file (never touches the history DB).

    {"group_by": "day"|"app", "days": {day: bool}, "groups": {day: {app: bool}},
     "apps": {app: bool}}. The values are overrides of the defaults, which the page owns (today
    expanded, older days collapsed, app groups expanded). A missing, unreadable or malformed file
    means no overrides. `path=None` keeps it in memory. Writes are atomic (temp file in the same
    directory, then rename) and a failed write is logged, not raised.
    """

    def __init__(self, path: str | os.PathLike | None = DEFAULT_STATE_PATH) -> None:
        self.path: Path | None = None if path is None else Path(path).expanduser()
        self.group_by = "day"
        self.expand: dict[str, Any] = parse_expand({})  # type: ignore[assignment]
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        if raw.get("group_by") in GROUP_MODES:
            self.group_by = raw["group_by"]
        if (exp := parse_expand(raw)) is not None:
            self.expand = exp

    def payload(self) -> dict[str, Any]:
        return {"group_by": self.group_by, **self.expand}

    def set_group_by(self, mode: str) -> None:
        if mode != self.group_by:
            self.group_by = mode
            self.save()

    def set_expand(self, expand: dict[str, Any]) -> None:
        if expand != self.expand:
            self.expand = expand
            self.save()

    def save(self) -> bool:
        if self.path is None:  # in-memory (tests): nothing to write
            return True
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".center-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.payload(), f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False


# ---- payloads --------------------------------------------------------------------------------


def plain_text(runs: list[dict]) -> str:
    return "".join(r["text"] for r in runs)


def preview(runs: list[dict]) -> str:
    """The first non-empty line of the body, plain text, capped."""
    for line in plain_text(runs).splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= PREVIEW_CHARS else line[: PREVIEW_CHARS - 1] + "…"
    return ""


def is_unread(n: Notification) -> bool:
    return n.read_at is None and n.state in (State.SHOWN, State.DND)


def is_open(n: Notification) -> bool:
    """Still on screen or waiting: not closed, and shown (blocked/dnd ones never were)."""
    return n.closed_at is None and n.state == State.SHOWN


def list_item(n: Notification, icon: str, runs: list[dict] | None = None) -> dict[str, Any]:
    """One row of the history list. No full body: just a preview line."""
    runs = body_runs(n.body) if runs is None else runs
    return {
        "id": n.id,
        "app": n.app_name,
        "summary": n.summary,
        "preview": preview(runs),
        "icon": icon,
        "urgency": n.urgency.name.lower(),
        "category": n.category,
        "state": str(n.state),
        "tags": list(n.tags),
        "classes": list(n.classes),
        "received_at": n.received_at,
        "unread": is_unread(n),
        "open": is_open(n),
    }


def detail_item(n: Notification, icon: str) -> dict[str, Any]:
    """Everything the details page shows. `body` is sanitized runs, never raw markup."""
    runs = body_runs(n.body)
    links: list[dict[str, str]] = []
    for r in runs:
        href = r.get("href")
        if href and all(link["href"] != href for link in links):
            links.append({"href": href, "text": r["text"]})
    return list_item(n, icon, runs) | {
        "body": runs,
        "links": links,
        "highlights": [dict(h) for h in n.highlights],
        "actions": [{"key": k, "label": label} for k, label in n.actions],
        "buttons": [button_web(b) for b in n.buttons],
        "reply": reply_of(n),
        "hints": n.hints,
        "matched_rules": list(n.matched_rules),
        "sound": n.sound,
        "desktop_entry": n.desktop_entry,
        "expire_timeout": n.expire_timeout,
        "timeout_ms": n.timeout_ms,
        "read_at": n.read_at,
        "closed_at": n.closed_at,
        "close_reason": None if n.close_reason is None else n.close_reason.name.lower(),
    }


def button_press(store: Any, msg: dict) -> tuple[int, str] | None:
    """(id, button) when `msg` names a known notification and one of its own rule buttons."""
    id, button = msg.get("id"), msg.get("button")
    if not valid_id(id) or not isinstance(button, str):
        return None
    n = store.get(id)
    if n is None or button not in {b["id"] for b in n.buttons}:
        return None
    return id, button


def run_status(run: dict) -> tuple[str, str]:
    """(text, state) for one run row. state: ok | failed | signal | timeout | skipped | running."""
    if run.get("skipped_reason"):
        return f"skipped: {run['skipped_reason']}", "skipped"
    sig, code = run.get("signal"), run.get("exit_code")
    if TIMED_OUT_MARK in (run.get("output_tail") or ""):
        return "timed out", "timeout"
    if sig is not None:
        try:
            name = _signal.Signals(sig).name
        except ValueError:
            name = str(sig)
        return f"signal {name}", "signal"
    if code is None:
        return "running", "running"
    return (f"exit {code} \u2713", "ok") if code == 0 else (f"exit {code}", "failed")


def run_item(run: dict) -> dict[str, Any]:
    """One run for the page: a fixed allow-list of fields, so the command (`cmd`) can never
    reach it. `title` is what the log shows as the trigger."""
    text, state = run_status(run)
    button = run.get("button_label")
    trigger = run.get("trigger") or "rule"
    tail = run.get("output_tail")
    return {
        "id": run.get("id"),
        "rule": run.get("rule"),
        "trigger": trigger,
        "button_label": button,
        "title": f"button: {button}" if trigger == "button" else (run.get("rule") or "rule"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "exit_code": run.get("exit_code"),
        "signal": run.get("signal"),
        "duration_ms": run.get("duration_ms"),
        "skipped_reason": run.get("skipped_reason"),
        "status": text,
        "state": state,
        "output_tail": tail[-MAX_TAIL:] if isinstance(tail, str) else "",
    }


def runs_payload(rows: list[dict]) -> list[dict[str, Any]]:
    """Run log rows as the page gets them: newest first (the store returns oldest first)."""
    return [run_item(r) for r in reversed(rows[-MAX_RUNS:])]


def _js(value: Any) -> str:
    """A JS literal for `value`; ASCII-only so no line-separator surprises."""
    return json.dumps(value, ensure_ascii=True)


# ---- bridge (no display needed) --------------------------------------------------------------


class CenterBridge:
    def __init__(
        self,
        store: Any,
        *,
        on_invoke_action: Callable[[int, str], bool],
        on_dismiss: Callable[[int], None],
        on_mark_read: Callable[[list[int]], None],
        on_delete: Callable[[list[int]], None],
        on_clear: Callable[[], None],
        run_js: Callable[[str], None],
        copy_text: Callable[[str], None],
        open_link: Callable[[str], None],
        theme_lookup: Callable[[str], str | None] | None = None,
        detail_theme_lookup: Callable[[str], str | None] | None = None,
        on_button: Callable[[int, str], None] | None = None,
        get_runs: Callable[[int], list[dict]] | None = None,
        state: CenterState | None = None,
        settings: Any = None,
        on_reply_choice: Callable[[int, str], dict] | None = None,
        on_reply_text: Callable[[int, str], dict] | None = None,
        on_reply_confirm: Callable[[int, str], Any] | None = None,
        on_reply_cancel: Callable[[int, str], Any] | None = None,
        on_perf: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.on_perf = on_perf  # None: the page's open-timing reports are dropped
        self.settings = settings  # a SettingsService; None: the panel says "unavailable"
        self.state = state if state is not None else CenterState(None)
        self.on_button = on_button  # None: the page hides rule buttons
        self.on_reply_choice = on_reply_choice  # all four None: the details page gets no `reply`
        self.on_reply_text = on_reply_text
        self.on_reply_confirm = on_reply_confirm
        self.on_reply_cancel = on_reply_cancel
        self.replies = ReplyRouter(self, self._reply_lookup, lambda js: self.run_js(js))
        self.get_runs = get_runs  # None: the run log is empty
        self.images: ImageService | None = None  # set by build_daemon; None: no previews
        self.on_invoke_action = on_invoke_action
        self.on_dismiss = on_dismiss
        self.on_mark_read = on_mark_read
        self.on_delete = on_delete
        self.on_clear = on_clear
        self.run_js = run_js
        self.copy_text = copy_text
        self.open_link = open_link
        self._theme_lookup = theme_lookup or (lambda name: None)
        self._detail_theme_lookup = detail_theme_lookup or self._theme_lookup
        self._icons: dict[tuple[str, ...], str] = {}

    # ---- payload building ----

    def icon_for(self, n: Notification, detail: bool = False) -> str:
        """The file:// URI, or "" (the page then draws a letter avatar). Misses are not cached,
        so a .desktop file installed later is found."""
        h = n.hints
        key = (
            "d" if detail else "l",
            n.app_name.casefold(),
            n.app_icon,
            str(h.get("image-path") or h.get("image_path") or ""),
            str(h.get("desktop-entry") or h.get("desktop_entry") or ""),
        )
        if key not in self._icons:
            lookup = self._detail_theme_lookup if detail else self._theme_lookup
            uri = resolve_icon(n.app_icon, n.hints, lookup, n.app_name)
            if not uri:
                return ""
            self._icons[key] = uri
        return self._icons[key]

    def list_payload(self, q: ListQuery) -> dict[str, Any]:
        rows = self.store.query(
            q.q,
            q.offset,
            q.limit + 1,  # one extra row tells whether another page exists
            app=q.app or None,
            state=q.state or None,
            tag=q.tag or None,
            unread_only=q.unread_only,
        )
        return {
            "items": [self.added_item(n) for n in rows[: q.limit]],
            "has_more": len(rows) > q.limit,
        }

    def detail_payload(self, id: int) -> dict[str, Any]:
        n = self.store.get(id)
        if n is None:
            return {"notification": None}
        item = detail_item(n, self.icon_for(n, True))
        item["image"] = self._image(n)
        item["reply"] = self.replies.present(item["reply"])
        return {
            "notification": item,
            "buttons_enabled": self.on_button is not None,
            "runs": self.runs_for(id),
        }

    def _reply_lookup(self, id: int) -> dict[str, Any] | None:
        n = self.store.get(id)
        return None if n is None else self.replies.present(reply_of(n))

    def runs_for(self, id: int) -> list[dict[str, Any]]:
        if self.get_runs is None:
            return []
        try:
            return runs_payload(self.get_runs(id))
        except Exception:
            log.exception("reading the run log of %d failed", id)
            return []

    def facets_payload(self) -> dict[str, Any]:
        return {"apps": self.store.apps(), "tags": self.store.tags()}

    def added_item(self, n: Notification) -> dict[str, Any]:
        return list_item(n, self.icon_for(n)) | {"image": self._image(n)}

    def _image(self, n: Notification) -> dict[str, str] | None:
        return self.images.info(n) if self.images is not None else None

    # ---- messages from the page ----

    def reply(self, req: int, data: dict[str, Any]) -> None:
        self.run_js(f"window.nc.reply({int(req)}, {_js(data)})")

    def handle(self, msg: Any) -> None:
        """Dispatch one decoded message. Untrusted: anything malformed is dropped (or, for a
        request with a usable `req`, answered with {error})."""
        if not isinstance(msg, dict):
            return
        log.debug("page -> python: %s", json.dumps(msg, ensure_ascii=False)[:300])
        kind = msg.get("type")
        try:
            if (
                kind in SETTINGS_KINDS
                or kind in RULES_KINDS
                or kind
                in (
                    "list",
                    "get",
                    "facets",
                    "action",
                    "dismiss",
                    "runs",
                    "center_state",
                )
            ):
                self._request(kind, msg)
            elif kind in ("mark_read", "delete"):
                ids = valid_ids(msg.get("ids"))
                if ids:
                    (self.on_mark_read if kind == "mark_read" else self.on_delete)(ids)
            elif kind == "mark_all_read":
                ids = self.store.unread_ids()
                if ids:
                    self.on_mark_read(ids)
            elif kind == "clear":
                self.on_clear()
            elif kind == "set_expand":
                if (expand := parse_expand(msg)) is not None:
                    self.state.set_expand(expand)
                else:
                    log.warning("ignored a malformed set_expand")
            elif kind == "set_group_by":
                if msg.get("mode") in GROUP_MODES:
                    self.state.set_group_by(msg["mode"])
            elif kind == "copy":
                self._copy(msg)
            elif kind == "button":
                self._press_button(msg)
            elif kind in REPLY_KINDS:
                self.replies.handle(msg)
            elif kind in IMAGE_KINDS:
                if self.images is not None:
                    self.images.handle(msg, self.run_js)
            elif kind == "link":
                href = link_allowed(msg.get("href"))
                if href:
                    self.open_link(href)
            elif kind == "perf":
                if msg.get("phase") in PAGE_PHASES and self.on_perf is not None:
                    self.on_perf(msg["phase"])
            else:
                log.warning("ignored page message of type %r", kind)
        except Exception:
            log.exception("page message %r failed", kind)

    def _request(self, kind: str, msg: dict) -> None:
        req = msg.get("req")
        if not valid_id(req):
            return
        try:
            self.reply(req, self._answer(kind, msg))
        except Exception as exc:
            log.exception("request %r failed", kind)
            self.reply(req, {"error": str(exc) or exc.__class__.__name__})

    def _settings_answer(self, kind: str, msg: dict) -> dict[str, Any]:
        svc = self.settings
        if svc is None:
            return {"ok": False, "unavailable": True, "errors": {"": "Settings unavailable"}}
        bad = {"ok": False, "errors": {"": "bad request"}}
        try:
            if kind == "settings_get":
                return {"ok": True, "settings": svc.get()}
            if kind == "settings_save_theme":
                values = parse_setting_values(msg.get("values"), scalars=False)
                if values is None:
                    return bad
                return {"ok": True, "theme": svc.save_theme(values)}
            if kind == "settings_preset":
                name = msg.get("name")
                if not isinstance(name, str) or not SETTING_NAME.fullmatch(name):
                    return bad
                return {"ok": True, "values": svc.preset_values(name)}
            if kind == "settings_save_style":
                text = msg.get("text")
                if (
                    not isinstance(text, str)
                    or len(text.encode("utf-8", "replace")) > MAX_STYLE_BYTES
                ):
                    return {
                        "ok": False,
                        "errors": {"style": "style.css is too large (256 KiB max)"},
                    }
                svc.save_style(text)
                return {"ok": True}
            if kind == "settings_save_config":
                section = msg.get("section")
                values = parse_setting_values(msg.get("values"), scalars=True)
                if section not in CONFIG_SECTIONS or values is None:
                    return bad
                res = svc.save_config(section, values)
                restart = res.get("restart_required", []) if isinstance(res, dict) else []
                return {
                    "ok": True,
                    "restart_required": [str(k) for k in restart],
                    "restart_command": f"{RESTART_BIN} --restart",
                }
            if kind == "settings_preview_sound":
                spec, volume = msg.get("spec"), msg.get("volume", 100)
                if (
                    not _setting_str(spec)
                    or not isinstance(volume, int)
                    or isinstance(volume, bool)
                    or not 0 <= volume <= 100
                ):
                    return bad
                res = svc.preview_sound(spec, volume)
                res = res if isinstance(res, dict) else {}
                return {
                    "ok": bool(res.get("ok")),
                    "resolved": res.get("resolved") or "",
                    "error": res.get("error") or "",
                }
            kind_ = msg.get("kind")  # settings_send_test
            if kind_ not in TEST_KINDS:
                return bad
            return {"ok": True, "id": int(svc.send_test(kind_))}
        except Exception as exc:
            if not getattr(exc, "errors", None):
                log.exception("%s failed", kind)
            return settings_failure(exc)

    def _rules_answer(self, kind: str, msg: dict) -> dict[str, Any]:
        svc = self.settings
        if svc is None:
            return {"ok": False, "unavailable": True, "errors": {"": "Settings unavailable"}}
        bad = {"ok": False, "errors": {"": "bad request"}}
        try:
            if kind == "rules_get":
                return {"ok": True, **svc.rules_get()}
            if kind == "rules_get_source":
                res = svc.rules_get_source()
                return {"ok": True, "text": str(res.get("text", "")), "path": res.get("path", "")}
            if kind == "rules_save_source":
                text = msg.get("text")
                if (
                    not isinstance(text, str)
                    or len(text.encode("utf-8", "replace")) > MAX_RULES_BYTES
                ):
                    return {
                        "ok": False,
                        "errors": {"source": "rules.toml is too large (256 KiB max)"},
                    }
                return {"ok": True, **svc.rules_save_source(text)}
            if kind == "rules_test":
                data, sample = valid_rule_data(msg.get("data")), valid_sample(msg.get("sample"))
                index = msg.get("index")
                if data is None or sample is None:
                    return bad
                if index is not None and rules_index(index) is None:
                    return bad
                res = svc.rules_test(data, sample, index)
                return {"ok": True, "matched": bool(res["matched"]), "result": res["result"]}
            index = rules_index(msg.get("index"))
            expect = msg.get("expect")  # the rule name the page listed (optional)
            if expect is not None and not (isinstance(expect, str) and len(expect) <= 512):
                return bad
            kw = {} if expect is None else {"expect": expect}
            if kind == "rules_update":
                data = valid_rule_data(msg.get("data"))
                if index is None or data is None:
                    return bad
                return {"ok": True, **svc.rules_update(index, data, **kw)}
            if kind == "rules_add":
                data, position = valid_rule_data(msg.get("data")), msg.get("position")
                if data is None or (position is not None and rules_index(position) is None):
                    return bad
                return {"ok": True, **svc.rules_add(data, position)}
            if kind == "rules_delete":
                if index is None:
                    return bad
                return {"ok": True, **svc.rules_delete(index, **kw)}
            if kind == "rules_move":
                to = rules_index(msg.get("to"))
                if index is None or to is None:
                    return bad
                return {"ok": True, **svc.rules_move(index, to, **kw)}
            on = msg.get("on")  # rules_set_enabled
            if index is None or not isinstance(on, bool):
                return bad
            return {"ok": True, **svc.rules_set_enabled(index, on, **kw)}
        except Exception as exc:
            if not getattr(exc, "errors", None):
                log.exception("%s failed", kind)
            return settings_failure(exc)

    def _answer(self, kind: str, msg: dict) -> dict[str, Any]:
        if kind in SETTINGS_KINDS:
            return self._settings_answer(kind, msg)
        if kind in RULES_KINDS:
            return self._rules_answer(kind, msg)
        if kind == "list":
            q = parse_list_query(msg)
            return {"error": "bad request"} if q is None else self.list_payload(q)
        if kind == "facets":
            return self.facets_payload()
        if kind == "center_state":
            return self.state.payload()
        id = msg.get("id")
        if not valid_id(id):
            return {"error": "bad request"}
        if kind == "get":
            return self.detail_payload(id)
        if kind == "dismiss":
            self.on_dismiss(id)
            return {"ok": True}
        if kind == "runs":
            return {"runs": self.runs_for(id)}
        key = msg.get("key")  # kind == "action"
        n = self.store.get(id)
        if not isinstance(key, str) or n is None or key not in {k for k, _ in n.actions}:
            return {"ok": False}
        return {"ok": bool(self.on_invoke_action(id, key))}

    def _press_button(self, msg: dict) -> None:
        """A rule button: the id must be a known notification and the button one of its own."""
        if self.on_button is not None and (pair := button_press(self.store, msg)) is not None:
            self.on_button(*pair)

    def _copy(self, msg: dict) -> None:
        id, what = msg.get("id"), msg.get("what")
        if not valid_id(id) or what not in COPY_WHAT:
            return
        n = self.store.get(id)
        if n is None:
            return
        self.copy_text(plain_text(body_runs(n.body)) if what == "body" else n.summary)


# ---- window (needs a display) ----------------------------------------------------------------


class CenterWindow:
    def __init__(
        self,
        store: Any,
        web_dir: Path,
        *,
        on_invoke_action: Callable[[int, str], bool],
        on_dismiss: Callable[[int], None],
        on_mark_read: Callable[[list[int]], None],
        on_delete: Callable[[list[int]], None],
        on_clear: Callable[[], None],
        on_button: Callable[[int, str], None] | None = None,
        get_runs: Callable[[int], list[dict]] | None = None,
        state_path: str | os.PathLike = DEFAULT_STATE_PATH,
        settings: Any = None,
        on_reply_choice: Callable[[int, str], dict] | None = None,
        on_reply_text: Callable[[int, str], dict] | None = None,
        on_reply_confirm: Callable[[int, str], Any] | None = None,
        on_reply_cancel: Callable[[int, str], Any] | None = None,
    ) -> None:
        self.web_dir = Path(web_dir)
        self._user_css: str | None = None
        self._ready = False
        self._stale = False
        self._pending_js: list[str] = []
        self._window: Any = None
        self._view: Any = None
        self._ucm: Any = None
        self._ucm_handler: Any = None
        self._closed = False
        self._size = DEFAULT_SIZE
        self._page_uri = ""
        self.trace = OpenTrace("center", ("painted",), schedule=glib_schedule)
        self.bridge = CenterBridge(
            store,
            on_invoke_action=on_invoke_action,
            on_dismiss=on_dismiss,
            on_mark_read=on_mark_read,
            on_delete=on_delete,
            on_clear=on_clear,
            run_js=self._eval,
            copy_text=self._copy_text,
            open_link=self._open_link,
            theme_lookup=self._theme_lookup,
            detail_theme_lookup=icons.gtk_theme_lookup(DETAIL_ICON_SIZE),
            on_button=on_button,
            get_runs=get_runs,
            state=CenterState(state_path),
            settings=settings,
            on_reply_choice=on_reply_choice,
            on_reply_text=on_reply_text,
            on_reply_confirm=on_reply_confirm,
            on_reply_cancel=on_reply_cancel,
            on_perf=self.trace.mark,
        )

    on_reply_choice = bridge_attr("on_reply_choice")
    on_reply_text = bridge_attr("on_reply_text")
    on_reply_confirm = bridge_attr("on_reply_confirm")
    on_reply_cancel = bridge_attr("on_reply_cancel")
    images = bridge_attr("images")

    @property
    def settings(self) -> Any:
        return self.bridge.settings

    @settings.setter
    def settings(self, svc: Any) -> None:
        self.bridge.settings = svc

    @property
    def on_button(self) -> Callable[[int, str], None] | None:
        return self.bridge.on_button

    @on_button.setter
    def on_button(self, fn: Callable[[int, str], None] | None) -> None:
        self.bridge.on_button = fn

    @property
    def get_runs(self) -> Callable[[int], list[dict]] | None:
        return self.bridge.get_runs

    @get_runs.setter
    def get_runs(self, fn: Callable[[int], list[dict]] | None) -> None:
        self.bridge.get_runs = fn

    # ---- public API ----

    def show(self) -> None:
        if self._closed:
            return
        self.trace.begin(self._window is None)
        self._ensure_view()
        self._window.resize(*self._size)
        self._window.show_all()
        self._window.present()
        self.trace.mark("present")
        self._eval("window.ncPaint.arm()")
        if self._stale:
            self._stale = False
            self._eval("window.nc.refresh()")

    def prewarm(self) -> None:
        """Build the window and the WebView hidden (nothing is mapped) so the first open skips
        the web process spawn and the page load."""
        if self._closed:
            return
        try:
            self._ensure_view()
        except Exception:
            log.exception("could not prewarm the center")

    def trace_mark(self, phase: str) -> None:
        self.trace.mark(phase)

    def hide(self) -> None:
        if self._window is not None and self._window.get_visible():
            self._remember_size()
            self._window.hide()

    def teardown(self) -> None:
        """Tear down the WebView and the window (delete-event only hides). Idempotent; the
        center never builds a window afterwards. Safe when it was never shown."""
        if self._closed:
            return
        self._closed = True
        try:
            self.hide()  # saves the window size to the state file
        except Exception:
            log.exception("teardown: hide failed")
        ucm, handler, view, win = self._ucm, self._ucm_handler, self._view, self._window
        self._ucm = self._ucm_handler = self._view = self._window = None
        self._ready = False
        self._pending_js = []
        teardown_webview(ucm, handler, view, (win,))

    def toggle(self) -> None:
        if self.visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self) -> bool:
        return self._window is not None and bool(self._window.get_visible())

    def show_notification(self, id: int) -> None:
        self.show()
        self._eval(f"window.nc.navigate({_js(f'#/n/{int(id)}')})")

    def notify_added(self, n: Notification) -> None:
        if self._window is None:
            return  # never opened: the first load reads the store
        if not self.visible:
            self._stale = True
            return
        self._eval(f"window.nc.added({_js(self.bridge.added_item(n))})")

    def refresh(self) -> None:
        if self.visible:
            self._eval("window.nc.refresh()")
        elif self._window is not None:
            self._stale = True

    def set_user_css(self, css: str) -> None:
        self._user_css = css
        self._eval(f"window.nc.setUserCss({_js(css)})")

    def save_snapshot(self, path: str | Path, done: Callable[[bool], None] | None = None) -> None:
        """Write a PNG of the visible page (dev/QA aid). `done(ok)` runs when finished."""
        from gi.repository import WebKit2

        def finish(view: Any, result: Any) -> None:
            ok = False
            try:
                surface = view.get_snapshot_finish(result)
                surface.write_to_png(str(path))
                ok = True
            except Exception as exc:
                print(f"center: snapshot failed: {exc}", file=sys.stderr)
            if done is not None:
                done(ok)

        self._view.get_snapshot(
            WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, finish
        )

    # ---- plumbing ----

    def _eval(self, script: str) -> None:
        if self._closed:
            return
        if not self._ready or self._view is None:
            self._pending_js.append(script)
            return
        self._run(script)

    def _run(self, script: str) -> None:
        self._view.evaluate_javascript(script, -1, None, None, None, None, None)

    def _remember_size(self) -> None:
        w, h = self._window.get_size()
        if w > 100 and h > 100:
            self._size = (w, h)

    def _theme_lookup(self, name: str) -> str | None:
        return icons.gtk_theme_lookup(ICON_SIZE)(name)

    def _open_link(self, href: str) -> None:
        try:
            from gi.repository import Gio

            Gio.AppInfo.launch_default_for_uri(href, None)
        except Exception as exc:
            print(f"center: could not open link: {exc}", file=sys.stderr)

    def _copy_text(self, text: str) -> None:
        try:
            from gi.repository import Gdk, Gtk

            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text(text, -1)
            clip.store()
        except Exception as exc:
            print(f"center: could not copy: {exc}", file=sys.stderr)

    def _ensure_view(self) -> None:
        if self._window is not None or self._closed:
            return
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        gi.require_version("JavaScriptCore", "4.1")
        from gi.repository import Gdk, GLib, Gtk, JavaScriptCore, WebKit2  # noqa: F401

        if Gdk.Display.get_default() is None:
            raise RuntimeError("no display (is WAYLAND_DISPLAY set?)")

        GLib.set_prgname(APP_ID)  # the Wayland app id GTK3 hands to the compositor
        win = Gtk.Window(title=TITLE)
        win.set_wmclass(APP_ID, APP_ID)
        win.set_role("notification-center")
        win.set_default_size(*self._size)
        win.connect("delete-event", self._on_delete_event)

        if self.images is not None:
            register_scheme(self.images)  # before the view exists; once per process
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("nc")
        self._ucm_handler = ucm.connect("script-message-received::nc", self._on_script_message)
        self._ucm = ucm
        view = WebKit2.WebView.new_with_user_content_manager(ucm)
        view.set_background_color(Gdk.RGBA(0.122, 0.129, 0.145, 1.0))
        settings = view.get_settings()
        settings.set_enable_javascript(True)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        view.connect("context-menu", lambda *_a: True)
        view.connect("decide-policy", self._on_decide_policy)
        view.connect("load-changed", self._on_load_changed)

        self._page_uri = file_uri(str((self.web_dir / "center.html").resolve()))
        self._window, self._view = win, view
        win.add(view)
        view.show()
        view.load_uri(self._page_uri)

    def _on_delete_event(self, *_a: Any) -> bool:
        self.hide()
        return True  # never destroy: the window is reused

    def _on_script_message(self, _ucm: Any, value: Any) -> None:
        try:
            if hasattr(value, "get_js_value"):  # legacy JavascriptResult wrapper
                value = value.get_js_value()
            msg = json.loads(value.to_json(0))
        except Exception as exc:
            print(f"center: bad page message: {exc}", file=sys.stderr)
            return
        self.bridge.handle(msg)

    def _on_load_changed(self, _view: Any, event: Any) -> None:
        from gi.repository import WebKit2

        if event != WebKit2.LoadEvent.FINISHED or self._ready:
            return
        self._ready = True
        if self._user_css is not None:
            self._run(f"window.nc.setUserCss({_js(self._user_css)})")
        pending, self._pending_js = self._pending_js, []
        for script in pending:
            if "setUserCss" in script and self._user_css is not None:
                continue  # already applied above with the latest text
            self._run(script)

    def _on_decide_policy(self, _view: Any, decision: Any, dtype: Any) -> bool:
        """The WebView never leaves our own page (hash changes inside it are fine)."""
        from gi.repository import WebKit2

        if dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if _same_page(uri, self._page_uri):
                decision.use()
            else:
                decision.ignore()
            return True
        if dtype == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False


def _same_page(uri: str, page: str) -> bool:
    """True when `uri` is `page` itself, ignoring the #fragment."""
    try:
        return urlsplit(uri)._replace(fragment="").geturl() == page
    except ValueError:
        return False


__all__ = ["CenterWindow", "CenterBridge", "APP_ID"]
