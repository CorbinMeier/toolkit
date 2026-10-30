"""Image previews, the UI side: what the pages may see, ask for and fetch.

Shared by the toast host, the shade bridge and the center bridge (they hold one `ImageService`
in an `images` attribute; None means previews are off). Free of GTK at import time.

Payload: `ImageService.info(n)` is the notification's `image` for the page, or None:
  {uri: "nc-image://img/<id>/<version>", mime, name}
The page never gets a file path, only that URI and the file's base name.

Serving: the pages' CSP allows `img-src file: nc-image:` and nothing else new. The scheme is
registered once on the WebKit default context (`register_scheme`; all three WebViews are built
on it). A request is `nc-image://img/<id>/<version>`; `ImageService.serve` ignores `<version>`
(cache busting only), looks the notification up, resolves ITS image again with
`images.resolve` (same checks as the payload: regular file, extension, magic, size) and answers
with the bytes and the MIME type that were verified, or an error. A page cannot name a path.
The scheme is not registered as local, secure or CORS-enabled: it is a plain, display-only
resource of the page, and no `file://` access to anything else is granted.

Messages from a page (untrusted; same shape on all three pages):
  {type: "image_copy", id}       copy the image: `wl-copy --type <mime>`, else the GTK clipboard
  {type: "image_copy_path", id}  copy the path as text (the path the sender gave)
  {type: "image_open", id}       open the file in the default viewer
`id` is an int of a notification that has a previewable image now; anything else is dropped.
The outcome goes back as window.nc.imageResult({id, kind, ok}).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from notifyd import images
from notifyd.images import ImageRef

log = logging.getLogger("notifyd.images")

SCHEME = "nc-image"
KINDS = ("image_copy", "image_copy_path", "image_open")
ID_MAX = 2**32 - 1
URI_RE = re.compile(rf"{SCHEME}://img/(\d{{1,10}})/[0-9a-f-]{{1,40}}")
_registered: set[int] = set()  # id() of the contexts that already have the scheme


def image_uri(id: int, ref: ImageRef) -> str:
    """The page-facing URL; the trailing version changes with the file so WebKit refetches."""
    return f"{SCHEME}://img/{id}/{ref.mtime_ns:x}-{ref.size:x}"


def parse_uri(uri: Any) -> int | None:
    """The notification id of a well-formed `nc-image://img/<id>/<version>` URL, else None."""
    if not isinstance(uri, str) or len(uri) > 100 or URI_RE.fullmatch(uri) is None:
        return None
    id = int(URI_RE.fullmatch(uri).group(1))  # type: ignore[union-attr]
    return id if id <= ID_MAX else None


def _valid_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= ID_MAX


def file_uri(path: str) -> str:
    return "file://" + quote(path, safe="/")


class ImageService:
    """Looks images up per notification and does the three things a page may ask for.

    `get(id) -> Notification | None` (the store); `enabled()` is `[images] preview`, read on
    every call so a saved change applies at once; the rest are the daemon's own clipboard and
    opener (`copy_text(str)`, `open_uri(uri)`) and test seams for the process spawn.
    """

    def __init__(
        self,
        get: Callable[[int], Any],
        *,
        enabled: Callable[[], bool] = lambda: True,
        copy_text: Callable[[str], None] | None = None,
        open_uri: Callable[[str], None] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        gtk_copy: Callable[[str], bool] | None = None,
    ) -> None:
        self.get = get
        self.enabled = enabled
        self.copy_text = copy_text
        self.open_uri = open_uri
        self.popen = popen
        self.which = which
        self.gtk_copy = gtk_copy_image if gtk_copy is None else gtk_copy

    def ref_for(self, n: Any) -> ImageRef | None:
        if n is None or not self.enabled():
            return None
        return images.resolve(n.hints, n.body)

    def info(self, n: Any) -> dict[str, str] | None:
        ref = self.ref_for(n)
        if ref is None:
            return None
        return {"uri": image_uri(n.id, ref), "mime": ref.mime, "name": ref.name}

    def serve(self, uri: Any) -> tuple[bytes, str] | None:
        """(bytes, mime) for a request URL, or None (the request is then answered with an error)."""
        id = parse_uri(uri)
        if id is None:
            return None
        ref = self.ref_for(self.get(id))
        data = images.read_image(ref) if ref is not None else None
        return None if data is None else (data, ref.mime)  # type: ignore[union-attr]

    def handle(self, msg: dict, run_js: Callable[[str], None]) -> None:
        """One page message of KINDS; answers with imageResult. Junk is dropped silently."""
        kind, id = msg.get("type"), msg.get("id")
        if kind not in KINDS or not _valid_id(id):
            return
        ref = self.ref_for(self.get(id))
        ok = ref is not None and self._do(kind, ref)
        run_js(f"window.nc.imageResult({json.dumps({'id': id, 'kind': kind, 'ok': ok})})")

    def _do(self, kind: str, ref: ImageRef) -> bool:
        try:
            if kind == "image_copy":
                return images.copy_image(
                    ref, popen=self.popen, which=self.which, fallback=self.gtk_copy
                )
            if kind == "image_copy_path":
                if self.copy_text is None:
                    return False
                self.copy_text(ref.shown)
                return True
            if self.open_uri is None:
                return False
            self.open_uri(file_uri(ref.path))
            return True
        except Exception:
            log.exception("%s failed", kind)
            return False


def gtk_copy_image(path: str) -> bool:
    """The fallback when wl-copy is missing: the image as a pixbuf on the GTK clipboard."""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GdkPixbuf, Gtk

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_image(pixbuf)
        cb.store()
        return True
    except Exception:
        log.exception("GTK clipboard image copy failed")
        return False


def register_scheme(service: ImageService, context: Any = None) -> bool:
    """Register `nc-image:` on the WebKit context (default: the default one, which every view
    here uses). Once per context; True when this call registered it."""
    if context is None:
        from gi.repository import WebKit2

        context = WebKit2.WebContext.get_default()
    if id(context) in _registered:
        return False
    _registered.add(id(context))
    context.register_uri_scheme(SCHEME, _on_request, service)
    return True


def _on_request(request: Any, service: ImageService) -> None:
    """URISchemeRequest handler (main thread): bytes with the verified MIME, or an error."""
    from gi.repository import Gio, GLib

    try:
        out = service.serve(request.get_uri())
    except Exception:
        log.exception("serving %s failed", request.get_uri())
        out = None
    if out is None:
        log.debug("nc-image request refused: %s", request.get_uri())
        err = GLib.Error.new_literal(Gio.io_error_quark(), "no image", Gio.IOErrorEnum.NOT_FOUND)
        request.finish_error(err)
        return
    data, mime = out
    log.debug("nc-image request served: %s (%d bytes, %s)", request.get_uri(), len(data), mime)
    request.finish(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data)), len(data), mime)
