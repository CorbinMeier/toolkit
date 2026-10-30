"""Sender-defined reply actions: choices and a free-text reply that make the daemon send ONE HTTP
request. Protocol, threat model and config: docs/reply-actions.md.

Decisions where the brief was silent:
- The sender's hint (`x-nc-actions`, JSON, <= 8 KiB) only DESCRIBES a request. Whether it may be
  sent is decided by `http-actions.toml` (the allowlist, hand-edited, missing = empty = inert).
  The sender never supplies headers, tokens or redirects.
- Strict parsing: unknown keys, bad ids, an unknown `{placeholder}`, a placeholder in the
  scheme/host part, choices without `{choice}` or text without `{text}` in the request all make the
  hint invalid. A placeholder is `{identifier}`, so literal JSON braces in a body are fine.
- The URL is normalized (`normalize_url`) and THAT string is checked against the prefixes and
  sent. The final URL, after substitution, is normalized and checked again at send time.
- Values in the URL are percent-encoded (`quote(safe="")`); a value that is exactly `.` or `..`
  is refused. A placeholder that is not used by a given reply (e.g. `{text}` on a choice reply)
  becomes the empty string.
- A broken `http-actions.toml` FAILS CLOSED (empty allowlist, error reported), unlike rules.toml
  which keeps the previous rules: widening access by keeping an old file is the worse surprise.
- Prefix match ends on a path-segment boundary: the prefix `http://h/api/reply` does not match
  `http://h/api/reply-evil`.
- Proxies from the environment are never used; redirects are never followed (a 3xx is the result).
- A token is read from `token_file` at send time and never logged; if it shows up in the response
  it is redacted from the stored output.
- Each reply is recorded in `runs` with the final URL (it carries the choice and text); the body
  is not recorded.
- The invalid-hint error is derived by re-parsing the hint (`plan`), also on web payloads;
  `Notification.reply_error` is its runtime-only copy, set on arrival together with the WARNING.
- Reply actions are not scripts: `--no-run` does not disable them. The allowlist does.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlsplit

from gi.repository import GLib

log = logging.getLogger("notifyd.http")

HINT_KEY = "x-nc-actions"
ANSWERED_KEY = "x-nc-answered"
CONFIG_NAME = "http-actions.toml"

MAX_HINT_BYTES = 8192
MAX_CHOICES = 6
MAX_LABEL = 40
MAX_PLACEHOLDER = 80
MAX_TEXT = 2000
MAX_URL = 2000
MAX_BODY_TEMPLATE = 4096
MAX_BODY = 65536
OUTPUT_TAIL = 4096
MAX_SEND_ERRORS = 500  # remembered send errors; the oldest go first
ANSWERED_TEXT = 200
METHODS = ("POST", "GET", "PUT", "PATCH")
CONTENT_TYPES = ("application/json", "text/plain", "application/x-www-form-urlencoded")
DEFAULT_PORTS = {"http": 80, "https": 443}
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "content-type", "connection"}

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class HintError(ValueError):
    """The x-nc-actions hint is invalid."""


class ConfigError(ValueError):
    """http-actions.toml is invalid."""


class Refused(Exception):
    """A reply cannot be sent; the message is the reason shown to the user."""


# ---- URL handling ---------------------------------------------------------------------------


def normalize_url(url: str) -> str | None:
    """Canonical form used for the allowlist check AND for sending, or None if unacceptable.

    http/https only; lowercase scheme and host; default port dropped; no userinfo; no fragment;
    `.` and `..` segments (also when percent-encoded) resolved against the root; control
    characters, spaces and backslashes rejected.
    """
    if not isinstance(url, str) or len(url) > MAX_URL or _CTRL_RE.search(url):
        return None
    if " " in url or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or parts.fragment or "#" in url:
        return None
    if "@" in parts.netloc:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    segments: list[str] = []
    raw = parts.path.split("/")[1:] if parts.path else [""]
    for i, seg in enumerate(raw):
        last = i == len(raw) - 1
        dec = unquote(seg)
        if dec == ".":
            if last:
                segments.append("")
            continue
        if dec == "..":
            if segments:
                segments.pop()
            if last:
                segments.append("")
            continue
        segments.append(seg)
    path = "/" + "/".join(segments)
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _prefix_matches(prefix: str, url: str) -> bool:
    if not url.startswith(prefix):
        return False
    if len(url) == len(prefix) or prefix.endswith("/"):
        return True
    return url[len(prefix)] in "/?"


def display_host(url: str) -> str:
    """`host[:port]` of a normalized URL, for the UI (never the path)."""
    return urlsplit(url).netloc


# ---- the hint -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hint:
    choices: tuple[tuple[str, str], ...]  # (id, label)
    text: dict | None  # {"placeholder", "max"}
    method: str
    url: str  # template, as the sender wrote it
    body: str | None  # template
    content_type: str
    host: str  # display host of the template (placeholders are never in the authority)


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(template))


def _clean_str(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise HintError(f"{what} must be a non-empty string")
    if len(value) > limit:
        raise HintError(f"{what} is longer than {limit} characters")
    if _CTRL_RE.search(value):
        raise HintError(f"{what} has control characters")
    return value


def _parse_choices(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise HintError("choices must be a list")
    if len(raw) > MAX_CHOICES:
        raise HintError(f"more than {MAX_CHOICES} choices")
    out: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            cid, label = item, item
        elif isinstance(item, dict):
            if set(item) - {"id", "label"}:
                raise HintError("a choice has unknown keys")
            cid = item.get("id")
            label = item.get("label", cid)
        else:
            raise HintError("a choice must be a string or an object")
        if not isinstance(cid, str) or not _ID_RE.match(cid) or cid in (".", ".."):
            raise HintError("a choice id must match [A-Za-z0-9_.-]{1,40} and not be . or ..")
        label = _clean_str(label, "a choice label", MAX_LABEL)
        if any(cid == c for c, _ in out):
            raise HintError(f"duplicate choice id {cid!r}")
        out.append((cid, label))
    return tuple(out)


def _parse_text(raw: Any) -> dict | None:
    if raw is None or raw is False:
        return None
    if raw is True:
        return {"placeholder": "Reply…", "max": MAX_TEXT}
    if not isinstance(raw, dict):
        raise HintError("text must be true or an object")
    if set(raw) - {"placeholder", "max"}:
        raise HintError("text has unknown keys")
    placeholder = raw.get("placeholder", "Reply…")
    placeholder = _clean_str(placeholder, "text placeholder", MAX_PLACEHOLDER)
    limit = raw.get("max", MAX_TEXT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TEXT:
        raise HintError(f"text max must be an integer from 1 to {MAX_TEXT}")
    return {"placeholder": placeholder, "max": limit}


def parse_hint(raw: Any) -> Hint:
    """Parse the `x-nc-actions` value. Raises HintError with a short reason."""
    if not isinstance(raw, str):
        raise HintError("hint must be a string holding JSON")
    if len(raw.encode("utf-8", errors="replace")) > MAX_HINT_BYTES:
        raise HintError(f"hint is larger than {MAX_HINT_BYTES} bytes")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise HintError(f"hint is not valid JSON ({e})") from None
    if not isinstance(data, dict):
        raise HintError("hint must be a JSON object")
    if set(data) - {"choices", "text", "request"}:
        raise HintError("hint has unknown keys")
    choices = _parse_choices(data["choices"]) if "choices" in data else ()
    text = _parse_text(data.get("text"))
    if not choices and text is None:
        raise HintError("hint has neither choices nor text")
    req = data.get("request")
    if not isinstance(req, dict):
        raise HintError("request must be an object")
    # A sender-supplied "headers" key is tolerated and ignored: only the allowlist adds headers.
    if set(req) - {"method", "url", "body", "content_type", "headers"}:
        raise HintError("request has unknown keys")
    method = req.get("method", "POST")
    if method not in METHODS:
        raise HintError("request method must be one of " + ", ".join(METHODS))
    url = req.get("url")
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise HintError("request url must be a string")
    body = req.get("body")
    if body is not None:
        if not isinstance(body, str) or len(body) > MAX_BODY_TEMPLATE:
            raise HintError("request body must be a string of at most 4096 characters")
        if method == "GET":
            raise HintError("a GET request cannot have a body")
    ctype = req.get("content_type", "application/json")
    if ctype not in CONTENT_TYPES:
        raise HintError("content_type must be one of " + ", ".join(CONTENT_TYPES))
    used = _placeholders(url) | _placeholders(body or "")
    unknown = used - {"choice", "text"}
    if unknown:
        raise HintError("unknown placeholder {" + sorted(unknown)[0] + "}")
    if "choice" in used and not choices:
        raise HintError("{choice} is used but there are no choices")
    if "text" in used and text is None:
        raise HintError("{text} is used but there is no text reply")
    if choices and "choice" not in used:
        raise HintError("choices are declared but the request never uses {choice}")
    if text is not None and "text" not in used:
        raise HintError("text is declared but the request never uses {text}")
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*", url)
    if m is None or "{" in m.group(0):
        raise HintError("the url must be absolute and have no placeholder before its path")
    sample = _PLACEHOLDER_RE.sub("x", url)
    norm = normalize_url(sample)
    if norm is None:
        raise HintError("request url is not an acceptable http(s) URL")
    return Hint(choices, text, method, url, body, ctype, display_host(norm))


# ---- the allowlist file ---------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    app: str
    prefixes: tuple[str, ...]
    confirm: bool = False
    close: bool = True
    enabled: bool = True
    token_file: str | None = None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    text_reply: bool = True
    timeout_s: float = 10.0
    max_response_bytes: int = 65536
    entries: tuple[Entry, ...] = ()

    def match(self, app: str, url: str) -> tuple[Entry | None, str | None]:
        """The entry that allows `url` (normalized) for `app`, or (None, reason)."""
        if not self.enabled:
            return None, "http actions are disabled"
        if not self.entries:
            return None, "no allowlist configured"
        mine = [e for e in self.entries if e.app.casefold() == app.casefold()]
        if not mine:
            return None, "this app is not allowed to send replies"
        live = [e for e in mine if e.enabled]
        if not live:
            return None, "reply actions are disabled for this app"
        for e in live:
            if any(_prefix_matches(p, url) for p in e.prefixes):
                return e, None
        return None, "this URL is not in the allowlist for this app"


def _bool(d: dict, key: str, default: bool, where: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{where}{key} must be true or false")
    return v


def _entry(raw: Any, i: int) -> Entry:
    where = f"[[allow]] #{i + 1}: "
    if not isinstance(raw, dict):
        raise ConfigError(where + "must be a table")
    unknown = set(raw) - {"app", "prefixes", "confirm", "close", "enabled", "token_file", "headers"}
    if unknown:
        raise ConfigError(where + f"unknown key {sorted(unknown)[0]!r}")
    app = raw.get("app")
    if not isinstance(app, str) or not app.strip():
        raise ConfigError(where + "app must be a non-empty string")
    prefixes_raw = raw.get("prefixes")
    if not isinstance(prefixes_raw, list) or not prefixes_raw:
        raise ConfigError(where + "prefixes must be a non-empty list")
    prefixes = []
    for p in prefixes_raw:
        norm = normalize_url(p) if isinstance(p, str) else None
        if norm is None or "?" in norm:
            raise ConfigError(where + f"prefix {p!r} must be an http(s) URL without query")
        prefixes.append(norm)
    token_file = raw.get("token_file")
    if token_file is not None and (not isinstance(token_file, str) or not token_file):
        raise ConfigError(where + "token_file must be a path string")
    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict):
        raise ConfigError(where + "headers must be a table")
    headers = []
    for k, v in headers_raw.items():
        if not _HEADER_NAME_RE.match(k) or k.casefold() in FORBIDDEN_HEADERS:
            raise ConfigError(where + f"header name {k!r} is not allowed")
        if k.casefold() == "authorization" and token_file:
            raise ConfigError(where + "Authorization header conflicts with token_file")
        if not isinstance(v, str) or _CTRL_RE.search(v):
            raise ConfigError(where + f"header {k!r} must be a string without control characters")
        headers.append((k, v))
    return Entry(
        app=app.strip(),
        prefixes=tuple(prefixes),
        confirm=_bool(raw, "confirm", False, where),
        close=_bool(raw, "close", True, where),
        enabled=_bool(raw, "enabled", True, where),
        token_file=token_file,
        headers=tuple(headers),
    )


def parse_config(text: str) -> Config:
    """Parse http-actions.toml text. Raises ConfigError."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"not valid TOML: {e}") from None
    unknown = set(data) - {"enabled", "text_reply", "timeout_s", "max_response_bytes", "allow"}
    if unknown:
        raise ConfigError(f"unknown key {sorted(unknown)[0]!r}")
    timeout = data.get("timeout_s", 10)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not 0.1 <= timeout <= 120
    ):
        raise ConfigError("timeout_s must be a number from 0.1 to 120")
    cap = data.get("max_response_bytes", 65536)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 1 << 20:
        raise ConfigError("max_response_bytes must be an integer from 1 to 1048576")
    allow = data.get("allow", [])
    if not isinstance(allow, list):
        raise ConfigError("allow must be an array of tables")
    return Config(
        enabled=_bool(data, "enabled", True, ""),
        text_reply=_bool(data, "text_reply", True, ""),
        timeout_s=float(timeout),
        max_response_bytes=cap,
        entries=tuple(_entry(e, i) for i, e in enumerate(allow)),
    )


