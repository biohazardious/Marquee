"""What the app can do, as opposed to how HTTP reaches it.

`Application` holds the whole of the server's state -- the running job, the artwork
cache, the release listing, the upgrade preview -- and every action the page can ask
for. It knows nothing about requests, headers or status codes, which is what lets it
be driven from a test without a socket.
"""
import threading
import time
from dataclasses import replace

from .. import art, fetch, pipeline, sync
from .. import config as configuration
from ..errors import MarqueeError
from ..plan import human_bytes
from .downloads import DownloadsMixin
from .fetches import FetchesMixin
from .reclaim import ReclaimMixin
from .job import Job
from .views import built_from
from .releases import ReleasesMixin, build_label, newest_version
from .settings import SettingsMixin
from .tasks import TasksMixin

# The release helpers lived here before the split, and are still found here.
__all__ = ["Application", "build_label", "newest_version"]


class Application(SettingsMixin, DownloadsMixin, FetchesMixin, ReclaimMixin, ReleasesMixin,
                  TasksMixin):
    """Everything the request handler needs, kept out of the handler class."""

    def __init__(self, settings_path, token=None):
        # Resolved once, here. `--config` may name the directory holding settings.ini
        # -- which is what the container mounts -- and reading went through
        # configuration.load(), which resolves it, while saving opened the raw path and
        # found a directory.
        self.settings_path = configuration.settings_file(settings_path)
        self.token = token
        self.job = Job()
        # A cold walk of 47,000 files takes about ten seconds, so it runs in the
        # background and the page picks it up when it lands.
        self.survey = {"running": False, "for": None, "data": None, "error": None}
        self.versions = {"running": False, "data": None, "error": None}
        # The Pleasuredome index changes about once a month, so it is fetched on
        # demand and then remembered rather than polled.
        self.releases = {"running": False, "data": None, "error": None,
                         "fetched_at": None}
        self.art = {"running": False, "kind": art.DEFAULT_KIND, "done": 0, "total": 0,
                    "summary": None, "error": None, "finished_at": 0}
        self.upgrade = {"running": False, "data": None, "error": None, "to": None}
        # What the published set says every machine weighs, learned from the torrent's
        # own metadata without downloading a byte of content. Until this is primed a
        # machine that is not on disk has no size at all, and "1,786 games" is not a
        # number anyone can decide on.
        self.catalogue = {"running": False, "error": None, "fetched_at": None,
                          "roms": {}, "disks": {}, "version": None,
                          "rom_files": 0, "disk_files": 0}
        self._sizes = {"key": None, "plan": None, "data": {}}
        # Read-modify-write of settings.ini happens from three pages; two saves that
        # overlap used to have the second silently drop the first one's fields.
        self._settings_lock = threading.Lock()
        # Guards the "running" flag of every background task below. Each checked it
        # and set it as two steps, so two requests close together -- a double click,
        # or every open tab's poll asking for the release listing -- both started one.
        self._task_lock = threading.Lock()
        # Download clients by (address, user, password); see client().
        self._clients = {}
        self._config_cache = {"stamp": None, "config": None}
        self._torrent_sizes = {"at": 0.0, "data": {}}
        # The status bar on every page asks for the queue; one answer serves all of
        # them for a few seconds rather than one qBittorrent call per tab per tick.
        self._queue_cache = {"at": 0.0, "data": None}
        # Whether a newer Marquee has been tagged. Asked of GitHub twice a day at
        # most; nothing is downloaded, and MARQUEE_NO_UPDATE_CHECK turns it off.
        self.update = {"running": False, "latest": None, "error": None, "checked_at": None}
        self._read_catalogue()

    def autoplan(self):
        """Build a plan at startup, so a restart does not leave an empty page.

        This is a service: it gets restarted for an image update, or when the host
        comes back, and until something has been planned the library, the selection
        tree and the wanted list all show nothing. Nobody should have to remember to
        press a button before the pages mean anything.

        Skipped when the settings are not complete, which is exactly what a first run
        looks like -- there is nothing to plan yet and the page says so.
        """
        try:
            config = self.current_config()
        except MarqueeError:
            return {"started": False, "reason": "the settings could not be read"}
        if config.missing_paths:
            return {"started": False, "reason": "the settings are not complete yet"}
        if self.job.busy:
            return {"started": False, "reason": "a job is already running"}
        self.job.start_plan(config, pipeline.SourceOptions(
            newest_published=self.newest_published))
        return {"started": True}

    # -- artwork ------------------------------------------------------------ #

    def art_payload(self):
        kind = self.art["kind"]
        count, size = art.cache_totals(kind)
        return {**self.art, "cached": count, "bytes": size,
                "bytes_human": human_bytes(size), "kinds": list(art.KINDS)}

    def start_art(self, body):
        if self.art["running"]:
            raise MarqueeError("Artwork is already downloading.")
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is a library to illustrate.")

        kind = (body or {}).get("kind") or art.DEFAULT_KIND
        if kind not in art.KINDS:
            raise MarqueeError(f"Unknown artwork kind: {kind}")
        # Every game the Library page shows, not only the ones in the source folder:
        # with the torrent folder empty and 10,022 games already on the console it
        # fetched nothing, and reported that as "Artwork done".
        descriptions = list(dict.fromkeys(item.description for item in plan.wanted))
        if not descriptions:
            raise MarqueeError("The plan has no games to find pictures for.")
        # Stop only flips the flag; the worker notices a moment later. A start in that
        # moment used to pass the guard, and the old worker's exit then switched the
        # new one off. Each run has a generation and only reads and writes its own.
        with self._task_lock:
            if self.art["running"]:
                raise MarqueeError("Artwork is already downloading.")
            generation = self.art.get("generation", 0) + 1
            self.art.update(generation=generation, running=True, kind=kind, done=0,
                            total=len(descriptions), summary=None, error=None)

        def mine():
            return self.art["generation"] == generation

        def work():
            try:
                summary = art.download(
                    descriptions, kind,
                    on_progress=lambda done, total: mine() and self.art.update(
                        done=done, total=total),
                    should_continue=lambda: self.art["running"] and mine())
                if mine():
                    self.art["summary"] = summary
                    self.art["error"] = None
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                if mine():
                    self.art["error"] = f"{type(error).__name__}: {error}"
            finally:
                if mine():
                    self.art["running"] = False
                    self.art["finished_at"] = time.time()

        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "total": len(descriptions), "kind": kind}

    def stop_art(self, _body=None):
        self.art["running"] = False
        return {"stopped": True}

    def start_versions(self, _body=None):
        """Fetch the list of MAME versions in the background; the API is rate limited."""
        if self.versions["running"] or self.versions["data"]:
            return {"started": False}

        def work():
            self.versions["data"] = fetch.version_catalogue()
            self.versions["error"] = None

        return {"started": self._start(self.versions, work)}

    def plan(self, body):
        config = self.config_from(body)
        if config.missing_paths:
            raise MarqueeError(f"Still missing: {', '.join(config.missing_paths)}")
        options = pipeline.SourceOptions(
            version=body.get("mame_version") or None,
            xml=body.get("xml") or None,
            catlist=body.get("catlist") or None,
            offline=bool(body.get("offline")),
            fetch_support_files=bool(body.get("fetch_support_files")),
            ignore_version_mismatch=bool(body.get("ignore_version_mismatch")),
            refresh_cache=bool(body.get("refresh_cache")),
            source_progress=lambda: self.source_progress(config),
            newest_published=self.newest_published)
        self.job.start_plan(config, options)
        return {"started": "plan"}

    def check(self, _body=None):
        """Check the library against the release, rather than trusting the filenames.

        The only honest answer to "is this still the right file". A name match says
        nothing: MAME replaces bad dumps and renames chips between releases, and a
        library that never looks inside carries those changes for ever.
        """
        resolution = self.job.resolution
        xml_file = getattr(resolution, "xml_file", None)
        deep = bool((_body or {}).get("deep"))
        config = self.current_config()
        self.job.start_check(xml_file, deep=deep,
                             pieces=(lambda: self.disk_pieces(config)) if deep else None)
        return {"started": "check", "deep": deep}

    # Settings a transfer reads as it goes, rather than ones that shaped the plan.
    COPY_ONLY = ("hardlink", "write_gamelist", "copy_artwork")

    def copy(self, body):
        if self.job.plan is None:
            raise MarqueeError("Build a plan first, so there is something to copy.")
        planned = self.job.config
        if planned is not None and not self.job.busy:
            current = self.current_config()
            # The plan is run with the configuration it was built from. Settings
            # saved since then that would have shaped it -- a genre left out, parents
            # only -- make it the wrong plan, and nothing on the server said so.
            if self._saved_since(self.job.planned_at) \
                    and built_from(current) != built_from(planned):
                raise MarqueeError("The settings were saved after this plan was built. "
                                   "Build the plan again before transferring.")
            # These only decide how files are written, and the saved ones are what
            # the page shows: hardlink ticked and saved used to copy in full anyway.
            # replace(), not with_overrides(): None is a value here -- a hardlink
            # set back to automatic -- and with_overrides() would drop it.
            self.job.config = replace(
                planned, **{key: getattr(current, key) for key in self.COPY_ONLY})
        version = (body.get("mame_version")
                   or getattr(self.job.config, "mame_version", None)
                   or getattr(self.job.resolution, "xml_version", None))
        self.job.start_copy(delete_orphans=bool(body.get("delete_orphans")),
                            mame_version=version)
        return {"started": "copy"}

    def start_survey(self, body):
        """Count and measure the source and destination, in the background."""
        config = self.config_from(body)
        key = (config.rom_dir, config.chd_dir, config.copy_path)
        if self.survey["running"] or self.survey["for"] == key:
            return {"started": False}

        def work():
            # A flat romset keeps ROMs and CHDs in one folder; walking it twice would
            # double a ten second scan and show the same figure twice.
            paths = {"roms": config.rom_dir}
            if config.chd_dir and config.chd_dir != config.rom_dir:
                paths["chds"] = config.chd_dir
            paths["destination"] = None if config.is_remote else config.copy_path
            data = sync.survey(paths)
            for entry in data.values():
                entry["bytes_human"] = human_bytes(entry["bytes"])
            self.survey.update(data=data, error=None, **{"for": key})

        return {"started": self._start(self.survey, work)}

    def cancel(self, _body):
        return {"cancelling": self.job.cancel()}
