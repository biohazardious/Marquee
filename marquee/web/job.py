"""One background job at a time, with everything the browser needs to poll for.

The pipeline already reports through a Reporter and separates planning from copying, so
this is only bookkeeping: run the work on a thread, collect events, and expose a
snapshot the page can render.
"""
import os
import threading
import time
import traceback

from .. import backends, catalog, pipeline, sources, sync, verify
from ..errors import MarqueeError
from ..plan import human_bytes
from ..reporting import Reporter
from . import checks
from .views import built_from, describe

MAX_EVENTS = 2000


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
        # When the plan in hand was started, to tell settings saved after it.
        self.planned_at = None
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
            self.planned_at = time.time()

        def work():
            reporter = QueueReporter(self)
            plan, resolution = pipeline.build_plan(config, options, reporter)
            self._restore_check(plan, config, resolution, reporter)
            with self._lock:
                self.plan, self.resolution = plan, resolution

        self._start(work, "planning", "planned", prepare=prepare)

    def _restore_check(self, plan, config, resolution, reporter):
        """Hand the last check's verdicts to a plan built since, where they still hold."""
        if plan.sync is None:
            return
        counts, checked_at = checks.restore(plan, config.copy_path,
                                            getattr(resolution, "xml_version", None))
        if not counts:
            return
        plan.checked, plan.checked_at = counts, checked_at
        with self._lock:
            self.checked = counts
        reporter.info(f"Kept the library check from "
                      f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(checked_at or 0))}: "
                      + verify.summarise(counts))
        if counts.get(verify.STALE) or counts.get(verify.DAMAGED):
            # Same as after a check: those files have to be replaced, and the diff
            # that was just taken from names and sizes calls them fine.
            try:
                plan.sync = pipeline.compare_destination(plan, config, reporter)
            except MarqueeError as error:
                reporter.warn(f"Could not re-check what the library holds: {error}")

    def start_check(self, xml_file, deep=False, pieces=None):
        """Check what the library holds against the release it is supposed to be.

        A separate job rather than part of planning: it reads every zip in the library
        and the whole XML again, which is a minute or two, and it is only worth doing
        when something has actually changed.
        """
        if self.plan is None:
            raise MarqueeError("Build a plan first, so there is something to check.")
        if not xml_file:
            raise MarqueeError("The release's XML is not on disk to check against.")
        plan, config = self.plan, self.config
        remote = backends.is_remote(config.copy_path)
        version = getattr(self.resolution, "xml_version", None)
        # Taken now, from the diff the check reads by: a stale verdict re-diffs the
        # plan afterwards, and what is kept has to name the files as they were read.
        held = checks.snapshot_sizes(plan)

        def work():
            reporter = QueueReporter(self)
            backend, opener = None, None
            if remote:
                # Read back through the same backend the transfer writes with: a
                # share on the console is checked in place, one ranged read per zip.
                backend = backends.for_destination(config.copy_path, reporter=reporter)
                if not backend.READS_BACK:
                    backend.close()
                    raise MarqueeError("Files on an FTP library cannot be read back "
                                       "to check them; SMB and SFTP can.")
                opener = backend.open_read
            try:
                _check(backend, opener)
            finally:
                if backend is not None:
                    backend.close()

        def _check(backend, opener):
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
            where, moved, sizes = {}, {}, {}
            if plan.sync is not None:
                for action in plan.sync.actions:
                    if action.size and action.kind in (sync.KEEP, sync.UPDATE, sync.MOVE):
                        sizes[action.relpath] = action.size
                    if action.kind != sync.MOVE:
                        continue
                    if action.machine and action.relpath.endswith(".zip"):
                        where[action.machine] = action.from_relpath
                    elif action.relpath.endswith(".chd"):
                        moved[action.relpath] = action.from_relpath
            # A disk's finished copy in the download folder, to hold a zero-filled
            # sample against. Only finished ones are in chd_sources at all.
            disk_sources = {}
            for item in here:
                by_name = {os.path.basename(path).lower(): path for path in item.chd_sources}
                for relpath in list(item.wanted_paths())[1:]:
                    source = by_name.get(os.path.basename(relpath).lower())
                    if source:
                        disk_sources[relpath] = source
            by_relpath = {}
            if deep and pieces is not None:
                reporter.stage("Reading the torrents' piece hashes...")
                try:
                    table = pieces() or {}
                except MarqueeError as error:
                    table = {}
                    reporter.warn(f"Could not read piece hashes from the download "
                                  f"client: {error}")
                for item in here:
                    for relpath in list(item.wanted_paths())[1:]:
                        key = (os.path.basename(relpath).lower(), sizes.get(relpath))
                        if key in table:
                            by_relpath[relpath] = table[key]
                reporter.info(f"{len(by_relpath):,} disks can be hashed against their "
                              f"torrent; this reads every one of them in full.")
            counts = verify.check_library(
                here, manifests, config.copy_path, reporter,
                should_continue=lambda: not self._cancel.is_set(), where=where,
                opener=opener, moved=moved, sources=disk_sources, pieces=by_relpath)
            checked_at = time.time()
            with self._lock:
                self.checked = counts
                # On the plan as well: the page's description of it is memoised, and
                # the check changes what that description should say.
                plan.checked = counts
                plan.checked_at = checked_at
            checks.save(plan, here, config.copy_path, version, held, deep=deep)
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

        # Whatever this run writes has not been read by anything yet.
        written = set()
        if self.plan.sync is not None:
            for kind in (sync.NEW, sync.UPDATE, sync.MOVE):
                written.update(self.plan.sync.machines(kind))

        def work():
            checks.forget(written)
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
        self.add_event("warn", "Stopping after the current machine...")
        return True

    # -- what the page reads ------------------------------------------------ #

    def snapshot(self, after=0, sizes=None, plan_rev=None):
        """The job as the page reads it.

        Only the copy of the fields happens under the lock. Describing the plan can
        mean a disk_usage call and, before a comparison, a scan of every destination
        folder -- and while that ran under the lock, the worker's every log line and
        progress tick waited on it.

        Every key is always present, None when there is nothing: the page merges
        snapshots, so a key that was simply left out kept its previous run's value,
        and a re-plan showed the old transfer's summary until the new one finished.

        The one exception is the plan, which is most of the answer and changes only
        when something is planned or compared. `plan_rev` is the revision the page
        already holds; when it is still current the plan is left out and
        `plan_rev` says so. A plan that went away is still sent, as None.
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
        payload["plan_rev"] = None
        if plan is not None:
            described_plan = describe(plan, config, described, sizes)
            # What this plan was actually built from, so the page can tell whether
            # its own form has moved on rather than guessing from a flag.
            origin = built_from(config)
            rev = f"{described_plan['rev']}:{hash(repr(sorted(origin.items())))}"
            payload["plan_rev"] = rev
            if plan_rev == rev:
                del payload["plan"]
            else:
                payload["plan"] = dict(described_plan, built_from=origin)
        if resolution is not None:
            payload["resolution"] = {
                "xml_file": resolution.xml_file,
                "xml_version": resolution.xml_version,
                "catlist_file": resolution.catlist_file,
                "catlist_version": resolution.catlist_version,
                "rom_dir": resolution.rom_dir,
                "chd_dir": resolution.chd_dir,
                "chd_set_found": getattr(resolution, "chd_set_found", None),
                "machines_read": resolution.machine_count,
                "filtered": [
                    {"reason": reason, "label": label,
                     "count": resolution.filtered.get(reason, 0)}
                    for reason, label in catalog.REASONS
                    if resolution.filtered.get(reason)],
                "machines_kept": resolution.kept_count,
                "genres": resolution.genres,
                "destination_exists": resolution.destination_exists,
                "console": getattr(resolution, "console", None),
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
