"""What Marquee has asked the download client for, followed through to the end.

The queue says a torrent is 63% done. That is not the question anyone has: they asked
for 212 games, and want to know how many have arrived, what is still coming, and why
eight of them never will from this set. So every download that starts is written
down -- which files, for which games, and what the release did not carry -- and read
back against the client's own per-file progress.

When a download finishes, that moment is kept. It is what lets the Overview say
"these arrived after the plan was built: plan again, then transfer" instead of
leaving someone to work out why the library has not changed.
"""
import json
import os
import posixpath
import time

from .. import acquire, atomic, sources
from ..errors import MarqueeError
from ..plan import human_bytes

FETCHES_FILE = "fetches.json"
# Enough to cover a session's work; older ones stop meaning anything to anyone.
KEEP = 20
# Shown per job on the Activity page, largest first.
ARRIVING_SHOWN = 30
MISSING_SHOWN = 60


def game_of(path, kind, owners=None):
    """The game a file in a release belongs to, as a person would name it.

    A ROM set holds one zip per machine. A merged CHD set files every clone's disk
    in its parent's folder, so the folder is not the game: `owners` maps a disk to
    the machine that asked for it, and the folder is only the fallback.
    """
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    if kind == "chds":
        stem = name[:-len(acquire.CHD_SUFFIX)] if name.lower().endswith(acquire.CHD_SUFFIX) else name
        if owners and stem in owners:
            return owners[stem]
        if len(parts) >= 2:
            return parts[-2]
    for suffix in (acquire.ROM_SUFFIX, acquire.CHD_SUFFIX):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def record_of(release, infohash, files, chosen, owners=None):
    """The part of a download worth remembering, from what `_one_part` had in hand."""
    by_index = {entry["index"]: entry for entry in files}
    wanted = []
    for index in chosen.indices:
        entry = by_index.get(index)
        if entry is None:
            continue
        wanted.append({"index": index, "path": entry["path"], "size": entry["size"],
                       "game": game_of(entry["path"], release.kind, owners)})
    return {"kind": release.kind, "release": release.name,
            "version": getattr(release, "version", None), "infohash": infohash,
            "files": wanted, "missing": list(chosen.missing)}


