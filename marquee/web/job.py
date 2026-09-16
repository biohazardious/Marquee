"""One background job at a time, with everything the browser needs to poll for.

The pipeline already reports through a Reporter and separates planning from copying, so
this is only bookkeeping: run the work on a thread, collect events, and expose a
snapshot the page can render.
"""
import threading
import traceback

from .. import backends, catalog, pipeline, sources, sync, verify
from ..errors import MarqueeError
from ..plan import _leaf_label, human_bytes
from ..reporting import Reporter

MAX_EVENTS = 2000
# The most rows one request will build. The page asks for 60 or 150; the selection
# tree asks for this.
ROW_LIMIT = 500


class QueueReporter(Reporter):
    """Pushes everything the pipeline says into the job, for the page to pick up."""

    def __init__(self, job):
        self.job = job

    def info(self, message):
        self.job.add_event("info", message)

    def warn(self, message):
        self.job.add_event("warn", message)

    def stage(self, name):
        self.job.add_event("stage", name)

    def progress(self, stage, done, total, detail=""):
        self.job.set_progress(stage, done, total, detail)


class Job:
    """States: idle -> planning -> planned -> copying -> done, or error at any point."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self.reset()

    def reset(self):
        with self._lock:
            self._reset_locked()

    def _reset_locked(self):
        """The field reset, for a caller already holding the lock."""
        self.state = "idle"
        # Always present. `copy` reads it to find out which release the library is
        # being written as, and reached for it before any plan had ever run.
        self.config = None
        self.events = []
        self._described = {}
        self.sequence = 0
        self.progress = None
        self.plan = None
        self.resolution = None
        self.summary = None
        self.checked = None
        self.error = None
        self._cancel.clear()

    # -- reporting sink ---------------------------------------------------- #

    def add_event(self, kind, text):
        with self._lock:
            self.sequence += 1
            self.events.append({"n": self.sequence, "kind": kind, "text": text})
            # A full run emits thousands of lines; the page only shows the tail.
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]

    def set_progress(self, stage, done, total, detail):
        with self._lock:
            self.progress = {"stage": stage, "done": done, "total": total,
                             "detail": detail,
                             "percent": int(done / total * 100) if total else 0}

    # -- running ----------------------------------------------------------- #

    @property
    def busy(self):
        return self.state in ("planning", "copying", "checking")

    def _run(self, work, running_state, finished_state):
        with self._lock:
            self.state = running_state
            self.error = None
        try:
            work()
            with self._lock:
                self.state = finished_state
        except MarqueeError as error:
            with self._lock:
                self.state, self.error = "error", str(error)
            self.add_event("warn", str(error))
        except Exception as error:  # noqa: BLE001 - surfaced to the page, not swallowed
            with self._lock:
                self.state, self.error = "error", f"{type(error).__name__}: {error}"
            self.add_event("warn", traceback.format_exc(limit=3))

    def _start(self, work, running_state, finished_state, prepare=None):
        """Start `work` on a thread -- or refuse, if one is already running.

        The busy check and the state change happen under the lock, together. The
        state used to become "copying" only once the thread got going, so two
        requests arriving within that window -- a double-click on Start transfer --
        both passed the check and ran two transfers into one destination.
        `prepare` runs inside the same critical section, for a reset that must not
        blank a job that is still running.
        """
        with self._lock:
            if self.busy:
                raise MarqueeError("A job is already running.")
            if prepare is not None:
                prepare()
            self.state = running_state
            self.error = None
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(work, running_state, finished_state),
                daemon=True)
            self._thread.start()

    def start_plan(self, config, options):
        def prepare():
            self._reset_locked()
            self.config = config

        def work():
            plan, resolution = pipeline.build_plan(config, options, QueueReporter(self))
            with self._lock:
                self.plan, self.resolution = plan, resolution

        self._start(work, "planning", "planned", prepare=prepare)

    def start_check(self, xml_file):
        """Check what the library holds against the release it is supposed to be.

        A separate job rather than part of planning: it reads every zip in the library
        and the whole XML again, which is a minute or two, and it is only worth doing
        when something has actually changed.
        """
        if self.plan is None:
            raise MarqueeError("Build a plan first, so there is something to check.")
        if not xml_file:
            raise MarqueeError("The release's XML is not on disk to check against.")
        if backends.is_remote(self.config.copy_path):
            # check_library opens the files with os.path; over a network protocol that
            # would report every game absent and call it a result.
            raise MarqueeError("Checking looks inside every file, which is only "
                               "possible for a library on a local path.")

        plan, config = self.plan, self.config

        def work():
            reporter = QueueReporter(self)
            # What the library holds, not what the torrent folder holds. Before the
            # first transfer the two are disjoint, and checking the 1,144 games that
            # were only in the source folder reported every one of them "absent" --
            # a result that then hid behind every check-result filter.
            if plan.sync is not None:
                at_destination = set()
                for kind in (sync.KEEP, sync.UPDATE, sync.MOVE):
                    at_destination.update(plan.sync.machines(kind))
                here = [item for item in plan.wanted
                        if item.in_library or item.name in at_destination]
            else:
                here = [item for item in plan.wanted
                        if item.in_library or item.rom_source]
            if not here:
                reporter.warn("Nothing is in the library yet, so there is nothing to "
                              "check. Transfer first.")
                return
            reporter.stage("Reading the release's ROM lists...")
            manifests = sources.rom_manifests(
                xml_file, wanted={item.name for item in here},
                on_progress=lambda done, total, count: reporter.progress(
                    "manifests", done, total, f"{count:,} machines"))
            reporter.stage(f"Checking {len(here):,} games against MAME "
                           f"{sources.read_xml_build(xml_file) or '?'}...")
            # A file the next run is going to relocate is not where it belongs yet.
            # Checking only the destination path reported 871 of those as absent when
            # every one of them was sitting a folder away.
            where = {}
            if plan.sync is not None:
                for action in plan.sync.actions:
                    if (action.kind == sync.MOVE and action.machine
                            and action.relpath.endswith(".zip")):
                        where[action.machine] = action.from_relpath
            counts = verify.check_library(
                here, manifests, config.copy_path, reporter,
                should_continue=lambda: not self._cancel.is_set(), where=where)
            with self._lock:
                self.checked = counts
                # On the plan as well: the page's description of it is memoised, and
                # the check changes what that description should say.
                plan.checked = counts
            reporter.info("Checked: " + (verify.summarise(counts) or "nothing"))
            if counts.get(verify.STALE) or counts.get(verify.DAMAGED):
                # The diff was worked out from names and sizes. Something has now read
                # what is inside, and a redump that weighs the same as the dump it
                # replaces is invisible to a size comparison -- so the diff has to be
                # taken again, or the transfer will leave those files exactly as they
                # are while the page says they are out of date.
                try:
                    report = pipeline.compare_destination(plan, config, reporter)
                    with self._lock:
                        plan.sync = report
                except MarqueeError as error:
                    reporter.warn(f"Could not re-check what the library holds: {error}")

        self._start(work, "checking", "planned")

    def start_copy(self, delete_orphans=False, mame_version=None):
        if self.plan is None:
            raise MarqueeError("Nothing planned yet.")

        def work():
            summary = pipeline.execute(self.plan, self.config, QueueReporter(self),
                                       should_continue=lambda: not self._cancel.is_set(),
                                       delete_orphans=delete_orphans,
                                       mame_version=mame_version)
            with self._lock:
                self.summary = summary
                # The destination has changed underneath the plan; the next run has to
                # look again rather than act on a stale diff.
                self.plan.sync = None

        self._start(work, "copying", "done")

    def cancel(self):
        """Ask the running transfer or check to stop. Planning cannot be stopped.

        Returns whether anything was told to stop: the button used to log "Stopping
        after the current machine.." while a plan was building and then nothing
        stopped, because nothing in build_plan ever looks at the flag.
        """
        if self.state not in ("copying", "checking"):
            return False
        self._cancel.set()
        self.add_event("warn", "Stopping after the current machine..")
        return True

    # -- what the page reads ------------------------------------------------ #

    def snapshot(self, after=0, sizes=None):
        """The job as the page reads it.

        Only the copy of the fields happens under the lock. Describing the plan can
        mean a disk_usage call and, before a comparison, a scan of every destination
        folder -- and while that ran under the lock, the worker's every log line and
        progress tick waited on it.

        Every key is always present, None when there is nothing: the page merges
        snapshots, so a key that was simply left out kept its previous run's value,
        and a re-plan showed the old transfer's summary until the new one finished.
        """
        with self._lock:
            payload = {
                "state": self.state,
                "error": self.error,
                "progress": self.progress,
                "events": [event for event in self.events if event["n"] > after],
                "sequence": self.sequence,
                "plan": None, "resolution": None, "checked": None, "summary": None,
            }
            plan, config, resolution = self.plan, self.config, self.resolution
            checked = None if self.checked is None else dict(self.checked)
            summary = self.summary
            described = self._described
        if plan is not None:
            payload["plan"] = describe(plan, config, described, sizes)
            # What this plan was actually built from, so the page can tell whether
            # its own form has moved on rather than guessing from a flag.
            payload["plan"]["built_from"] = built_from(config)
        if resolution is not None:
            payload["resolution"] = {
                "xml_file": resolution.xml_file,
                "xml_version": resolution.xml_version,
                "catlist_file": resolution.catlist_file,
                "catlist_version": resolution.catlist_version,
                "rom_dir": resolution.rom_dir,
                "chd_dir": resolution.chd_dir,
                "machines_read": resolution.machine_count,
                "filtered": [
                    {"reason": reason, "label": label,
                     "count": resolution.filtered.get(reason, 0)}
                    for reason, label in catalog.REASONS
                    if resolution.filtered.get(reason)],
                "machines_kept": resolution.kept_count,
                "genres": resolution.genres,
                "destination_exists": resolution.destination_exists,
            }
        if checked is not None:
            payload["checked"] = checked
        if summary is not None:
            payload["summary"] = {
                "machines": summary.machines,
                "copied": summary.copied,
                "updated": summary.updated,
                "moved": summary.moved,
                "deleted": summary.deleted,
                "skipped": summary.skipped,
                "bytes": summary.copied_bytes,
                "bytes_human": human_bytes(summary.copied_bytes),
                "seconds": round(summary.seconds, 1),
                "rate_human": human_bytes(summary.rate) + "/s",
                "cancelled": summary.cancelled,
                "failed": summary.failed,
            }
        return payload


# Which label wins when one machine's files disagree.
STATUS_ORDER = (sync.NEW, sync.UPDATE, sync.MOVE, sync.KEEP)


def machine_status(plan):
    """{machine name: worst status of its files}, for the table the page shows.

    Read off the actions, which every one of them now names its machine on. The
    version that walked `plan.items` and then stamped every in-library machine as
    KEEP could not see a relocation: 944 games the next run was going to move read
    `keep` on the library page, and the `move` filter was permanently empty.
    """
    if plan.sync is None:
        return {}
    # Memoised on the plan: the library search asks on every keystroke, and walking
    # fifty thousand actions each time is work that gives the same answer.
    key = (id(plan.sync), len(plan.sync.actions), plan.sync.to_transfer)
    cached = getattr(plan, "_status_cache", None)
    if cached and cached[0] == key:
        return cached[1]
    found = {}
    for action in plan.sync.actions:
        if action.machine:
            found.setdefault(action.machine, set()).add(action.kind)
    statuses = {}
    for name, kinds in found.items():
        for candidate in STATUS_ORDER:
            if candidate in kinds:
                statuses[name] = candidate
                break
    plan._status_cache = (key, statuses)
    return statuses


# What an absent machine's status is called. The sync kinds describe files that are
# here; this is the fourth answer, and the one a fresh install gives for everything.
MISSING = "missing"


def filtered(plan, query="", status="", genre="", category="", mature="", have="",
             condition="", reason="", state="", statuses=None, with_excluded=False):
    """The machines the page is currently showing -- on disk or not.

    Shared with the download action on purpose: "fetch what I am looking at" is only
    trustworthy if the list being fetched is built by the same code that built the
    list being looked at.

    `with_excluded` adds the machines the exclude list leaves out. The selection tree
    needs them -- it lists every category so one can be put back -- while the library
    and the download button do not.
    """
    statuses = machine_status(plan) if statuses is None else statuses
    needle = query.strip().lower()
    kept = []
    source = plan.wanted
    if with_excluded:
        source = source + list(getattr(plan, "excluded_items", ()))
    for item in source:
        if needle and needle not in item.name.lower() \
                and needle not in item.description.lower():
            continue
        if genre and item.genre != genre:
            continue
        if category and item.category != category:
            continue
        if mature == "only" and not item.mature:
            continue
        if mature == "hide" and item.mature:
            continue
        if reason and item.reason != reason:
            continue
        if state and item.state != state:
            continue
        if condition == "good" and not item.flawless:
            continue
        if condition == "flawed" and item.flawless:
            continue
        present = bool(item.rom_source or item.chd_sources or item.in_library)
        if have == "yes" and not present:
            continue
        if have == "no" and present:
            continue
        if status and (statuses.get(item.name, "") if present else MISSING) != status:
            continue
        kept.append(item)
    return kept


def machine_rows(plan, query="", status="", genre="", category="", offset=0, limit=200,
                 sort="size", descending=True, with_excluded=False, mature="", have="",
                 condition="", reason="", state="", sizes=None):
    """A filtered, sorted page of the plan's machines.

    `with_excluded` lists the machines the exclude list leaves out as well, marked.
    `sizes` is what the release torrent says a machine weighs, which is the only figure
    there is for one that has not been downloaded yet.
    """
    statuses = machine_status(plan)
    sizes = sizes or {}

    # Filter and sort the items, and only build row dictionaries for the page that is
    # actually returned. Building 12,000 of them to hand back 60 cost 32 ms a request.
    kept = filtered(plan, query=query, status=status, genre=genre, category=category,
                    mature=mature, have=have, condition=condition, reason=reason,
                    state=state, statuses=statuses, with_excluded=with_excluded)

    weigh = lambda item: item.total_bytes or sizes.get(item.name, 0)  # noqa: E731
    keys = {"size": weigh,
            "name": lambda item: item.name,
            "description": lambda item: item.description.lower(),
            "category": lambda item: item.category,
            "year": lambda item: item.year or "",
            "manufacturer": lambda item: (item.manufacturer or "").lower()}
    kept.sort(key=keys.get(sort, keys["size"]), reverse=descending)

    # Clamped, not trusted: a negative offset is a Python slice from the end, so
    # `?offset=-5` quietly answered with the last five rows and called them page one.
    offset = max(0, offset)
    page = kept[offset:offset + max(1, min(limit, ROW_LIMIT))]
    rows = []
    for item in page:
        if item.rom_source or item.chd_sources or item.in_library:
            rows.append(machine_row(item, statuses.get(item.name, ""),
                                    size=None if item.total_bytes
                                    else sizes.get(item.name, 0)))
        else:
            rows.append(machine_row(item, MISSING, size=sizes.get(item.name, 0)))
    return {"total": len(kept), "offset": offset,
            "bytes": sum(weigh(item) for item in kept), "rows": rows}


def left_out_payload(plan, sizes=None, reason="", query="", condition=""):
    """The shape of the left-out catalogue: why, and the tree to browse it in.

    The tree is built from whatever the filters leave, not from the whole set. A
    dropdown that narrows the rows inside a category but leaves every count above it
    reading the unfiltered total is worse than no filter at all.
    """
    empty = {"total": 0, "reasons": [], "genres": [], "categories": [], "bytes_human": ""}
    if plan is None or plan.left_out is None:
        return empty
    left = plan.left_out

    # The tally is always over everything, so the dropdown can say what choosing each
    # one would give you.
    counted = {}
    for item in left.wanted:
        counted[item.reason] = counted.get(item.reason, 0) + 1
    reasons = [{"reason": name, "label": label, "count": counted.get(name, 0)}
               for name, label in catalog.REASONS if counted.get(name)]

    kept = filtered(left, reason=reason, query=query, condition=condition)

    # What the exclusions have in common. One press of "leave all out" on a filtered
    # view writes every name it matched, and the list that comes out carries no memory
    # of the rule that made it -- so this is the only way back to "ah, those".
    flawed = sum(1 for item in left.wanted
                 if item.reason == "blacklisted" and not item.flawless)
    sizes = sizes or {}
    genres, categories = {}, {}
    total_bytes = 0
    for item in kept:
        weight = item.total_bytes or sizes.get(item.name, 0)
        total_bytes += weight
        genre = genres.setdefault(item.genre, {"name": item.genre, "wanted": 0,
                                               "wanted_bytes": 0})
        leaf = categories.setdefault(item.category,
                                     {"name": item.category, "genre": item.genre,
                                      "mature": item.mature,
                                      "label": _leaf_label(item.category),
                                      "wanted": 0, "wanted_bytes": 0})
        for entry in (genre, leaf):
            entry["wanted"] += 1
            entry["wanted_bytes"] += weight

    order = lambda entry: (-entry["wanted_bytes"], -entry["wanted"], entry["name"])  # noqa: E731
    for entry in list(genres.values()) + list(categories.values()):
        entry["wanted_bytes_human"] = human_bytes(entry["wanted_bytes"])
    return {"total": len(kept), "reasons": reasons,
            "genres": sorted(genres.values(), key=order),
            "categories": sorted(categories.values(), key=order),
            "imperfect_exclusions": flawed,
            "bytes_human": human_bytes(total_bytes)}


# What a run will do, worst first, in the words somebody about to press Transfer
# needs: not the sync kind, but what happens to their files.
# Not a sync kind: nothing here can be copied, because there is no file to copy. It
# belongs on this page all the same -- "out of date" and "Replace: 0 files" side by
# side is the page contradicting itself, when the answer is that those games have to
# be downloaded before any transfer can do anything about them.
FETCH = "fetch"

CHANGES = (
    (sync.NEW, "Copy over", "games the library does not have yet"),
    (sync.UPDATE, "Replace", "there already, but not what this release says it is"),
    (sync.MOVE, "Move", "the same file, in a folder this release no longer uses -- "
                        "renamed, never re-copied"),
    (sync.ORPHAN, "Delete", "in the library, wanted by nothing -- left alone unless "
                            "you ask for them to go"),
    (sync.KEEP, "Leave alone", "already correct, and not touched"),
    (FETCH, "Fetch first", "wanted, with nothing here to copy from -- download them "
                           "and this run can place them"),
)

CHANGE_LABELS = dict((kind, label) for kind, label, _note in CHANGES)


def _disk_owners(plan):
    """{disk name: the machine that wants it}, the library and the left-out set alike.

    A disk is not named after the machine that uses it -- CarnEvil's clone keeps
    `carnevi1.chd` in `carnevil/` -- so the only way to say whose a stray disk is, is
    to ask the release which machine lists it.
    """
    owners = {}
    for source in (plan, getattr(plan, "left_out", None)):
        if source is None:
            continue
        for item in source.wanted:
            for disk in item.disks:
                owners.setdefault(disk, item)
    return owners


def _described(plan):
    """({machine name: the record}, the names the selection keeps).

    An orphan is by definition not in the plan, so the only way to say more about it
    than its filename is to look in the catalogue it fell out of -- the library's own
    and the one it was left out of, both.
    """
    index, wanted = {}, set()
    for item in plan.wanted:
        index[item.name] = item
        wanted.add(item.name)
    left = getattr(plan, "left_out", None)
    if left is not None:
        for item in left.wanted:
            index.setdefault(item.name, item)
    return index, wanted


REASON_LABELS = dict(catalog.REASONS)


def _kept_copies(report):
    """{(file name, size): where that file ends up}, for everything the run keeps.

    A library that has been recategorised more than once holds the same disk in two
    or three old folders. Exactly one of them is claimed; the rest are duplicates, and
    saying so is the difference between "delete 917 mystery files" and "delete the
    copies of files you are keeping anyway".

    Keyed on the size as well as the name: two machines can name a disk the same
    thing, and pointing at an unrelated file would be worse than saying nothing.
    """
    copies = {}
    for action in report.actions:
        if action.kind == sync.ORPHAN:
            continue
        copies.setdefault(
            (action.relpath.rsplit("/", 1)[-1], action.size), action.relpath)
    return copies


def _why_unwanted(path, index, wanted, headed_for, disks=None, version="",
                  copies=None, size=0):
    """(what this file is, why it is not wanted) -- in one phrase each.

    "Delete 4,000 files" is not something anybody can agree to; "these are the ones
    you excluded, these belong to a clone MAME has dropped, these are not in MAME any
    more" is. Upgrading a library is mostly the middle one.

    The one thing this must never do is borrow the folder's game as the file's
    identity. A dropped clone keeps its disk in its parent's folder, so doing that put
    `CarnEvil (v1.0.3)` -- the game being kept -- on the row proposing to delete
    1.4 GB, and made a correct deletion look like a mistake.
    """
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    release = f"MAME {version}" if version else "this release"

    # The same file, the same size, already accounted for somewhere else. Whatever it
    # is named after, this copy of it is spare.
    elsewhere = (copies or {}).get((name, size))
    if elsewhere and elsewhere != path:
        owner = (disks or {}).get(stem) or index.get(stem)
        return (owner.description if owner else ""), f"a second copy; kept as {elsewhere}"

    item = index.get(stem)
    if item is None and name.lower().endswith(".chd"):
        # Disks are named after themselves, not after the machine that uses them, so
        # ask the release which machine lists this one.
        owner = (disks or {}).get(stem)
        if owner is not None and owner.name not in wanted:
            return owner.description, f"a disk for {owner.name}, which is left out"
        if owner is not None:
            # Listed by the release, for a machine being kept, and yet nothing claimed
            # this copy. Say what is true and no more.
            return owner.description, (f"{release} lists this disk for {owner.name}, "
                                       f"which keeps a different copy")
        holder = index.get(path.rsplit("/", 2)[-2]) if path.count("/") >= 2 else None
        if holder is not None:
            # Whose folder it is in is context, not identity. Naming the file is the
            # only honest answer to "what is this".
            return "", (f"in {holder.description}’s folder, but no machine in "
                        f"{release} uses this disk")
        return "", f"no machine in {release} uses this disk"
    if item is None:
        return "", f"not in {release}"
    if item.name in headed_for:
        return item.description, f"a new copy is going to {headed_for[item.name]}"
    if item.reason == "blacklisted":
        return item.description, "you left it out"
    if item.reason:
        return item.description, REASON_LABELS.get(item.reason, item.reason)
    if item.name in wanted:
        return item.description, f"not one of its files in {release}"
    return item.description, "no longer wanted"


def _change_rows(plan, report, kind, needle, index, wanted, version=""):
    """The rows for one kind: games for everything the plan owns, files for orphans."""
    actions = [action for action in report.actions if action.kind == kind]
    if kind == sync.ORPHAN:
        headed_for = {action.machine: action.relpath.rsplit("/", 1)[0]
                      for action in report.actions
                      if action.machine and action.kind in (sync.NEW, sync.UPDATE)}
        disks = _disk_owners(plan)
        copies = _kept_copies(report)
        rows = []
        for action in actions:
            description, why = _why_unwanted(action.relpath, index, wanted,
                                             headed_for, disks, version, copies,
                                             action.size)
            if needle and needle not in action.relpath.lower() \
                    and needle not in description.lower():
                continue
            rows.append({"kind": kind, "path": action.relpath,
                         "name": action.relpath.rsplit("/", 1)[-1],
                         "description": description, "why": why,
                         "folder": action.relpath.rsplit("/", 1)[0],
                         "files": 1, "bytes": action.size,
                         "bytes_human": human_bytes(action.size)})
        return rows

    grouped = {}
    for action in actions:
        grouped.setdefault(action.machine, []).append(action)
    rows = []
    for name, mine in grouped.items():
        item = index.get(name)
        description = item.description if item else ""
        if needle and needle not in (name or "").lower() \
                and needle not in description.lower():
            continue
        weight = sum(action.size for action in mine)
        row = {"kind": kind, "name": name or "", "description": description,
               "category": item.category if item else "",
               "genre": item.genre if item else "",
               "folder": item.folder if item else mine[0].relpath.rsplit("/", 1)[0],
               "files": len(mine), "bytes": weight,
               "bytes_human": human_bytes(weight),
               "paths": [action.relpath for action in mine[:8]],
               "chd": bool(item and (item.chd_sources or item.disks)),
               "mature": bool(item and item.mature),
               "state": item.state if item else "",
               "state_detail": item.state_detail if item else []}
        moved = [action for action in mine if action.from_relpath]
        if moved:
            row["from"] = moved[0].from_relpath
        rows.append(row)
    return rows


def _fetch_rows(plan, needle, sizes, needed=None):
    """The games no transfer can help with until they are downloaded."""
    wanted = set(plan.needed) if needed is None else needed
    rows = []
    for item in plan.wanted:
        if item.name not in wanted:
            continue
        if needle and needle not in item.name.lower() \
                and needle not in item.description.lower():
            continue
        if item.state in (verify.STALE, verify.DAMAGED):
            why = "out of date -- the library's copy is not this release"
        elif item.in_library:
            why = "in the library, but not complete"
        else:
            why = "not downloaded yet"
        weight = item.total_bytes or sizes.get(item.name, 0)
        rows.append({"kind": FETCH, "name": item.name,
                     "description": item.description,
                     "category": item.category, "genre": item.genre,
                     "folder": item.folder, "why": why,
                     "files": 1, "bytes": weight,
                     "bytes_human": human_bytes(weight) if weight else "—",
                     "paths": [], "chd": bool(item.chd_sources or item.disks),
                     "mature": item.mature, "state": item.state,
                     "state_detail": item.state_detail})
    return rows


def changes_payload(plan, kind="", query="", offset=0, limit=ROW_LIMIT, sizes=None,
                    version=""):
    """What the next Transfer will actually do, file by file and game by game.

    Everything needed to answer this has been worked out since the first version --
    the sync report is what the copy runs from -- and none of it was ever shown. A
    run that copies, replaces, renames and deletes should be readable before it
    happens, not afterwards in the log.
    """
    empty = {"compared": False, "kinds": [], "rows": [], "total": 0, "offset": 0,
             "files": 0, "bytes": 0}
    if plan is None or plan.sync is None:
        return empty
    report = plan.sync
    index, wanted = _described(plan)
    sizes = sizes or {}
    needed = plan.needed
    # One set, built once. `set(needed)` inside the comprehension was rebuilt for
    # every one of 45,000 wanted machines: twenty seconds per page load.
    needed_set = set(needed)
    to_fetch = sum(
        (item.total_bytes or sizes.get(item.name, 0))
        for item in plan.wanted if item.name in needed_set)

    kinds = []
    for name, label, note in CHANGES:
        if name == FETCH:
            kinds.append({"kind": name, "label": label, "note": note,
                          "files": len(needed), "machines": len(needed),
                          "bytes": to_fetch, "bytes_human": human_bytes(to_fetch)})
            continue
        kinds.append({
            "kind": name, "label": label, "note": note,
            "files": report.counts.get(name, 0),
            # Orphans belong to nothing -- that is what makes them orphans -- so
            # counting games for them would always read zero.
            "machines": 0 if name == sync.ORPHAN else len(report.machines(name)),
            "bytes": report.bytes.get(name, 0),
            "bytes_human": human_bytes(report.bytes.get(name, 0)),
        })

    payload = {
        "compared": True,
        "kinds": kinds,
        "to_transfer": report.to_transfer,
        "to_transfer_human": human_bytes(report.to_transfer),
        "delete_bytes": report.bytes.get(sync.ORPHAN, 0),
        "delete_bytes_human": human_bytes(report.bytes.get(sync.ORPHAN, 0)),
        # Until something has read inside the files, "replace" only knows what the
        # sizes say -- and a redump that weighs the same is invisible to that.
        "checked": bool(plan.checked),
        "states": {state: plan.checked[state] for state in verify.STATES
                   if (plan.checked or {}).get(state)},
        # Wanted, not here, and not at the destination either: no action can be
        # planned for a file that does not exist yet, so it would otherwise be
        # missing from the one page that claims to say what is going to happen.
        "waiting": len(needed),
    }

    chosen = kind if any(entry["kind"] == kind for entry in kinds) else ""
    rows = []
    if chosen == FETCH:
        rows = _fetch_rows(plan, query.strip().lower(), sizes, needed_set)
        rows.sort(key=lambda row: (-row["bytes"], row["name"]))
    elif chosen:
        rows = _change_rows(plan, report, chosen, query.strip().lower(),
                            index, wanted, version)
        rows.sort(key=lambda row: (-row["bytes"], row["name"]))
    payload["kind"] = chosen
    payload["total"] = len(rows)
    payload["files"] = sum(row["files"] for row in rows)
    payload["bytes"] = sum(row["bytes"] for row in rows)
    payload["bytes_shown_human"] = human_bytes(payload["bytes"])
    offset = max(0, offset)
    payload["offset"] = offset
    payload["rows"] = rows[offset:offset + max(1, min(limit, ROW_LIMIT))]
    return payload

def machine_keys(plan, sizes=None, **filters):
    """Just enough of every match to act on it: name, genre, category, weight.

    The Selection page needs two things a page of rows cannot give it. "Untick all of
    these" has to cover the whole match rather than the 500 rows drawn -- and the
    rules for what unticking one game does to its siblings have to know every game in
    its category, not the ones that happen to have been fetched. A category of 965
    games judged on the 500 that were loaded reports the wrong state and silently puts
    the other 465 back.

    Everything else in a row is a description nobody is going to read; this is about
    a tenth the size.
    """
    kept = filtered(plan, **filters)
    sizes = sizes or {}
    return {"total": len(kept),
            "machines": [{"name": item.name, "genre": item.genre,
                          "category": item.category,
                          "bytes": item.total_bytes or sizes.get(item.name, 0)}
                         for item in kept]}


def machine_row(item, state="", size=None):
    """One machine as the page shows it.

    Deliberately flat and pre-formatted: the browser renders thousands of these and
    should not be computing sizes or working out whether a screen is rotated.
    """
    return {
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "genre": item.genre,
        "folder": item.folder,
        "excluded": bool(getattr(item, "excluded", False)),
        "partial": bool(getattr(item, "partial", False)),
        "bytes": size if size is not None else item.total_bytes,
        "bytes_human": human_bytes(size if size is not None else item.total_bytes),
        "chd": bool(item.chd_sources or item.disks),
        "status": state,
        "year": item.year,
        "manufacturer": item.manufacturer,
        "players": item.players,
        "buttons": item.buttons,
        "controls": item.controls,
        "vertical": item.is_vertical,
        "clone": item.is_clone,
        "mature": item.mature,
        "cloneof": item.cloneof,
        "resolution": item.resolution,
        "display": (item.display or {}).get("type", ""),
        "refresh": (item.display or {}).get("refresh", 0),
        "missing_disks": item.missing_disks,
        "driver_status": item.driver_status,
        "reason": item.reason,
        # What checking it against the release found, and what was wrong.
        "state": item.state,
        "state_detail": item.state_detail,
        "savestate": item.savestate,
        "features": item.features,
        "condition": item.condition(),
        "flawless": item.flawless,
        "have_rom": bool(item.rom_source),
        "in_library": item.in_library,
        "here": bool(item.rom_source or item.chd_sources or item.in_library),
    }


def _library_files(plan, item):
    """Where this machine's files are at the destination right now.

    `item.files()` can only speak for the source folder, so a machine that is already
    in the library -- which on an upgrade is most of them -- showed an empty file list
    and "none of this machine's files are on disk yet" under a row marked `keep`.
    """
    if plan.sync is None:
        return []
    found = []
    for action in plan.sync.actions:
        if action.machine != item.name:
            continue
        if action.kind in (sync.KEEP, sync.UPDATE):
            found.append((action.relpath, action.size, ""))
        elif action.kind == sync.MOVE:
            found.append((action.from_relpath, action.size, action.relpath))
    return [{"destination": path, "bytes": size, "bytes_human": human_bytes(size),
             "moving_to": moving_to}
            for path, size, moving_to in found]


def machine_detail(plan, name, sizes=None):
    """Everything known about one machine, plus its file list."""
    sizes = sizes or {}
    statuses = machine_status(plan)
    for item in plan.wanted:
        if item.name != name:
            continue
        # The same rule the grid uses. Deciding it here from the source folder alone
        # put a "missing" badge on games the row beside it called `keep`.
        present = bool(item.rom_source or item.chd_sources or item.in_library)
        row = machine_row(item, statuses.get(item.name, "") if present else MISSING,
                          size=None if item.total_bytes else sizes.get(item.name, 0))
        row["files"] = [{"source": source, "destination": relpath,
                         "bytes": size, "bytes_human": human_bytes(size)}
                        for source, relpath, size in item.files()]
        row["library_files"] = _library_files(plan, item)
        row["signature"] = item.signature
        siblings = [other.name for other in plan.wanted
                    if other.cloneof and other.cloneof == (item.cloneof or item.name)
                    and other.name != name]
        row["parent"] = item.cloneof
        row["clones"] = sorted(siblings)[:40]
        return row
    return None


def missing_rows(plan, records=None, limit=400, priced=False):
    """The machines the selection wants that are not on disk, as something actionable.

    `plan.missing_roms` is just names; on a partial set that is a wall of 1,500 words
    nobody can do anything with. Grouped by genre, with a description and a size where
    one is known, it becomes a shopping list.
    """
    if plan is None:
        return {"total": 0, "bytes": 0, "genres": [], "rows": []}

    sizes = records or {}
    described = {entry["name"]: entry for entry in plan.absent}
    # What is nowhere, not what is merely absent from the source folder: a machine
    # already in the library is not something to go and fetch.
    wanted = set(plan.needed)
    genres = {}
    rows = []
    total_bytes = 0

    for name in sorted(wanted):
        record = described.get(name) or {}
        size = sizes.get(name) or 0
        total_bytes += size
        genre = record.get("genre") or "Unlisted"
        entry = genres.setdefault(genre, {"name": genre, "machines": 0, "bytes": 0})
        entry["machines"] += 1
        entry["bytes"] += size
        if len(rows) < limit:
            rows.append({"name": name,
                         "description": record.get("description") or name,
                         "genre": genre,
                         "category": record.get("category") or "",
                         "bytes": size,
                         "bytes_human": human_bytes(size) if size else ""})

    for entry in genres.values():
        entry["bytes_human"] = human_bytes(entry["bytes"])

    return {
        "total": len(wanted),
        "shown": len(rows),
        # Whether a download client answered. Without it the sizes are unknown; with
        # it, a zero really means the release does not carry those machines.
        "priced": priced,
        "bytes": total_bytes,
        "bytes_human": human_bytes(total_bytes),
        "disks": sorted(plan.missing_chds),
        "genres": sorted(genres.values(), key=lambda entry: -entry["bytes"]),
        "rows": rows,
    }


def built_from(config):
    if config is None:
        return None
    return {
        "rom_dir": config.rom_dir or "", "chd_dir": config.chd_dir or "",
        "copy_path": config.copy_path or "", "mame_version": config.mame_version or "",
        "allow_mature": bool(config.allow_mature),
        "blacklist_genres": sorted(config.blacklist_genres or []),
        "blacklist_categories": sorted(config.blacklist_categories or []),
        "blacklist_roms": sorted(config.blacklist_roms or []),
    }


def describe(plan, config, cache=None, sizes=None):
    """The plan as the page reads it.

    Memoised through `cache`: a plan does not change once built, so recomputing its
    totals, its folder breakdown and 270 formatted genre entries on every poll cost
    48 ms of pure repetition. The key covers the only things that do move -- which
    plan, whether it has been compared against the destination, and where it is
    going.
    """
    from ..plan import check_free_space

    # Not id(plan.sync): CPython reuses the id of a freed object, so a second
    # comparison could land on the same address and read back the first one's answer.
    compared = None if plan.sync is None else (len(plan.sync.actions),
                                               plan.sync.to_transfer)
    key = (id(plan), len(plan.items), len(plan.absent_items), compared,
           len(sizes or ()), tuple(sorted((plan.checked or {}).items())),
           config.copy_path if config else None)
    if cache is not None and cache.get("key") == key:
        return cache["payload"]

    space = check_free_space(plan, config.copy_path) if config else None
    payload = {
        "machines": len(plan.items),
        # What the selection asks for, as against what is here. On a fresh install the
        # first is zero and the second is the whole catalogue, and a page that only
        # knows the first has nothing to say.
        "wanted": len(plan.items) + len(plan.absent_items),
        "files": plan.file_count,
        "bytes": plan.total_bytes,
        "bytes_human": human_bytes(plan.total_bytes),
        # Counts, not the lists: on a partly-downloaded set these run to thousands of
        # names and the page polls this payload every few seconds. The Wanted page
        # asks for the detail when it needs it.
        "missing_roms": len(plan.needed),
        # What checking the library actually found, once something has looked.
        "states": {state: plan.checked[state] for state in verify.STATES
                   if (plan.checked or {}).get(state)},
        "missing_chds": len(plan.missing_chds),
        "missing_examples": plan.needed[:12],
        # In the source folder but not finished: a placeholder the torrent client
        # allocated, or a download still on its way. Not downloaded, and said so.
        "partial_roms": len(plan.partial_roms),
        # Wanted, not in the source folder, but already sitting at the destination.
        "in_library": sum(1 for item in plan.wanted if item.in_library),
        "mature_filtered": len(plan.mature_filtered),
        "folders": _folder_totals(plan),
        "genres": [dict(entry, bytes_human=human_bytes(entry["bytes"]))
                   for entry in plan.genres],
        "categories": [dict(entry, bytes_human=human_bytes(entry["bytes"]))
                       for entry in plan.categories],
    }
    _weigh_the_wanted(plan, payload, sizes)
    if plan.sync is not None:
        payload["sync"] = {
            kind: {"count": plan.sync.counts.get(kind, 0),
                   "bytes_human": human_bytes(plan.sync.bytes.get(kind, 0))}
            for kind in (sync.KEEP, sync.NEW, sync.UPDATE, sync.MOVE, sync.ORPHAN)
        }
        payload["to_transfer_human"] = human_bytes(plan.sync.to_transfer)
        # What the library actually holds, as against what this run can copy into it.
        # `machines` counts what is in the source folder, which on a library that was
        # built before Marquee ever ran is nothing at all -- and a sidebar reading
        # "0 games, 0 B" over ten thousand of them is simply wrong.
        held = [sync.KEEP, sync.MOVE, sync.UPDATE]
        payload["library_bytes_human"] = human_bytes(
            sum(plan.sync.bytes.get(kind, 0) for kind in held))
        payload["library_machines"] = len(
            {name for kind in held for name in plan.sync.machines(kind)})
        payload["orphan_examples"] = [action.relpath
                                      for action in plan.sync.of(sync.ORPHAN)[:40]]
    if space:
        needed, free = space
        payload["needed_human"] = human_bytes(needed)
        payload["free_human"] = human_bytes(free)
        payload["fits"] = needed <= free
        payload["short_human"] = human_bytes(max(needed - free, 0))

    if cache is not None:
        cache["key"], cache["payload"] = key, payload
    return payload


def _weigh_the_wanted(plan, payload, sizes):
    """What each genre and category would weigh if it were all here.

    `bytes` is what is on disk, which on a fresh install is nothing at all -- and a
    selection tree that answers "0 B" to every choice is one nobody can choose from.
    The release's own file table is where the other figure comes from.
    """
    by_genre, by_category, counted = {}, {}, set()
    for item in plan.wanted:
        # Each destination file once. A merged clone names its parent's disk on
        # purpose, and counting it twice inflates exactly the figure being decided on.
        weight = 0
        for _source, relpath, size in item.files():
            if relpath in counted:
                continue
            counted.add(relpath)
            weight += size
        if not (item.rom_source or item.chd_sources):
            weight = (sizes or {}).get(item.name, 0)
        by_genre[item.genre] = by_genre.get(item.genre, 0) + weight
        by_category[item.category] = by_category.get(item.category, 0) + weight
    for entry in payload["genres"]:
        entry["wanted_bytes"] = by_genre.get(entry["name"], 0)
        entry["wanted_bytes_human"] = human_bytes(entry["wanted_bytes"])
    for entry in payload["categories"]:
        entry["wanted_bytes"] = by_category.get(entry["name"], 0)
        entry["wanted_bytes_human"] = human_bytes(entry["wanted_bytes"])
    payload["wanted_bytes"] = sum(by_genre.values())
    payload["wanted_bytes_human"] = human_bytes(payload["wanted_bytes"])
    # And what the part that is not here yet weighs, which is the figure the front
    # page answers "what would fetching cost" with.
    needed = set(plan.needed)
    priced = sizes or {}
    missing = sum((item.total_bytes or priced.get(item.name, 0))
                  for item in plan.wanted if item.name in needed)
    payload["missing_bytes"] = missing
    payload["missing_bytes_human"] = human_bytes(missing) if missing else ""


def _folder_totals(plan):
    """Top-level destination folders with their size, biggest first."""
    totals = {}
    for item in plan.items:
        top = item.folder.split("/")[0]
        entry = totals.setdefault(top, {"name": top, "machines": 0, "bytes": 0})
        entry["machines"] += 1
        entry["bytes"] += item.total_bytes
    ordered = sorted(totals.values(), key=lambda entry: -entry["bytes"])
    for entry in ordered:
        entry["bytes_human"] = human_bytes(entry["bytes"])
    return ordered[:25]
