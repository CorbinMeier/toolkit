"""Two-file configuration: tracked definitions + untracked selections (#74).

`config.json` is **definitions** -- location, ports, thresholds, patterns,
panel tuning. It is tracked, reviewed in a diff, and from here on never
written by the running app.

`data/config.local.json` is **this machine's selections** -- the theme picked
from the context menu (#66), the units and log source saved from the Control
Backend (#30). Untracked, and the only file any write path touches.

The split exists because writing a tracked file at runtime made a selection
indistinguishable from a deliberate change to a shipped default: every theme
pick left `M config.json` in the working tree, which in turn blocked
`git merge --ff-only` and trained the merge protocol's dirty-tree guard rail
to be waved through.

**The override file holds only the overrides, never a full copy.** A full
snapshot would freeze the defaults at the moment it was written, so a later
edit to `config.json` would silently stop taking effect -- the failure mode
would be a config change that appears to do nothing, which is far worse than
the problem being solved.

Writes land in `data/` deliberately: both units grant it **directory-level**
`ReadWritePaths`, unlike the single-file grant on `config.json` that made
tmp-then-rename impossible in #69/#70. Creating a sibling temp file here is
permitted, so this module keeps the crash-safe write those two had to give up.
"""

from __future__ import annotations

import json
from pathlib import Path

LOCAL_NAME = "config.local.json"


def local_path(root: Path) -> Path:
    return root / "data" / LOCAL_NAME


def deep_merge(base: dict, overrides: dict) -> dict:
    """`overrides` onto a copy of `base`, recursing into nested dicts.

    Only dict-vs-dict recurses; a list or scalar override replaces wholesale.
    That is deliberate for the shapes here -- `logs.patterns` is an ordered
    list the user means to replace entirely, not element-wise merge into.
    """
    out = dict(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_local(root: Path) -> dict:
    """This machine's selections, or {} when there are none.

    Never raises: a corrupt or unreadable override file must not take the
    whole dashboard down, since the tracked defaults alone are a complete,
    working configuration. A bad file degrades to "no selections applied".
    """
    path = local_path(root)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load(root: Path) -> dict:
    """The effective config: tracked definitions with local selections on top."""
    with (root / "config.json").open() as fh:
        base = json.load(fh)
    return deep_merge(base, load_local(root))


def save_local(root: Path, overrides: dict) -> None:
    """Replace the override file. `config.json` is never touched.

    tmp-then-rename is available again here, unlike #69/#70's in-place write:
    the sandbox grants `data/` at directory level, so creating the temp file
    is permitted and the rename stays within one bind mount (no EXDEV).

    The trailing newline is not incidental -- `json.dumps` omits it, and its
    absence is what left a permanently dirty tracked file in #73.
    """
    path = local_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overrides, indent=2) + "\n")
    tmp.replace(path)


def apply_patch(root: Path, patch: dict) -> dict:
    """Merge `patch` into the stored selections and persist.

    Returns the new effective config, so a caller can answer with what the
    running app will actually use rather than with what it just wrote.

    The patch must already be whitelist-filtered by the caller -- this module
    decides *where* a setting is stored, never *whether* it may be set.
    """
    merged = deep_merge(load_local(root), patch)
    save_local(root, merged)
    return load(root)
