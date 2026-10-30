# Reply actions (`x-nc-actions`)

A sender can attach choices (and/or a free-text box) to a notification and name one HTTP
endpoint. Clicking a choice makes the **daemon** send that request. Example: reply `yes` / `no`
to `POST http://127.0.0.1:4320/endpoint/reply/thread-22/{choice}`.

The feature is **inert until the user configures it**: with no `http-actions.toml` the allowlist
is empty and nothing is ever sent. Code: `notifyd/http_actions.py`; server side in
`notifyd/dbus_server.py` (`reply_choice`, `reply_text`, `confirm_reply`, `cancel_reply`).

## The hint

A hint named `x-nc-actions`: a string holding JSON, at most 8 KiB. Unknown keys, a bad id or an
unknown placeholder make the whole hint invalid.

```json
{"choices": ["yes", "no"],
 "text": {"placeholder": "Reply…", "max": 2000},
 "request": {"method": "POST",
             "url": "http://127.0.0.1:4320/endpoint/reply/thread-22/{choice}",
             "body": "{\"answer\": \"{choice}\", \"text\": \"{text}\"}",
             "content_type": "application/json"}}
```

- `choices`: optional, at most 6. An item is `"yes"` or `{"id": "yes", "label": "Yes"}`. An id
  matches `[A-Za-z0-9_.-]{1,40}` and is not `.` or `..`; ids are unique; a label is at most 40
  characters without control characters (default: the id).
- `text`: optional. `true` or `{"placeholder": "...", "max": 1..2000}` (default `Reply…`, 2000).
  At least one of `choices` and `text` is required.
- `request.method`: `POST` (default), `GET`, `PUT`, `PATCH`. `url` is absolute `http`/`https`,
  at most 2000 characters. `body` is an optional template (not with `GET`), at most 4096
  characters. `content_type` is `application/json` (default), `text/plain` or
  `application/x-www-form-urlencoded`.
- Placeholders are `{choice}` and `{text}` only (an identifier in braces; other braces, such as
  the ones in a JSON body, are literal). Any other `{name}` makes the hint invalid, as does a
  placeholder in the scheme/host/port part, `choices` without `{choice}` in the request, or `text`
  without `{text}`. A placeholder the reply does not use (`{text}` on a choice click) becomes empty.
- A sender-supplied `headers` key is ignored. A sender cannot set a header, a token or a redirect.
- An invalid hint is ignored: a WARNING is logged, `Notification.reply_error` is set (runtime
  only) and the web payload carries `reply.error`; the notification is otherwise normal.

### Substitution

| Where | Encoding |
|---|---|
| URL (path or query) | percent-encoded as one segment/value (`quote(safe="")`): `/ ? # & = %` and spaces are encoded, so `%2f` arrives as `%252f`. A value that is exactly `.` or `..` is refused. |
| body, JSON | JSON string escaping (quotes, backslashes, CR/LF, control characters) |
| body, form | `application/x-www-form-urlencoded` encoding |
| body, text/plain | raw (UTF-8) |

One pass: a text that contains `{choice}` is not expanded again.

## Security model

The notification is untrusted input; the request goes out with the user's network position and
possibly a bearer token. So:

1. **The user decides what can be reached**, in `http-actions.toml`. The hint only picks among
   the allowed requests. Nothing is allowed by default.
2. **The URL is normalized, then checked, then sent as that same string.** Normalization lowercases
   scheme and host, drops the default port, resolves `.`/`..` segments (also `%2e%2e`), and rejects
   userinfo (`user@`), fragments, spaces, backslashes and control characters. The prefix must match
   the normalized URL on a path-segment boundary, and the host exactly (`127.0.0.1.evil.com` and
   `127.0.0.10` do not match `127.0.0.1`). The URL after substitution is normalized and checked
   again at send time, and re-checked when a confirmation is accepted.
3. **Values cannot change the request's structure** (encoding above).
4. **The token never comes from the notification.** `token_file` is read at send time, sent as
   `Authorization: Bearer ...`, never logged, never put in the web payload and redacted if the
   response echoes it. Sender headers are ignored; only the allowlist's `headers` are added.
5. **No redirects** are followed (a 3xx is the result), no environment proxies are used.
6. **Bounded**: timeout (`timeout_s`), response read capped at `max_response_bytes`, 4 KB of it
   stored; one send at a time per notification; an answered notification takes no more replies.
7. **The UI never sees the URL path, body or token**, only `host`, `method` and what is allowed.
8. **A broken `http-actions.toml` fails closed** (empty allowlist). A sender cannot forge the
   answered state: `x-nc-answered` is dropped from incoming hints.
9. **Optional confirmation** (`confirm = true` per endpoint): the daemon does not send until the
   UI calls `confirm_reply(id, token)`; the pending token lives 60 s, is single use and is bound
   to the notification. The bus only ever returns `pending_confirm` for such an endpoint.

Not covered: a local process that can already call the session bus can press any reply itself;
and an allowlisted endpoint is trusted to treat the (encoded) values as data.

## `http-actions.toml`

