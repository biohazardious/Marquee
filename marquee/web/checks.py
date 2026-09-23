"""What the last library check found, kept past the plan it was made on.

A check opens every file in the library -- ten minutes over a share for ten thousand
games -- and its answer used to live on the plan's items. Building the plan again,
or restarting the container, threw it away, so the Overview said "nothing has looked
inside the files yet" about a library checked an hour before.

Each verdict is kept with the sizes the machine's files had when it was read, and is
handed back to a later plan only while every one of them is still there at that
size, for the same library and the same release. A file a transfer wrote is
forgotten outright: a same-size redump that replaced a stale zip must not inherit
the old zip's verdict and be replaced again for ever.
"""
import json
import os
import time

from .. import atomic, sources, verify
from .views import held_sizes

CHECK_FILE = "library-check.json"


def _path():
    return os.path.join(sources.cache_dir(), CHECK_FILE)


def _read():
    try:
        with open(_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("items"), dict):
        return None
    return stored


def _held(plan):
    """{library path: size} for the files the library holds as they are. A file the
    check found stale is an UPDATE by then, and it is the size it holds that counts."""
    return held_sizes(plan)


def snapshot_sizes(plan):
    """The sizes to file verdicts under, taken before the check reads anything."""
    return _held(plan)


def save(plan, items, library, version, sizes, deep=False):
    """Keep the verdicts of the items this check reached. Merged with the last
    check of the same library and release, so a stopped check loses nothing."""
    stored = _read()
    if not stored or stored.get("library") != library or stored.get("version") != version:
        stored = {"library": library, "version": version, "items": {}}
    for item in items:
        if not item.state:
            continue
        paths = {relpath: sizes[relpath] for relpath in item.wanted_paths()
                 if relpath in sizes}
        if not paths:
            # Read now, but with nothing to file it under: an older verdict must not
            # outlive it.
            stored["items"].pop(item.name, None)
            continue
        stored["items"][item.name] = {
            "state": item.state, "detail": list(item.state_detail or []),
            "rom_state": item.rom_state or "",
            "damaged": list(item.damaged_disks or []), "sizes": paths}
    stored["checked_at"] = time.time()
    stored["deep"] = bool(deep) or bool(stored.get("deep"))
    try:
        atomic.write_json(_path(), stored)
    except OSError:
        pass


def forget(names):
    """Drop the verdicts of machines whose files a transfer has just written."""
    names = set(names)
    stored = _read()
    if not stored or not names:
        return
    before = len(stored["items"])
    stored["items"] = {name: verdict for name, verdict in stored["items"].items()
                       if name not in names}
    if len(stored["items"]) != before:
        try:
            atomic.write_json(_path(), stored)
        except OSError:
            pass


def restore(plan, library, version):
    """Put the kept verdicts back on a freshly compared plan.

    Returns ({state: count}, checked_at) for what was restored, or (None, None).
    """
    stored = _read()
    if not stored or stored.get("library") != library or stored.get("version") != version:
        return None, None
    held = _held(plan)
    if not held:
        return None, None
    counts = {}
    for item in plan.wanted:
        verdict = stored["items"].get(item.name)
        if not verdict or verdict.get("state") not in verify.STATES:
            continue
        kept = verdict.get("sizes") or {}
        present = {relpath for relpath in item.wanted_paths() if relpath in held}
        # Every file it was read with, still there at that size -- and nothing new
        # arrived beside them since.
        if not kept or set(kept) != present \
                or any(held.get(relpath) != size for relpath, size in kept.items()):
            continue
        item.state = verdict["state"]
        item.state_detail = list(verdict.get("detail") or [])
        item.rom_state = verdict.get("rom_state") or ""
        item.damaged_disks = list(verdict.get("damaged") or [])
        counts[item.state] = counts.get(item.state, 0) + 1
    if not counts:
        return None, None
    return counts, stored.get("checked_at")
