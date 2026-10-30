"""GitHub per-repository issue stats for the Projects panel (#62).

Answers one question for the wallpaper: *across every project I own, what is
waiting on me?* Three counts per repo -- `ai-blocked` open issues (needs a
human: credentials, sign-off, a decision), the remaining open issues
(actionable right now), and closed issues (progress made).

Cost. Every owner is folded into a **single** aliased GraphQL document, with
`totalCount`-only sub-selections so no issue node is ever transferred. That
measures at 1 rate-limit point total against a 5000/hr budget, versus the
~300 REST calls a per-repo fan-out would need. Issue bodies are never
fetched; this module only ever sees numbers.

Pagination is deliberately absent. `orderBy: PUSHED_AT DESC` plus the
`stale_after_days` cutoff means every repo that could survive the filter is
necessarily in the first 100 -- a repo pushed less recently than the 100th
is, by definition, older than one already excluded. `truncated` is still
reported so the assumption fails loudly rather than silently dropping work
if an owner ever exceeds that.

`gh` is a hard dependency but a soft failure: no binary, no auth, or no
network degrades to `available: False` and the panel says so, matching how
`sysinfo.now_playing()` treats a missing `playerctl`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime

# Only the counts, never the issues -- `totalCount` on a connection with no
# `nodes` selection is what keeps this a 1-point query.
_FRAGMENT = """
fragment C on Repository {
  nameWithOwner
  pushedAt
  isArchived
  open:    issues(states: OPEN) { totalCount }
  closed:  issues(states: CLOSED) { totalCount }
  blocked: issues(states: OPEN, labels: ["ai-blocked"]) { totalCount }
}
"""

_REPOS = ("repositories(first: 100, isArchived: false%s, "
          "orderBy: {field: PUSHED_AT, direction: DESC}) "
          "{ pageInfo { hasNextPage } nodes { ...C } }")

# A user's own repos need ownerAffiliations to exclude the (potentially
# hundreds of) repos they merely collaborate on; an organization connection
# has no such argument and errors if given one.
_USER_REPOS = _REPOS % ", ownerAffiliations: [OWNER]"
_ORG_REPOS = _REPOS % ""

DEFAULT_TIMEOUT = 25


class GhUnavailableError(RuntimeError):
    """`gh` is missing, unauthenticated, or otherwise unusable."""


def _run_gh(args: list[str], timeout: int) -> str:
    if not shutil.which("gh"):
        raise GhUnavailableError("gh is not installed")
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhUnavailableError(f"gh failed: {exc}") from exc
    if proc.returncode != 0:
        raise GhUnavailableError((proc.stderr or "gh exited non-zero").strip()[:300])
    return proc.stdout


def discover_owners(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Every owner whose issues belong on the card: the authenticated user
    plus each organization they belong to.

    Returned as `{"login", "kind"}` because the GraphQL root field differs --
    `user(login:)` vs `organization(login:)` -- and picking the wrong one
    errors rather than falling back.
    """
    login = _run_gh(["api", "user", "--jq", ".login"], timeout).strip()
    if not login:
        raise GhUnavailableError("could not resolve the authenticated user")
    owners = [{"login": login, "kind": "user"}]
    orgs = _run_gh(["api", "user/orgs", "--jq", ".[].login"], timeout)
    owners += [{"login": o.strip(), "kind": "org"}
               for o in orgs.splitlines() if o.strip()]
    return owners


def build_query(owners: list[dict]) -> str:
    """One document, one alias per owner. Aliases are positional (`o0`, `o1`)
    rather than derived from the login, since a login may contain `-`/`.`
    which are not legal in a GraphQL alias."""
    parts = []
    for i, owner in enumerate(owners):
        root = "user" if owner["kind"] == "user" else "organization"
        repos = _USER_REPOS if owner["kind"] == "user" else _ORG_REPOS
        login = json.dumps(owner["login"])
        parts.append(f'  o{i}: {root}(login: {login}) {{ {repos} }}')
    return f"{_FRAGMENT}\n{{\n" + "\n".join(parts) + "\n}\n"


def _age_days(pushed_at: str, now: float) -> float | None:
    try:
        ts = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC).timestamp()
    except (ValueError, TypeError):
        return None
    return (now - ts) / 86400.0