`~/.config/net.corbinmeier.notifications/http-actions.toml`, hand-edited, hot-reloaded (300 ms
debounce) like `rules.toml`; a missing file means empty. A bad file logs an error, signals
`RulesReloaded(false, ...)` and leaves the allowlist empty until fixed. Unknown keys are errors.

```toml
enabled = true            # master switch (default true; entries are the real consent)
text_reply = true         # false: free-text replies are off everywhere (choices still work)
timeout_s = 10            # 0.1 .. 120
max_response_bytes = 65536  # 1 .. 1048576

[[allow]]
app = "Example App"                 # exact, case-insensitive match on the app name
prefixes = ["http://127.0.0.1:4320/"]  # normalized URL must start with one; http/https only
confirm = false                        # true: the UI must confirm each send (default false)
close = true                           # false: keep the notification open after a 2xx
enabled = true                         # false: switch this entry off
token_file = "~/.config/net.corbinmeier.notifications/actions.token"  # optional bearer token
headers = { "X-Source" = "notifyd" }   # optional static headers (not Host, Content-*, ...)
```

Defaults when the user has not decided: allowlist empty (inert), free-text on, confirm off
globally with `confirm = true` per endpoint, close as DISMISSED after a 2xx, no changes to the sending app.

## What happens on a click

`NotificationServer.reply_choice(id, choice_id)` / `reply_text(id, text)` return
`{"status": "sent" | "pending_confirm" | "refused", "token"?, "expires_in"?, "reason"?}`
(`"sent"` = request started). `confirm_reply(id, token)` returns the same; `cancel_reply` returns
`{"status": "cancelled"}`. When the request finishes (worker thread, result handed to the GLib main
thread) the daemon:

1. records a row in `runs`: `trigger = "button"`, `button_label` = the choice label or `reply`,
   `cmd` = JSON `{"http": {"method", "url"}}` (no body, no token), `exit_code` = HTTP status
   (NULL on a network error), `output_tail` = first 4 KB of the response or the error;
2. on a 2xx: stores `x-nc-answered` = `{"choice"?, "text"? (first 200 characters), "at",
   "status"}` **inside the existing `hints` JSON** (no schema change), and closes the notification
   as DISMISSED unless the endpoint says `close = false`;
3. on a failure: nothing is answered or closed; `reply.error` shows the reason and a retry works;
4. calls the changed observers (shade) and `center.refresh()`.

### Contract for the UI hosts

`to_web(n)["reply"]` is `null` without a hint, otherwise
`{"choices": [{"id", "label"}], "text": {"placeholder", "max"} | null, "host", "method",
"allowed", "reason", "confirm", "answered": {...} | null, "error"}`.
Toast, shade and center hosts get the callbacks by attribute from `build_daemon`:
`on_reply_choice(id, choice_id)`, `on_reply_text(id, text)`, `on_reply_confirm(id, token)`,
`on_reply_cancel(id, token)`. The `runs` row's `cmd` is an object for these rows, not a string
or list.

### Bus and CLI

Control interface: `ReplyChoice(u id, s choice) -> s json`, `ReplyText(u id, s text) -> s json`.
CLI clients: `bin/notifyd --reply-choice ID CHOICE`, `bin/notifyd --reply-text ID TEXT` print the
JSON and exit 0 only for `sent`. They do not confirm; a `confirm = true` endpoint needs the UI.

## Sending one from the shell

```sh
notify-send -a "Example App" "Thread 22: ship it?" "Reply from here" \
  --hint=string:x-nc-actions:'{"choices":[{"id":"yes","label":"Yes"},"no"],"request":{"method":"POST","url":"http://127.0.0.1:4320/endpoint/reply/thread-22/{choice}"}}'
```

With a free-text box as well:

```sh
notify-send -a "Example App" "Thread 22" "Anything to add?" \
  --hint=string:x-nc-actions:'{"choices":["yes","no"],"text":true,"request":{"url":"http://127.0.0.1:4320/endpoint/reply/thread-22","body":"{\"answer\":\"{choice}\",\"text\":\"{text}\"}"}}'
```

Both are inert until `http-actions.toml` has an `[[allow]]` for `app = "Example App"`.

## Adding buttons from your own app (guidance only, nothing here changes the app)

Any app can add buttons to the notification system: it already notifies through
`org.freedesktop.Notifications`, and to make a notification answerable it adds the hint when it posts,
e.g. in Python:

```python
hint = {
    "choices": [{"id": "approve", "label": "Approve"}, {"id": "decline", "label": "Decline"}],
    "text": {"placeholder": "Reply to the thread…", "max": 2000},
    "request": {
        "method": "POST",
        "url": f"http://127.0.0.1:4320/endpoint/reply/{thread_id}",
        "body": '{"answer": "{choice}", "text": "{text}"}',
    },
}
notify(
    app_name="Example App", summary=title, body=body, hints={"x-nc-actions": json.dumps(hint)}
)
```

The receiving endpoint should authenticate with the bearer token (put it in the user's `actions.token`,
referenced by `token_file`), treat `answer` and `text` as data, be idempotent per thread (a retry
after a timeout can repeat a request that did arrive), and return 2xx only when the reply was
recorded: a 2xx marks the notification answered and closes it.