class FetchesMixin:
    """Part of `Application`; see marquee.web.application."""

    def _fetches_path(self):
        return os.path.join(sources.cache_dir(), FETCHES_FILE)

    def _read_fetches(self):
        try:
            with open(self._fetches_path(), encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return []
        return stored if isinstance(stored, list) else []

    def _write_fetches(self, jobs):
        try:
            atomic.write_json(self._fetches_path(), jobs[-KEEP:])
        except OSError:
            pass

    def record_fetch(self, parts, label):
        """Remember a download that has just been started. `parts` carry `_record`."""
        records = [part.pop("_record") for part in parts if part.get("_record")]
        if not records:
            return None
        with self._task_lock:
            jobs = self._read_fetches()
            job = {"id": f"{records[0]['infohash'][:8]}-{int(time.time())}",
                   "label": label, "started_at": time.time(), "completed_at": None,
                   "parts": records}
            jobs.append(job)
            self._write_fetches(jobs)
        self._queue_cache = {"at": 0.0, "data": None}
        return job["id"]

    def fetch_jobs(self, client):
        """Every remembered download, read against the client, newest first.

        Only jobs still arriving ask the client anything: a finished one is finished,
        and the queue is polled every few seconds.
        """
        with self._task_lock:
            jobs = self._read_fetches()
        changed = False
        out = []
        plan_version = getattr(getattr(self.job, "resolution", None), "xml_version", None)
        for job in reversed(jobs):
            view = _view(job, plan_version)
            if job.get("completed_at") is None:
                try:
                    progress = {part["infohash"]: _progress(client, part["infohash"])
                                for part in job["parts"]}
                except MarqueeError as error:
                    view["error"] = str(error)
                    progress = None
                if progress is not None:
                    _fill(view, job, progress)
                    if view["state"] == "complete":
                        job["completed_at"] = time.time()
                        view["completed_at"] = job["completed_at"]
                        changed = True
            else:
                view["state"] = "complete"
                view["games_done"] = view["games"]
                view["bytes_done"] = view["bytes"]
            out.append(view)
        if changed:
            with self._task_lock:
                stored = {job["id"]: job for job in self._read_fetches()}
                for job in jobs:
                    if job["id"] in stored and job.get("completed_at"):
                        stored[job["id"]]["completed_at"] = job["completed_at"]
                self._write_fetches(list(stored.values()))
        return out

    def finished_since_plan(self, _views=None):
        """Games a finished download brought that the plan in hand still calls missing.

        Asked of the plan, not of the clock. When a download finished is only known
        when someone next looks -- a restart that planned straight away would look
        later than the plan it had already built with those very files in it.
        """
        plan = self.job.plan
        # By file, not by game: a game whose zip arrived and whose disk the set never
        # had is still "needed", and counting the game would ask for a new plan for
        # ever. The question is whether the plan still wants what *this* brought.
        still = set()
        for item in (getattr(plan, "wanted", None) or []):
            if item.rom_to_fetch:
                still.add(("roms", item.name))
            for disk in item.disks_to_fetch:
                still.add(("chds", disk))
        games, files, size, jobs = set(), set(), 0, 0
        with self._task_lock:
            stored = self._read_fetches()
        for job in stored:
            if not job.get("completed_at"):
                continue
            arrived = False
            for part in job["parts"]:
                for entry in part["files"]:
                    name = entry["path"].replace("\\", "/").rsplit("/", 1)[-1]
                    stem = name.rsplit(".", 1)[0]
                    if (part["kind"], entry["game"] if part["kind"] == "roms" else stem) \
                            not in still:
                        continue
                    games.add(entry["game"])
                    # The same file fetched twice -- asked for again after it was
                    # removed -- is one file on disk, not two.
                    key = (part["release"], entry["path"])
                    if key not in files:
                        files.add(key)
                        size += entry["size"]
                    arrived = True
            jobs += arrived
        return {"jobs": jobs, "games": len(games), "bytes": size,
                "bytes_human": human_bytes(size)}


def _progress(client, infohash):
    """{file index: fraction} for one torrent, or None when the client no longer has it."""
    if client.one(infohash) is None:
        return None
    return {entry["index"]: entry.get("progress", 0.0) for entry in client.files(infohash)}


def _view(job, plan_version):
    games = set()
    size = 0
    for part in job["parts"]:
        for entry in part["files"]:
            # One game, however many files: a zip and its disk are one game arriving.
            games.add(entry["game"])
            size += entry["size"]
    missing = []
    for part in job["parts"]:
        if not part.get("missing"):
            continue
        missing.append({"release": part["release"], "kind": part["kind"],
                        "count": len(part["missing"]),
                        "names": [posixpath.basename(name) if part["kind"] == "chds"
                                  else name for name in part["missing"][:MISSING_SHOWN]],
                        "why": _why_missing(part, plan_version)})
    return {"id": job["id"], "label": job.get("label") or "",
            "started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
            "releases": [part["release"] for part in job["parts"]],
            "games": len(games), "bytes": size, "bytes_human": human_bytes(size),
            "games_done": 0, "bytes_done": 0, "state": "waiting",
            "arriving": [], "missing": missing}


def _fill(view, job, progress):
    """Per game: done when every one of its files is."""
    games, done_bytes, gone = {}, 0, False
    for part in job["parts"]:
        files = progress.get(part["infohash"])
        if files is None:
            gone = True
            continue
        for entry in part["files"]:
            fraction = min(1.0, files.get(entry["index"], 0.0))
            done_bytes += int(entry["size"] * fraction)
            key = entry["game"]
            have, total = games.get(key, (0, 0))
            games[key] = (have + int(entry["size"] * fraction), total + entry["size"])
    finished = [key for key, (have, total) in games.items() if total and have >= total]
    view["games_done"] = len(finished)
    view["bytes_done"] = done_bytes
    view["bytes_left_human"] = human_bytes(max(view["bytes"] - done_bytes, 0))
    arriving = sorted(((key, have / total) for key, (have, total) in games.items()
                       if total and have < total), key=lambda row: -row[1])
    view["arriving"] = [{"game": game, "progress": round(fraction, 3)}
                        for game, fraction in arriving[:ARRIVING_SHOWN]]
    view["arriving_count"] = len(arriving)
    if gone:
        # Removed from the client before it finished: what is there is all there is.
        view["state"] = "removed"
    elif games and view["games_done"] == len(games):
        view["state"] = "complete"
    elif done_bytes:
        view["state"] = "downloading"


def _why_missing(part, plan_version):
    """Why a release could not supply these -- the thing Wanted used to leave unsaid."""
    version = part.get("version")
    if part["kind"] == "chds":
        if version and plan_version and _older(version, plan_version):
            return (f"The newest CHD set is MAME {version}; these disks are new or changed "
                    f"since, and will come with a later CHD set. The games play once "
                    f"their disks arrive.")
        return "The CHD set does not carry these disks."
    return "The ROM set does not carry these machines; nothing to fetch for them here."


def _older(one, other):
    try:
        return tuple(int(x) for x in str(one).split(".")) < \
            tuple(int(x) for x in str(other).split("."))
    except ValueError:
        return False