# ---- plans, requests, results ---------------------------------------------------------------


@dataclass
class ReplyPlan:
    """What the UI may offer for one notification. `to_web()` is the UI contract."""

    choices: list[dict] = field(default_factory=list)
    text: dict | None = None
    host: str = ""
    method: str = ""
    allowed: bool = False
    reason: str | None = None
    confirm: bool = False
    close: bool = True
    answered: dict | None = None
    error: str | None = None
    hint: Hint | None = None
    entry: Entry | None = None

    def to_web(self) -> dict:
        return {
            "choices": [dict(c) for c in self.choices],
            "text": None if self.text is None else dict(self.text),
            "host": self.host,
            "method": self.method,
            "allowed": self.allowed,
            "reason": self.reason,
            "confirm": self.confirm,
            "answered": None if self.answered is None else dict(self.answered),
            "error": self.error,
        }


@dataclass
class Prepared:
    method: str
    url: str
    body: bytes | None
    headers: dict[str, str]
    timeout: float
    max_bytes: int
    token: str | None
    label: str
    close: bool
    choice: str | None
    text: str | None


@dataclass
class SendResult:
    ok: bool  # 2xx
    status: int | None  # None on a network error
    output: str  # first 4 KB of the response, or the error
    error: str | None
    method: str
    url: str
    label: str
    close: bool
    choice: str | None
    text: str | None
    started_at: float
    finished_at: float
    duration_ms: int
    bytes_read: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the 3xx is raised as an HTTPError and reported as the result


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _read_capped(resp, cap: int, deadline: float) -> bytes:
    """Read at most `cap` bytes, within the overall deadline; the rest is dropped."""
    chunks: list[bytes] = []
    got = 0
    while got < cap:
        if time.monotonic() > deadline:
            raise TimeoutError("timed out")
        chunk = resp.read(min(8192, cap - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _describe(e: BaseException) -> str:
    if isinstance(e, urllib.error.URLError):
        e = e.reason if isinstance(e.reason, BaseException) else e
    if isinstance(e, TimeoutError):
        return "timed out"
    return f"{type(e).__name__}: {e}"


class HttpActions:
    """Plans and sends reply requests. All methods but the worker run on the GLib main thread."""

    def __init__(self, config_dir: Path | str | None, clock: Callable[[], float] = time.time):
        self._path = None if config_dir is None else Path(config_dir) / CONFIG_NAME
        self._clock = clock
        self.config = Config()
        self.error: str | None = None
        self._send_errors: dict[int, str] = {}

    # ---- config

    def load(self) -> str | None:
        """(Re)read http-actions.toml. Missing means empty. A bad file fails closed (empty
        allowlist) and its message is returned and kept in `self.error`."""
        text = None
        try:
            if self._path is not None:
                text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as e:
            return self._fail(f"{CONFIG_NAME}: {e}")
        if text is None:
            self.config, self.error = Config(), None
            return None
        try:
            self.config = parse_config(text)
        except ConfigError as e:
            return self._fail(f"{CONFIG_NAME}: {e}")
        self.error = None
        log.info(
            "%s loaded: %d allow entries, enabled=%s", CONFIG_NAME, len(self.config.entries),
            self.config.enabled,
        )  # fmt: skip
        return None

    def _fail(self, msg: str) -> str:
        self.config, self.error = Config(), msg
        log.error("%s; the allowlist is now EMPTY", msg)
        return msg

    # ---- planning

    def set_error(self, nid: int, message: str | None) -> None:
        """Remember (or clear) why the last send for `nid` failed; shown as `reply.error`.
        The map is bounded: past MAX_SEND_ERRORS entries the oldest is dropped."""
        self._send_errors.pop(nid, None)  # a re-set entry becomes the newest
        if message is None:
            return
        self._send_errors[nid] = message
        while len(self._send_errors) > MAX_SEND_ERRORS:
            del self._send_errors[next(iter(self._send_errors))]

    def forget(self, ids) -> None:
        """Drop the remembered send errors of notifications that no longer exist."""
        for nid in ids:
            self._send_errors.pop(nid, None)

    def forget_all(self) -> None:
        self._send_errors.clear()

    def sweep(self, existing: Callable[[list[int]], Iterable[int]]) -> None:
        """Drop the errors of notifications gone from the store (pruned by retention);
        `existing(ids)` returns which of `ids` are still stored."""
        if self._send_errors:
            self.forget(set(self._send_errors) - set(existing(list(self._send_errors))))

    def on_arrival(self, n) -> None:
        """Called for every recorded notification: log an invalid hint once, keep the error."""
        raw = n.hints.get(HINT_KEY)
        n.reply_error = None
        if raw is None:
            return
        try:
            parse_hint(raw)
        except HintError as e:
            n.reply_error = str(e)
            log.warning("id=%d app=%r: %s ignored: %s", n.id, n.app_name, HINT_KEY, e)

    def plan(self, n) -> ReplyPlan | None:
        """None when the notification has no hint; otherwise what may be offered."""
        raw = n.hints.get(HINT_KEY)
        if raw is None:
            return None
        answered = n.hints.get(ANSWERED_KEY)
        answered = answered if isinstance(answered, dict) else None
        try:
            hint = parse_hint(raw)
        except HintError as e:
            return ReplyPlan(reason="invalid reply hint", answered=answered, error=str(e))
        sample = normalize_url(_PLACEHOLDER_RE.sub("x", hint.url))
        entry, reason = (None, "invalid URL") if sample is None else self.config.match(
            n.app_name or "", sample
        )  # fmt: skip
        text = hint.text if self.config.text_reply else None
        allowed = entry is not None
        if allowed and not hint.choices and text is None:
            allowed, reason = False, "free-text replies are disabled"
        return ReplyPlan(
            choices=[{"id": c, "label": label} for c, label in hint.choices],
            text=text,
            host=hint.host,
            method=hint.method,
            allowed=allowed,
            reason=None if allowed else reason,
            confirm=bool(entry and entry.confirm),
            close=True if entry is None else entry.close,
            answered=answered,
            error=self._send_errors.get(n.id),
            hint=hint,
            entry=entry,
        )

    # ---- building the request

    def prepare(self, n, choice: str | None, text: str | None) -> Prepared:
        """Validate and build the request. Raises Refused(reason)."""
        plan = self.plan(n)
        if plan is None:
            raise Refused("this notification has no reply actions")
        if plan.error and plan.hint is None:
            raise Refused("the reply hint is invalid")
        if not plan.allowed:
            raise Refused(plan.reason or "not allowed")
        hint, entry = plan.hint, plan.entry
        assert hint is not None and entry is not None
        if choice is None and text is None:
            raise Refused("nothing to send")
        label = "reply"
        if choice is not None:
            labels = dict(hint.choices)
            if choice not in labels:
                raise Refused("unknown choice")
            label = labels[choice]
        if text is not None:
            if plan.text is None:
                raise Refused("free-text replies are disabled")
            if not isinstance(text, str) or not text.strip():
                raise Refused("the reply is empty")
            if len(text) > plan.text["max"]:
                raise Refused(f"the reply is longer than {plan.text['max']} characters")
        values = {"choice": choice or "", "text": text or ""}
        for v in values.values():
            if v in (".", ".."):
                raise Refused("unsafe value")

        def sub_url(m: re.Match) -> str:
            return quote(values[m.group(1)], safe="")

        url = normalize_url(_PLACEHOLDER_RE.sub(sub_url, hint.url))
        if url is None:
            raise Refused("the request URL is not acceptable")
        matched, reason = self.config.match(n.app_name or "", url)
        if matched is None:
            raise Refused(reason or "not allowed")
        entry = matched
        body = None
        if hint.body is not None:
            enc = {
                "application/json": lambda v: json.dumps(v)[1:-1],
                "application/x-www-form-urlencoded": quote_plus,
                "text/plain": lambda v: v,
            }[hint.content_type]
            body = _PLACEHOLDER_RE.sub(lambda m: enc(values[m.group(1)]), hint.body)
            body = body.encode("utf-8")
            if len(body) > MAX_BODY:
                raise Refused("the request body is too large")
        headers = {"User-Agent": "cosmic-notification-center", "Accept": "*/*"}
        if body is not None:
            headers["Content-Type"] = hint.content_type
        headers.update(dict(entry.headers))
        token = None
        if entry.token_file:
            try:
                token = Path(entry.token_file).expanduser().read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                raise Refused("the token file cannot be read") from None
            if not token or _CTRL_RE.search(token):
                raise Refused("the token file is empty or malformed")
            headers["Authorization"] = f"Bearer {token}"
        return Prepared(
            method=hint.method,
            url=url,
            body=body,
            headers=headers,
            timeout=self.config.timeout_s,
            max_bytes=self.config.max_response_bytes,
            token=token,
            label=label,
            close=entry.close,
            choice=choice,
            text=text,
        )

    def check(self, n, choice: str | None = None, text: str | None = None) -> str | None:
        """Dry run of `send`: the refusal reason, or None if it would be sent."""
        try:
            self.prepare(n, choice, text)
        except Refused as e:
            return str(e)
        except Exception:
            log.exception("reply check failed id=%s", getattr(n, "id", "?"))
            return "internal error"
        return None

    # ---- sending

    def send(
        self,
        n,
        choice: str | None = None,
        text: str | None = None,
        on_done: Callable[[SendResult], None] | None = None,
    ) -> str | None:
        """Start the request in a worker thread. Returns None when started (then `on_done`
        gets a SendResult on the GLib main thread), or the refusal reason (`on_done` is not
        called). Never raises."""
        try:
            prep = self.prepare(n, choice, text)
        except Refused as e:
            return str(e)
        except Exception:
            log.exception("reply prepare failed id=%s", getattr(n, "id", "?"))
            return "internal error"
        try:
            started = self._clock()
            threading.Thread(
                target=self._work, args=(prep, started, on_done), daemon=True, name="http-action"
            ).start()
        except Exception:
            log.exception("reply thread failed")
            return "internal error"
        log.info(
            "reply %s %s id=%s", prep.method, display_host(prep.url), getattr(n, "id", "?")
        )  # path and query are never logged: they carry the user's text
        return None

    def _work(self, prep: Prepared, started: float, on_done) -> None:
        t0 = time.monotonic()
        status: int | None = None
        output = ""
        error: str | None = None
        read = 0
        try:
            req = urllib.request.Request(
                prep.url, data=prep.body, method=prep.method, headers=prep.headers
            )
            try:
                resp = _opener().open(req, timeout=prep.timeout)
            except urllib.error.HTTPError as e:  # also every 3xx: redirects are not followed
                resp = e
            try:
                status = int(resp.status if hasattr(resp, "status") else resp.code)
                data = _read_capped(resp, prep.max_bytes, t0 + prep.timeout)
                read = len(data)
                output = data[:OUTPUT_TAIL].decode("utf-8", errors="replace")
            finally:
                resp.close()
        except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as e:
            error = _describe(e)
            output = output or error
        except Exception as e:  # never let a worker die silently
            log.exception("reply worker failed")
            error = _describe(e)
            output = output or error
        if prep.token:
            output = output.replace(prep.token, "[redacted]")
            error = error.replace(prep.token, "[redacted]") if error else error
        result = SendResult(
            ok=status is not None and 200 <= status < 300 and error is None,
            status=status,
            output=output,
            error=error,
            method=prep.method,
            url=prep.url,
            label=prep.label,
            close=prep.close,
            choice=prep.choice,
            text=prep.text,
            started_at=started,
            finished_at=self._clock(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            bytes_read=read,
        )
        log.info(
            "reply %s %s -> %s%s",
            prep.method, display_host(prep.url), status if status is not None else "error",
            f" ({error})" if error else "",
        )  # fmt: skip

        def finish() -> bool:
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    log.exception("reply on_done failed")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(finish)


# ---- hook for model.to_web ------------------------------------------------------------------

_active: HttpActions | None = None
_EMPTY = HttpActions(None)


def set_active(actions: HttpActions | None) -> None:
    """The instance `model.to_web` asks for the `reply` payload (the server sets it)."""
    global _active
    _active = actions


def reply_for_web(n) -> dict | None:
    """The `reply` key of `to_web(n)`: None without a hint. Never raises."""
    try:
        plan = (_active or _EMPTY).plan(n)
    except Exception:
        log.exception("reply plan failed id=%s", getattr(n, "id", "?"))
        return None
    return None if plan is None else plan.to_web()


def answered_record(choice: str | None, text: str | None, at: float, status: int) -> dict:
    rec: dict[str, Any] = {}
    if choice is not None:
        rec["choice"] = choice
    if text is not None:
        rec["text"] = text[:ANSWERED_TEXT]
    rec["at"] = at
    rec["status"] = status
    return rec


__all__ = [
    "ANSWERED_KEY",
    "HINT_KEY",
    "HttpActions",
    "ReplyPlan",
    "SendResult",
    "answered_record",
    "normalize_url",
    "parse_config",
    "parse_hint",
    "reply_for_web",
    "set_active",
]
