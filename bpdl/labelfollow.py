"""Recognise a label's folder again after it has been moved or renamed.

A label is downloaded once into a download folder, then the folder gets moved
into the library — usually to a different drive, and usually renamed on the way
(`Nukleuz [2026-08-26]` becomes `NUKLEUZ`). A watch entry that remembers only the
path it first wrote to is stale from that moment on, and the next unattended
sweep would quietly re-create the old path and file new releases into a folder
nobody ever looks at again.

So the folder carries its own identity instead: a small hidden JSON file written
inside it. Move it, rename it, put it on another disk — the marker travels with
the contents, and the label is found again by reading markers rather than by
guessing from folder names.

Guessing from names was the obvious alternative and does not work on a real
library: a rename defeats it outright, and labels here already resolve to two
genre folders at once (5th Gear is in both GABBER and MULTI_GENRE). A wrong guess
files a download into someone else's folder, unattended, six hours later. The
marker is the only answer that is right by construction.

Nothing in this module deletes, moves or overwrites anything. It writes one file
and otherwise only reads.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Hidden so it stays out of the way in a file manager, `.json` so it is readable
# by a person who finds it and wonders what it is.
ANCHOR_NAME = ".bpdl-label.json"

# Depth below a root at which a label folder can still be found. Genre / LABELS /
# label is three, so this leaves room for a couple of levels of personal filing
# without walking an entire 5 TB library.
DEFAULT_MAX_DEPTH = 6

# A walk that has not finished in this long is abandoned rather than left to hold
# up a sweep — a sleeping USB disk can stall for minutes. An abandoned walk is
# reported as incomplete so a "not found" is never mistaken for "gone".
DEFAULT_BUDGET_SECONDS = 90.0

# Directories that are never worth descending: filesystem bookkeeping, recycle
# bins and NAS thumbnail caches, none of which hold library folders.
_SKIP_DIRS = frozenset({
    "$RECYCLE.BIN", "System Volume Information", "lost+found", "@eaDir",
    ".Trash-1000", "#recycle",
})

# How long a built index stays usable. One sweep checks every watched label in a
# few seconds, so this collapses a sweep's worth of lookups into a single walk
# while still noticing a folder moved between sweeps.
_INDEX_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class Resolution:
    """Where this label's new releases should actually be written, and why.

    `path` empty means there is no folder we can stand behind; the caller falls
    back to the staging folder. That is deliberately the failure mode for every
    uncertain case — staging costs a manual file, a wrong guess costs a download
    buried in the wrong label's folder.
    """

    path: str = ""
    status: str = "none"
    note: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.path)


# ---- the marker itself ----------------------------------------------------------

def anchor_path(folder: str | Path) -> Path:
    return Path(folder) / ANCHOR_NAME


def read_anchor(folder: str | Path) -> dict | None:
    """The marker in `folder`, or None if there isn't one we can read.

    Unreadable and malformed are both None on purpose: this runs against removable
    media that can vanish mid-read, and the only sane response to a marker we
    cannot understand is to behave as though it were absent.
    """
    try:
        with open(anchor_path(folder), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def anchor_matches(data: dict | None, label_id: int, store: str) -> bool:
    if not data:
        return False
    try:
        if int(data.get("label_id")) != int(label_id):
            return False
    except (TypeError, ValueError):
        return False
    # An older marker written before stores were recorded still identifies the
    # label; only a marker that names a DIFFERENT store is a mismatch.
    return str(data.get("store") or store) == str(store)


def write_anchor(folder: str | Path, *, label_id: int, store: str,
                 label_url: str = "", label_name: str = "") -> str:
    """Mark `folder` as this label's home. Returns "" on success, else why not.

    Never raises: this is called from an unattended sweep, and a read-only disk
    or a folder that disappeared must degrade to "not followed", never to a
    failed download run.

    Refuses to overwrite a marker naming a different label. That folder belongs
    to something else, and stamping our name on it is how two labels end up
    fighting over one directory.
    """
    target = Path(folder)
    existing = read_anchor(target)
    if existing and not anchor_matches(existing, label_id, store):
        return (f"{target} already belongs to label "
                f"{existing.get('label_name') or existing.get('label_id')}")
    payload = {
        "label_id": int(label_id),
        "store": str(store),
        "label_url": label_url,
        "label_name": label_name,
        # Kept from the first write so the marker records when the folder was
        # claimed, not when it was last touched.
        "created": (existing or {}).get("created")
                   or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "written_by": "beatportdl-webui",
        "note": "Identifies this folder as the label's home so new releases "
                "follow it when it is moved or renamed. Safe to delete; the "
                "folder then stops being followed.",
    }
    tmp = target / (ANCHOR_NAME + ".tmp")
    try:
        target.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        # Replace in one step: a marker half-written by an interrupted sweep would
        # read as malformed, i.e. as no marker at all, and lose the folder.
        os.replace(tmp, anchor_path(target))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return str(e)
    return ""


# ---- finding a marked folder ----------------------------------------------------

def _iter_marked(root: Path, max_depth: int, deadline: float) -> tuple[list[tuple[Path, dict]], bool]:
    """Every marked folder under `root`. Returns (found, completed).

    Stops descending as soon as a folder is marked — a label's releases live
    inside it and none of them is another label's home — which is what keeps this
    cheap on a library with tens of thousands of release folders.
    """
    found: list[tuple[Path, dict]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if time.monotonic() > deadline:
            return found, False
        current, depth = stack.pop()
        data = read_anchor(current)
        if data is not None:
            found.append((current, data))
            continue                      # marked: its children are its releases
        if depth >= max_depth:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    # follow_symlinks=False: a symlink back up the tree would
                    # otherwise walk forever, and a link is not where a folder
                    # physically lives anyway.
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue                      # unreadable branch: skip, don't abort
    return found, True


@dataclass
class _Index:
    built: float = 0.0
    key: tuple = ()
    complete: bool = True
    # (store, label_id) -> [(root rank, path), ...]
    entries: dict = field(default_factory=dict)


_cache = _Index()


def invalidate() -> None:
    """Forget the cached scan. Called after anything that moves a marker."""
    global _cache
    _cache = _Index()


def build_index(roots, *, max_depth: int = DEFAULT_MAX_DEPTH,
                budget: float = DEFAULT_BUDGET_SECONDS, use_cache: bool = True) -> _Index:
    """Index every marked folder under `roots`, in the order given.

    Root order is a priority: the first root a label is found under wins. That is
    what makes the copy-rather-than-move case resolve correctly — the copy left
    behind in the downloads folder loses to the one in the library, which is
    where the person meant it to end up.
    """
    global _cache
    roots = [str(r) for r in roots if r]
    key = (tuple(roots), max_depth)
    now = time.monotonic()
    if (use_cache and _cache.key == key and _cache.complete
            and now - _cache.built < _INDEX_TTL_SECONDS):
        return _cache

    index = _Index(built=now, key=key, complete=True)
    deadline = now + budget
    for rank, root in enumerate(roots):
        path = Path(root)
        if not path.is_dir():
            continue
        found, done = _iter_marked(path, max_depth, deadline)
        index.complete = index.complete and done
        for folder, data in found:
            try:
                ident = (str(data.get("store") or "beatport"), int(data.get("label_id")))
            except (TypeError, ValueError):
                continue
            index.entries.setdefault(ident, []).append((rank, str(folder)))

    _cache = index
    return index


def find(label_id: int, store: str, roots, **kw) -> tuple[list[str], bool]:
    """Marked folders for this label, best root first. Returns (paths, complete).

    Only the best-ranked root's matches are returned: a label found in the library
    is not made ambiguous by a leftover copy in the downloads folder.
    """
    index = build_index(roots, **kw)
    hits = index.entries.get((str(store), int(label_id)), [])
    if not hits:
        return [], index.complete
    best = min(rank for rank, _ in hits)
    return sorted(p for rank, p in hits if rank == best), index.complete


def resolve(recorded: str, label_id: int, store: str, roots, **kw) -> Resolution:
    """Decide where this label's new releases go, given the path last recorded.

    The recorded path is only trusted while it still holds this label's marker.
    Everything else is settled by the markers on disk, because the recorded path
    cannot tell a folder that moved from a folder that was emptied and left
    behind — and those two need opposite answers.
    """
    recorded = (recorded or "").strip()
    if not recorded:
        # No destination set is a decision, not a fault: it means staging.
        return Resolution(status="none")

    here = Path(recorded)
    at_recorded = read_anchor(here)
    if anchor_matches(at_recorded, label_id, store):
        return Resolution(path=recorded, status="ok")

    matches, complete = find(label_id, store, roots, **kw)
    if len(matches) == 1 and matches[0] != recorded:
        return Resolution(path=matches[0], status="moved",
                          note=f"folder moved to {matches[0]}")
    if len(matches) == 1:
        # Marker present but unmatched a moment ago — a transient read failure.
        return Resolution(path=recorded, status="ok")
    if len(matches) > 1:
        return Resolution(status="ambiguous", candidates=tuple(matches),
                          note=("this label is marked in more than one folder: "
                                + ", ".join(matches)))

    if at_recorded is not None:
        # Someone else's marker sits where ours should be. Writing here would put
        # this label's releases inside another label's folder.
        return Resolution(status="conflict",
                          note=(f"{recorded} is marked as "
                                f"{at_recorded.get('label_name') or at_recorded.get('label_id')}"))

    if here.is_dir():
        # Unmarked but present, and nothing marked anywhere else: this is the
        # folder, it just predates markers (or the marker was deleted). Claim it.
        return Resolution(path=recorded, status="adopted",
                          note="folder had no marker; marked it now")

    if not complete:
        # The walk ran out of time. "Not found" here means "not found yet", and
        # clearing the destination on that basis would lose a good path.
        return Resolution(status="unscanned",
                          note="library scan did not finish; will retry next check")

    return Resolution(status="lost",
                      note=f"{recorded} is gone and no marked folder was found")
