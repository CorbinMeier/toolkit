"""Image previews, the pure part: which image a notification is about, and whether it is safe.

Pure and free of GTK (the clipboard fallback and the WebKit side live in `image_ui`).

Where the image comes from (first one that validates wins):
  1. the `image-path` / `image_path` hint: a plain absolute path or a `file://` URI
  2. the body, when it is (once stripped) exactly one absolute path or `file://` URI; this is
     what COSMIC Screenshot sends ("Screenshot saved to:" + the path, no image hint)
Image bytes in hints (`image-data`) are never used and never stored; the DB holds no image.

A path is an image only when ALL of these hold (`check`):
  - after `realpath` it is a regular file (a symlink to a non-image fails the next two steps)
  - its extension is one of EXTENSIONS and its magic bytes say the same format
  - it is at most MAX_BYTES
Anything else (missing, deleted later, a directory, a FIFO, a text file named .png) gives no
preview and no error. SVG is not an accepted format: it can carry script.

`read_image` re-checks while reading, so what is served is exactly what was verified:
bytes read from one open descriptor, size and magic taken from those bytes.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_BYTES = 25 * 1024 * 1024
MAX_PATH = 4096
EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
HEAD = 16  # bytes `sniff` needs
COPY_TIMEOUT_S = 3.0
HINT_KEYS = ("image-path", "image_path")


@dataclass(frozen=True)
class ImageRef:
    path: str  # the realpath: the only thing ever read
    shown: str  # the path as the sender gave it (what "Copy path" copies)
    mime: str
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def sniff(head: bytes) -> str | None:
    """The MIME type the leading bytes say, or None when they are none of our formats."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return None


def to_path(raw: Any) -> str | None:
    """A plain absolute path or a `file://` URI (no host, or localhost) -> the path, else None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > MAX_PATH or any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return None
    if s.startswith("file://"):
        parts = urlsplit(s)
        if parts.netloc not in ("", "localhost") or parts.query or parts.fragment:
            return None
        s = unquote(parts.path)
        if "\x00" in s or any(ord(c) < 0x20 for c in s):
            return None
    return s if s.startswith("/") else None


def candidates(hints: dict[str, Any], body: str) -> list[str]:
    """Paths worth checking, best first: the image-path hint, then a body that is one path."""
    out = []
    for key in HINT_KEYS:
        if (p := to_path(hints.get(key))) is not None:
            out.append(p)
    if (p := to_path(body)) is not None:
        out.append(p)
    return out


def _open(path: str) -> int:
    """A read-only, non-blocking descriptor that never follows a final symlink."""
    return os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)


def check(shown: str) -> ImageRef | None:
    """Validate one path (see the module doc); None for anything that is not a safe image."""
    try:
        real = os.path.realpath(shown)
        mime = EXTENSIONS.get(os.path.splitext(real)[1].lower())
        if mime is None:
            return None
        fd = _open(real)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_BYTES:
                return None
            if sniff(os.read(fd, HEAD)) != mime:
                return None
        finally:
            os.close(fd)
        return ImageRef(real, shown, mime, st.st_size, st.st_mtime_ns)
    except (OSError, ValueError):
        return None


def resolve(hints: dict[str, Any], body: str) -> ImageRef | None:
    """The image of a notification (its hints and raw body), or None."""
    for path in candidates(hints or {}, body or ""):
        if (ref := check(path)) is not None:
            return ref
    return None


def read_image(ref: ImageRef) -> bytes | None:
    """The bytes of `ref`, re-verified (regular file, size, magic) while reading; else None."""
    try:
        fd = _open(ref.path)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_BYTES:
                return None
            chunks, left = [], MAX_BYTES + 1
            while left > 0:
                chunk = os.read(fd, min(1 << 20, left))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            os.close(fd)
    except OSError:
        return None
    data = b"".join(chunks)
    if not data or len(data) > MAX_BYTES or sniff(data[:HEAD]) != ref.mime:
        return None
    return data


def copy_argv(mime: str, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """`wl-copy --type <mime>` (the file goes on stdin, never on the command line), or None when
    wl-copy is not installed."""
    exe = which("wl-copy")
    return [exe, "--type", mime] if exe else None


def copy_image(
    ref: ImageRef,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    which: Callable[[str], str | None] = shutil.which,
    fallback: Callable[[str], bool] | None = None,
) -> bool:
    """Put the image on the clipboard: wl-copy with the file as stdin (no shell). `fallback(path)`
    (the GTK clipboard) runs when wl-copy is missing or fails. True when something took it."""
    argv = copy_argv(ref.mime, which)
    if argv is not None:
        try:
            fd = _open(ref.path)
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            with os.fdopen(fd, "rb", closefd=False) as stdin:
                proc = popen(
                    argv,
                    stdin=stdin,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                try:
                    if proc.wait(timeout=COPY_TIMEOUT_S) == 0:
                        return True
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except OSError:
            pass
        finally:
            os.close(fd)
    return bool(fallback and fallback(ref.path))