def shape(raw: dict, owners: list[dict], stale_after_days: int = 90,
          now: float | None = None) -> dict:
    """Flatten the GraphQL payload into the panel's row list.

    Filters to repos with at least one OPEN issue -- a repo with nothing open
    is not a reminder of anything -- then drops those untouched for longer
    than `stale_after_days`, which is what keeps long-dormant repos from
    permanently occupying the card. `stale_after_days <= 0` disables the
    cutoff.

    Sort is blocked desc, then actionable desc, then most-recently-pushed.
    Recency alone was the first instinct and the real data disproved it: it
    floats a 1-issue repo above one with 37 open issues.
    """
    now = time.time() if now is None else now
    data = raw.get("data") or {}
    rows: list[dict] = []
    truncated: list[str] = []
    missing: list[str] = []

    for i, owner in enumerate(owners):
        node = data.get(f"o{i}")
        # A null owner means the token can't see it (org SSO, removed
        # membership). Report it rather than silently under-counting.
        if not node:
            missing.append(owner["login"])
            continue
        repos = node.get("repositories") or {}
        if (repos.get("pageInfo") or {}).get("hasNextPage"):
            truncated.append(owner["login"])
        for repo in repos.get("nodes") or []:
            if not repo:
                continue
            open_n = (repo.get("open") or {}).get("totalCount", 0)
            if open_n <= 0:
                continue
            age = _age_days(repo.get("pushedAt") or "", now)
            if stale_after_days > 0 and age is not None and age > stale_after_days:
                continue
            blocked = (repo.get("blocked") or {}).get("totalCount", 0)
            name = repo.get("nameWithOwner") or "?"
            rows.append({
                "repo": name,
                "owner": owner["login"],
                # The card has room for the repo, not the owner prefix.
                "short": name.split("/", 1)[-1],
                "pushed_at": repo.get("pushedAt"),
                "age_days": None if age is None else round(age, 1),
                "blocked": blocked,
                # What the user can actually pick up right now. The card's
                # green number is open-minus-blocked, not open.
                "actionable": max(0, open_n - blocked),
                "open": open_n,
                "closed": (repo.get("closed") or {}).get("totalCount", 0),
            })

    # Two passes rather than one compound key: the numeric keys sort
    # descending via negation, but `pushed_at` is an ISO string and can't be
    # negated. Python's sort is stable, so sorting by the weakest key first
    # and the strongest last yields blocked desc > actionable desc > newest.
    rows.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    rows.sort(key=lambda r: (-r["blocked"], -r["actionable"]))
    return {
        "rows": rows,
        "totals": {
            "blocked": sum(r["blocked"] for r in rows),
            "actionable": sum(r["actionable"] for r in rows),
            "closed": sum(r["closed"] for r in rows),
            "repos": len(rows),
        },
        "owners": [o["login"] for o in owners],
        "truncated": truncated,
        "missing": missing,
        "stale_after_days": stale_after_days,
        "available": True,
    }


def fetch(cfg: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Full refresh: discover owners, run the one query, shape the result.

    Raises GhUnavailableError on any `gh`-level failure so the caller can decide
    to serve a stale cache instead -- an old count beats an error on a
    wallpaper, same posture as the weather fetch.
    """
    pcfg = (cfg or {}).get("projects") or {}
    stale_after = int(pcfg.get("stale_after_days", 90))
    # Owners are always discovered, never listed in config: the whole point
    # is "every org I'm in", and a hand-maintained list silently goes stale
    # the moment a new org is joined. exclude_owners trims the result, which
    # is the only direction that can't drop work on the floor.
    skip = {s.lower() for s in pcfg.get("exclude_owners") or []}
    owners = [o for o in discover_owners(timeout)
              if o["login"].lower() not in skip]
    if not owners:
        raise GhUnavailableError("no owners to query")

    out = _run_gh(["api", "graphql", "-f", f"query={build_query(owners)}"],
                  timeout)
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhUnavailableError(f"unparseable gh response: {exc}") from exc
    # Partial success is normal here (one inaccessible org among several):
    # GraphQL returns both `errors` and whatever `data` it could resolve, and
    # shape() already records unreachable owners under `missing`.
    if raw.get("errors") and not raw.get("data"):
        first = (raw["errors"][0] or {}).get("message", "graphql error")
        raise GhUnavailableError(str(first)[:300])
    shaped = shape(raw, owners, stale_after)
    shaped["fetched_at"] = time.time()
    return shaped
