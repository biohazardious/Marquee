"""What the app can do, as opposed to how HTTP reaches it.

`Application` holds the whole of the server's state -- the running job, the artwork
cache, the release listing, the upgrade preview -- and every action the page can ask
for. It knows nothing about requests, headers or status codes, which is what lets it
be driven from a test without a socket.
"""
import json
import os
import posixpath
import threading
import time
import urllib.request

from .. import (acquire, acquisition, art, atomic, backends, download, fetch, indexers, manifest,
                pipeline, sources, sync)
from .. import __version__
from .. import config as configuration
from ..errors import MarqueeError
from ..plan import human_bytes
from .job import Job, filtered, missing_rows


class Application:
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
        self.survey = {"running": False, "for": None, "data": None}
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
        self._config_cache = {"stamp": None, "config": None}
        self._torrent_sizes = {"at": 0.0, "data": {}}
        # The status bar on every page asks for the queue; one answer serves all of
        # them for a few seconds rather than one qBittorrent call per tab per tick.
        self._queue_cache = {"at": 0.0, "data": None}
        # Whether a newer Marquee has been tagged. Asked of GitHub twice a day at
        # most; nothing is downloaded, and MARQUEE_NO_UPDATE_CHECK turns it off.
        self.update = {"running": False, "latest": None, "error": None, "checked_at": None}
        self._read_catalogue()

    def current_config(self):
        """The settings as they are on disk right now.

        Cached on the file's mtime and size: one /api/state used to parse the file
        five times, and the blacklists inside it are thousands of names.
        """
        try:
            stat = os.stat(self.settings_path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        cache = self._config_cache
        if stamp is not None and cache["stamp"] == stamp and cache["config"] is not None:
            return cache["config"]
        config = configuration.load(self.settings_path)
        self._config_cache = {"stamp": stamp, "config": config}
        return config

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
        self.job.start_plan(config, pipeline.SourceOptions())
        return {"started": True}

    def destination_payload(self, copy_path=None):
        """What the destination says it already holds, if anything."""
        record = manifest.read(copy_path or self.current_config().copy_path)
        described = manifest.describe(record)
        if described:
            described["settings"] = manifest.settings_from(record)
            described["history"] = (record.get("history") or [])[-5:]
        return described

    # -- acquisition -------------------------------------------------------- #

    def client(self, overrides=None):
        """A download client built from the saved settings, or whatever is on the form.

        Settings can be tested before they are saved, which is the only way a Test
        button is any use.
        """
        config = self.current_config()
        values = overrides or {}
        url = values.get("download_client") or config.download_client
        if not url:
            raise MarqueeError("No download client is configured.")
        password = values.get("download_password")
        if not password or set(password) == {"\u2022"}:
            password = config.download_password
        return download.for_url(
            url,
            username=values.get("download_username") or config.download_username,
            password=password)

    def test_client(self, body):
        """Reach the client, and say where its downloads land -- in its own terms and
        in this app's.

        The two are often different paths to one folder (qBittorrent in one
        container, this app in another), and a download that lands somewhere this
        app cannot see finishes and then goes nowhere. Saying so here, before
        anything is fetched, is the whole point of a Test button.
        """
        body = body or {}
        client = self.client(body)
        info = client.test()
        landing = self._landing(client, info)
        mappings_text = body.get("remote_path_mappings")
        if mappings_text is None:
            mappings_text = self.current_config().remote_path_mappings
        try:
            mappings = acquisition.parse_mappings(mappings_text)
        except MarqueeError:
            mappings = []
        download_dir = body.get("download_dir")
        if download_dir is None:
            download_dir = self.current_config().download_dir
        download_dir = (download_dir or "").strip() or None
        # Where the existing Marquee torrents are found from here, by name -- which is
        # what a fresh download will be found by too, and the reason a mapping is
        # normally not needed at all.
        found_by_name = None
        try:
            for entry in (client.status() or []):
                if entry.get("category") != acquisition.CATEGORY:
                    continue
                pair = acquisition.inferred_mapping(
                    entry.get("content_path") or entry.get("save_path"), download_dir)
                if pair:
                    found_by_name = pair
                    break
        except (MarqueeError, AttributeError):
            pass
        local = (acquisition.locate(landing, mappings, download_dir) if landing else None)
        if local == landing and found_by_name and landing.startswith(found_by_name[0]):
            local = os.path.join(found_by_name[1], landing[len(found_by_name[0]):].strip("/"))
        visible = bool(local) and (os.path.isdir(local) or os.path.isdir(os.path.dirname(local)))
        category_path = self._category_path(client)
        message = (f"qBittorrent {info['app_version']} (Web API {info['api_version']}). "
                   f"New downloads land in {landing or 'its default folder'}")
        if category_path:
            message += " (the marquee category's own folder)"
        if landing and local != landing:
            message += f", which this app sees as {local}"
        if found_by_name:
            message += (f". Its downloads are found here by name: qBittorrent's "
                        f"{found_by_name[0]} is this app's {found_by_name[1]}, no mapping needed")
        if landing:
            message += (". Visible from here." if visible else
                        ". NOT visible from here: mount that folder into this container, "
                        "or add a remote path mapping for it.")
        return {"ok": True, "message": message, "details": info,
                "landing": landing, "local": local, "visible": visible,
                "category_path": category_path, "default_path": info.get("save_path") or "",
                "found_by_name": found_by_name}

    @staticmethod
    def _category_path(client):
        try:
            return ((client.categories() or {}).get(acquisition.CATEGORY) or {}).get("savePath") or ""
        except MarqueeError:
            return ""

    def reset_category(self, body):
        """Send the marquee category back to qBittorrent's default folder.

        An earlier Marquee created the category with *this* container's download
        path as its save path -- meaningless to a qBittorrent in another container,
        and every download into it failed on the spot. One button undoes that.
        """
        client = self.client(body or {})
        client.set_category_path(acquisition.CATEGORY, "")
        return {"ok": True, "message": "The marquee category now uses qBittorrent's "
                                       "default download folder."}

    @staticmethod
    def _landing(client, info):
        """Where a new Marquee torrent would be saved, as qBittorrent sees it: the
        marquee category's folder when it has one, else the client's default."""
        try:
            category = (client.categories() or {}).get(acquisition.CATEGORY) or {}
        except MarqueeError:
            category = {}
        return category.get("savePath") or info.get("save_path") or ""

    def test_destination(self, body):
        """Reach the library and read its top level, before anything is planned.

        The folder picker can only walk this machine's disks; a share on the console
        cannot be browsed, and the Browse button used to wander into a local folder
        literally named "smb:" instead of saying so. This is the answer for a remote
        library: connect, list, say what is there.
        """
        copy_path = ((body or {}).get("copy_path") or self.current_config().copy_path
                     or "").strip()
        if not copy_path:
            raise MarqueeError("No library path to test.")
        remote = backends.is_remote(copy_path)
        backend = None
        try:
            backend = backends.for_destination(copy_path)
            names = backend.probe()
        except (MarqueeError, OSError, ValueError, ConnectionError) as error:
            raise MarqueeError(f"Could not reach {copy_path}: {error}") from error
        except Exception as error:  # noqa: BLE001 - pysmb/paramiko raise their own
            raise MarqueeError(f"Could not reach {copy_path}: "
                               f"{type(error).__name__}: {error}") from error
        finally:
            if backend is not None:
                backend.close()
        folders = [name for name in names if "." not in name][:6]
        managed = sum(1 for name in names if backends.is_managed(name))
        where = (f"{backend.server}, share {backend.share_name}" if remote and hasattr(backend, "share_name")
                 else copy_path)
        message = (f"Reached {where}: {len(names):,} entries at the top"
                   + (f" ({', '.join(folders)}{'…' if len(names) > 6 else ''})" if folders else "")
                   + (f", {managed:,} loose ROM files" if managed else "") + ".")
        return {"ok": True, "remote": remote, "entries": len(names), "sample": folders,
                "message": message,
                "writable": None if remote else os.access(copy_path, os.W_OK)}

    # -- this program's own version ------------------------------------------ #

    UPDATE_TTL = 12 * 60 * 60
    UPDATE_RETRY = 60 * 60
    TAGS_URL = "https://api.github.com/repos/biohazardious/Marquee/tags?per_page=30"

    def about(self):
        """Version, build and whether something newer is out -- for the sidebar."""
        self._watch_updates()
        latest = self.update["latest"]
        return {
            "version": __version__,
            "build": build_label(),
            "update": {
                "latest": latest,
                "newer": bool(latest and _version_key(latest) > _version_key(__version__)),
                "url": f"https://github.com/biohazardious/Marquee/releases/tag/v{latest}"
                       if latest else None,
                "error": self.update["error"],
            },
        }

    def _watch_updates(self):
        if os.environ.get("MARQUEE_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
            return
        age = time.time() - (self.update["checked_at"] or 0)
        due = self.update["checked_at"] is None or age > (
            self.UPDATE_RETRY if self.update["error"] else self.UPDATE_TTL)
        if self.update["running"] or not due:
            return

        def work():
            try:
                request = urllib.request.Request(
                    self.TAGS_URL, headers={"User-Agent": f"Marquee/{__version__}",
                                            "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    tags = json.loads(response.read().decode("utf-8"))
                self.update["latest"] = newest_version(
                    entry.get("name", "") for entry in tags if isinstance(entry, dict))
                self.update["error"] = None
            except Exception as error:  # noqa: BLE001 - a failed check is a note, not a fault
                self.update["error"] = f"{type(error).__name__}: {error}"
            finally:
                self.update["checked_at"] = time.time()
                self.update["running"] = False

        self.update["running"] = True
        threading.Thread(target=work, daemon=True).start()

    def refresh_releases(self, _body=None):
        def work():
            try:
                found = indexers.default().releases()
                self.releases["data"] = [_release_payload(r) for r in found]
                self.releases["error"] = None
            except Exception as error:  # noqa: BLE001 - anything: a truncated read
                # from the index is not a MarqueeError, and an error that escaped
                # here left "running" false with no data, which release_watch read as
                # "never asked" and asked again on every poll.
                self.releases["error"] = (str(error) if isinstance(error, MarqueeError)
                                          else f"{type(error).__name__}: {error}")
            finally:
                self.releases["fetched_at"] = time.time()
                self.releases["running"] = False

        if not self.releases["running"]:
            self.releases["running"] = True
            threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    # How long a release listing is trusted before it is fetched again. Pleasuredome
    # publishes about monthly, so this is about noticing within a day, not polling.
    RELEASE_TTL = 6 * 60 * 60
    # After a failure: soon enough to recover from a blip, not so soon as to hammer.
    RELEASE_RETRY = 10 * 60

    def release_watch(self):
        """Whether anything newer than this library has been published.

        Cheap enough to answer on every poll: it reads the listing already in memory,
        and only kicks off a refresh when that listing has gone stale.
        """
        age = time.time() - (self.releases["fetched_at"] or 0)
        due = self.releases["data"] is None and self.releases["fetched_at"] is None
        due = due or age > (self.RELEASE_RETRY if self.releases["error"] else self.RELEASE_TTL)
        if not self.releases["running"] and due:
            self.refresh_releases()

        versions = sorted({entry["version"]
                           for entry in (self.releases.get("data") or [])
                           if entry["kind"] == "roms" and entry["full_set"]},
                          reverse=True)
        newest = versions[0] if versions else None
        record = manifest.read(self.current_config().copy_path)
        here = (record or {}).get("mame_version") or self.current_config().mame_version
        return {"newest": newest, "library": here,
                "newer": bool(newest and here and _version_key(newest) > _version_key(here))}

    def releases_payload(self):
        # Fetched once, lazily. It is a static page that changes about once a month,
        # so asking for it on the first read beats making the user press a button to
        # find out what MAME releases exist.
        if self.releases["data"] is None and not self.releases["running"] \
                and not self.releases["error"]:
            self.refresh_releases()
        return dict(self.releases)

    # -- the published set, as a catalogue ---------------------------------- #

    def _ensure_release(self, release, client):
        """The release in the client with its file table, having fetched no content.

        Added stopped with `stopCondition=MetadataReceived`: qBittorrent pulls the
        torrent's own metadata -- 44,166 names and sizes -- and stops before a byte of
        content. That file table is the price list for everything the set contains.

        No save path is given. Where downloads go is the download client's own
        business -- it is configured there, per category, and handing it a path as
        *this* process sees it would scatter the set somewhere nobody meant whenever
        the two do not share a filesystem. What comes back instead is `content_path`,
        which says where the files really are.

        Returns (infohash, files, added_now, ours). A torrent is ours when it sits
        in this tool's category -- this call filed it there, or an earlier one did.
        Anything else in the client is the user's own download, possibly of the
        whole set, and may only ever have files added to it.
        """
        infohash = release.infohash
        entry = client.one(infohash)
        added = entry is None
        if added:
            client.ensure_category(acquisition.CATEGORY)
            infohash = client.add(release.magnet_uri(),
                                  category=acquisition.CATEGORY,
                                  tags=[release.kind, release.version], stopped=True)
            client.wait_for_metadata(infohash)
        ours = added or (entry or {}).get("category") == acquisition.CATEGORY
        files = client.files(infohash)
        if not files:
            raise MarqueeError(
                f"{release.name}: the download client has no file list for it yet.")
        return infohash, files, added, ours

    @staticmethod
    def _disarm(client, infohash, files):
        """Leave a set we added only to read holding the smallest file it has.

        It arrives with all 44,166 of them selected and it is stopped, so it does
        nothing on its own -- but anyone who presses play in qBittorrent then gets 1.3
        TB. qBittorrent will not accept a torrent with nothing selected, so the least
        it can be left holding is one file, chosen for being the smallest.
        """
        if not files:
            return
        smallest = min(files, key=lambda entry: entry["size"])
        client.narrow(infohash, [smallest["index"]])

    CATALOGUE_FILE = "catalogue.json"

    def _catalogue_path(self):
        return os.path.join(sources.cache_dir(), self.CATALOGUE_FILE)

    def _read_catalogue(self):
        """Reload the price list written by a previous run.

        Learning it costs a round trip to the download client and about half a minute
        of waiting for two torrents' metadata. Doing that again on every container
        restart -- which, in a container, is often -- would be silly.
        """
        try:
            with open(self._catalogue_path(), encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict) or not isinstance(stored.get("roms"), dict):
            return
        self.catalogue.update({
            "roms": stored.get("roms") or {}, "disks": stored.get("disks") or {},
            "version": stored.get("version"), "fetched_at": stored.get("fetched_at"),
            "rom_files": len(stored.get("roms") or {}),
            "disk_files": len(stored.get("disks") or {})})

    def _write_catalogue(self):
        try:
            atomic.write_json(self._catalogue_path(),
                              {key: self.catalogue[key]
                               for key in ("roms", "disks", "version", "fetched_at")})
        except OSError:
            # An unwritable cache is not a reason to lose the catalogue we just read.
            pass

    def prime_catalogue(self, body=None):
        """Read the sizes of everything in the release, in the background.

        Deliberately an action and not something a page load triggers: it puts a
        torrent in somebody's download client, and that is not a side effect anyone
        should discover.
        """
        if self.catalogue["running"]:
            raise MarqueeError("Already reading the release.")

        def work():
            try:
                client = self.client()
                release = self._rom_release()
                infohash, files, added, _ours = self._ensure_release(release, client)
                if added:
                    self._disarm(client, infohash, files)
                roms = {}
                for entry in files:
                    stem = entry["path"].rsplit("/", 1)[-1]
                    if stem.lower().endswith(acquire.ROM_SUFFIX):
                        roms[stem[:-len(acquire.ROM_SUFFIX)]] = entry["size"]

                disks = {}
                try:
                    chd = self._chd_release()
                except MarqueeError:
                    chd = None
                if chd is not None:
                    chd_hash, chd_files, chd_added = self._ensure_release(chd, client)
                    if chd_added:
                        self._disarm(client, chd_hash, chd_files)
                    for entry in chd_files:
                        parts = entry["path"].split("/")
                        if len(parts) >= 2 and parts[-1].lower().endswith(acquire.CHD_SUFFIX):
                            disks["/".join(parts[-2:])] = entry["size"]

                self.catalogue.update({"roms": roms, "disks": disks,
                                       "version": release.version, "error": None,
                                       "rom_files": len(roms), "disk_files": len(disks),
                                       "fetched_at": time.time()})
                self._sizes = {"key": None, "plan": None, "data": {}}
                self._write_catalogue()
            except MarqueeError as error:
                self.catalogue["error"] = str(error)
            finally:
                self.catalogue["running"] = False

        self.catalogue["running"] = True
        self.catalogue["error"] = None
        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def catalogue_payload(self):
        return {"running": self.catalogue["running"], "error": self.catalogue["error"],
                "version": self.catalogue["version"],
                "roms": self.catalogue["rom_files"],
                "disks": self.catalogue["disk_files"],
                "priced": bool(self.catalogue["roms"]),
                "client": bool(self.current_config().download_client)}

    def release_sizes(self):
        """{machine: bytes it would take to have it}, from the published set.

        A machine's weight is its zip plus the disks it needs, and a clone's disk
        lives in its parent's folder -- so the answer needs the plan, which is what
        knows the disk names. Memoised: the library page asks on every keystroke.
        """
        plan = self.job.plan
        roms, disks = self.catalogue["roms"], self.catalogue["disks"]
        if plan is None or not roms:
            return roms or {}
        left_out = getattr(plan, "left_out", None)
        key = (len(plan.items), len(left_out.wanted) if left_out else 0,
               self.catalogue["fetched_at"])
        # The plan itself is held, not id(plan): CPython reuses a freed address, and a
        # re-plan of the same size could have read back the old plan's sizes.
        if self._sizes["key"] == key and self._sizes["plan"] is plan:
            return self._sizes["data"]

        # The left-out catalogue too: it is browsed in the same tree and a tree whose
        # every row reads 0 B is not worth drawing.
        every = plan.wanted + (left_out.wanted if left_out else [])
        sizes = {}
        for item in every:
            total = roms.get(item.name, 0)
            for disk in item.disks:
                for owner in (item.chd_name, item.name, item.cloneof):
                    if owner and f"{owner}/{disk}{acquire.CHD_SUFFIX}" in disks:
                        total += disks[f"{owner}/{disk}{acquire.CHD_SUFFIX}"]
                        break
            if total:
                sizes[item.name] = total
        self._sizes = {"key": key, "plan": plan, "data": sizes}
        return sizes

    def torrent_sizes(self):
        """{machine name: bytes} from the release torrents the client already knows.

        The sizes are what make a missing list answerable -- "1,786 games" says
        nothing, "1,786 games, 41 GB" is a decision. Read-only: the torrents are
        inspected, never touched.
        """
        config = self.current_config()
        if not config.download_client:
            return {}
        # Remembered for a minute: the Wanted page asks twice per visit, and each
        # answer is a status call plus a 44,000-row file table per release torrent.
        if time.time() - self._torrent_sizes["at"] < 60:
            return self._torrent_sizes["data"]
        try:
            handle = self.client()
            entries = handle.status()
        except MarqueeError:
            self._torrent_sizes.update(at=time.time(), data={})
            return {}

        sizes = {}
        for entry in entries:
            if " roms " not in entry["name"].lower():
                continue
            try:
                for item in handle.files(entry["hash"]):
                    stem = item["path"].rsplit("/", 1)[-1]
                    if stem.lower().endswith(".zip"):
                        sizes[stem[:-4]] = item["size"]
            except MarqueeError:
                continue
        self._torrent_sizes.update(at=time.time(), data=sizes)
        return sizes

    def missing_payload(self):
        plan = self.job.plan
        if plan is None:
            return {"total": 0, "bytes": 0, "genres": [], "rows": []}
        # The catalogue, if it has been read, is the better answer: it prices every
        # machine in the release rather than only the ones a torrent in the client
        # happens to cover.
        sizes = self.release_sizes() or self.torrent_sizes()
        payload = missing_rows(plan, sizes, priced=bool(sizes))
        payload["releases"] = self.releases.get("data")
        # Three different answers, and they need different words: no client at all,
        # a client that does not hold the release, or a real figure.
        payload["client"] = bool(self.current_config().download_client)
        return payload

    # -- version upgrades ---------------------------------------------------- #

    def upgrade_payload(self):
        record = manifest.read(self.current_config().copy_path)
        return {**self.upgrade,
                "library_version": (record or {}).get("mame_version"),
                "configured_version": self.current_config().mame_version,
                "available": sorted(
                    {entry["version"] for entry in (self.releases.get("data") or [])
                     if entry["kind"] == "roms" and entry["full_set"]},
                    reverse=True)}

    def preview_upgrade(self, body):
        """What moving the library to another release would actually cost.

        The whole thesis: 0.282 -> 0.289 changes 244 of the wanted machines and adds
        877, which is 4.5 GB rather than the 163 GB of a full set. Seven releases cost
        the same as one, because only the machines this library keeps are considered.
        """
        if self.upgrade["running"]:
            raise MarqueeError("Already working that out.")
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is a library to move.")

        target = (body or {}).get("version")
        if not target:
            raise MarqueeError("Which release should it move to?")
        record = manifest.read(self.current_config().copy_path)
        current = (record or {}).get("mame_version") or self.current_config().mame_version
        if not current:
            raise MarqueeError("This library does not say which release it holds.")

        # What the library holds, not what the source folder happens to hold: a
        # machine already at the destination with no zip in the torrent folder is
        # exactly the kind an upgrade is about.
        wanted = [item.name for item in plan.library_items]

        def work():
            try:
                here = acquisition.signatures(fetch.fetch_xml(current))
                there = acquisition.signatures(fetch.fetch_xml(target))
                split = acquire.delta(here, there, wanted)
                self.upgrade["data"] = {
                    "from": current, "to": target,
                    "unchanged": len(split["unchanged"]),
                    "changed": len(split["changed"]),
                    "added": len(split["added"]),
                    "fetch": sorted(split["changed"] + split["added"]),
                    "examples": sorted(split["changed"])[:12],
                }
                self.upgrade["error"] = None
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                self.upgrade["error"] = f"{type(error).__name__}: {error}"
            finally:
                self.upgrade["running"] = False

        self.upgrade.update(running=True, data=None, error=None, to=target)
        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "from": current, "to": target}

    def fetch_missing(self, body):
        """Ask the download client for the machines the selection wants but has not got.

        The Wanted page's shortcut for "all of it"; the Library page's Download button
        is the same act with a filter in front of it. Both go through `_one_part`, so
        the rule about what may be deselected is written once.
        """
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is something to compare.")

        # Either what the library is missing, or what an upgrade would need.
        machines = (body or {}).get("machines")
        if machines is None:
            # This release carries zips; a machine on the list for its disk alone is
            # the Library page's Download button's business, not this torrent's.
            by_name = {item.name: item for item in plan.wanted}
            machines = [name for name in plan.needed
                        if name not in by_name or by_name[name].rom_to_fetch]
        if not machines:
            raise MarqueeError("Nothing to fetch.")

        client = self.client()
        release = self._rom_release()
        dry_run = bool((body or {}).get("dry_run"))
        part = self._one_part(release, client, dry_run,
                              lambda files, piece: acquire.rom_selection(
                                  files, machines, piece_size=piece))
        if part.get("error"):
            raise MarqueeError(part["error"])
        return {"release": part["release"], "infohash": part["infohash"],
                "files": part["files"], "bytes": part["bytes"],
                "bytes_human": part["bytes_human"],
                "missing_from_set": part["missing_from_set"],
                "already_in_client": not part["added_now"],
                "shared": bool(part.get("shared")),
                "path": part.get("path"), "client_path": part.get("client_path"),
                "path_visible": part.get("path_visible"),
                "selected": part.get("selected", part["files"]),
                "raised": part.get("raised", part.get("selected", 0)),
                "already_selected": part.get("kept_complete", 0)}

    # -- putting something back --------------------------------------------- #

    def restore(self, body):
        """Take machines off the blacklist.

        The only reason a machine can be lifted off the left-out list one at a time.
        The other five are the tool's own rules -- a preliminary driver, a BIOS set --
        and overriding those is a setting, not a per-game act.

        Excluding a game used to be a one-way door: it was dropped before anything else
        ran, so it appeared in no list, no search and no tree, and the only way back was
        editing settings.ini by hand.
        """
        plan = self.job.plan
        if plan is None or plan.left_out is None:
            raise MarqueeError("Build a plan first, so there is something to look at.")

        body = body or {}
        names = body.get("machines")
        if names is None:
            chosen = filtered(plan.left_out, **_filters(body.get("filters") or {}))
            names = [item.name for item in chosen]

        excluded = {item.name for item in plan.left_out.wanted
                    if item.reason == "blacklisted"}
        putting_back = sorted(set(names) & excluded)
        if not putting_back:
            raise MarqueeError("None of those were excluded by name. The rest are left "
                               "out by the working filters, not by your settings.")

        with self._settings_lock:
            config = self.current_config()
            gone = set(putting_back)
            remaining = [name for name in config.blacklist_roms if name not in gone]
            configuration.write_settings_file(
                self.settings_path, config.with_overrides(blacklist_roms=remaining))
        return {"restored": len(putting_back), "remaining": len(remaining),
                "replan": True}

    @staticmethod
    def _client_path(client, infohash):
        """The same place in qBittorrent's own words, for the message that says why
        this app cannot see it."""
        try:
            entry = client.one(infohash) or {}
        except MarqueeError:
            return None
        return entry.get("content_path") or entry.get("save_path") or None

    def _content_path(self, client, infohash):
        """Where the download client is putting this torrent, in our terms.

        The one thing that has to cross back: the library is built from those files,
        and on a fresh install nobody yet knows what the folder will be called.
        """
        try:
            config = self.current_config()
            mappings = acquisition.parse_mappings(config.remote_path_mappings)
            return acquisition.torrent_root(client, infohash, mappings, config.download_dir)
        except (MarqueeError, KeyError, TypeError):
            return None

    def _release_of(self, kind, variant, what):
        found = self.releases.get("data")
        if not found:
            self.refresh_releases()
            raise MarqueeError("Reading the release list; try again in a moment.")
        candidates = [entry for entry in found
                      if entry["kind"] == kind and entry["variant"] == variant
                      and entry["full_set"]]
        if not candidates:
            raise MarqueeError(f"No {what} is listed.")
        newest = max(candidates, key=lambda entry: _version_key(entry["version"]))
        return indexers.Release(kind=newest["kind"], version=newest["version"],
                                variant=newest["variant"], infohash=newest["infohash"],
                                name=newest["name"], magnet=newest["magnet"])

    def _rom_release(self):
        """The newest full non-merged ROM set, from the indexer."""
        return self._release_of("roms", "non-merged", "non-merged ROM set")

    def _chd_release(self):
        """The newest full merged CHD set. Disks are only published merged."""
        return self._release_of("chds", "merged", "merged CHD set")

    # -- downloading a selection -------------------------------------------- #

    def download_selection(self, body):
        """Fetch exactly what the library page is currently showing, and nothing else.

        The filters are resolved here rather than in the browser so the list that is
        fetched is built by the same code that built the list being looked at -- a
        page that shows 412 games and a download that fetches 380 of them is worse
        than no button at all.

        Two passes are expected: `dry_run` prices it, and the second commits. Nothing
        is ever deselected on a torrent that was already in the client.
        """
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is something to choose from.")

        body = body or {}
        names = body.get("machines")
        if names is not None:
            wanted = set(names)
            chosen = [item for item in plan.wanted if item.name in wanted]
        else:
            chosen = filtered(plan, **_filters(body.get("filters") or {}))
        if not chosen:
            raise MarqueeError("That filter matches nothing.")

        # Only what is not already here. Asking for a machine whose zip is on disk is
        # not an error -- it is what "download this genre" means when half of it has
        # already arrived -- it simply costs nothing.
        # The same rule as the Transfer page's "Fetch first": a copy the check found
        # stale or damaged is in the library and still needs fetching.
        needed = set(plan.needed)
        rom_names = [item.name for item in chosen
                     if item.name in needed and item.rom_to_fetch]
        # Disks the library does not have either: 199 of 311 "missing" disks were
        # sitting in the library, and fetching them again was 120 GB for nothing.
        disks = [(item.name, item.cloneof or item.chd_name, disk)
                 for item in chosen for disk in item.disks_to_fetch]

        if not rom_names and not disks:
            return {"nothing": True, "chosen": len(chosen),
                    "message": f"All {len(chosen):,} of those are already here."}

        client = self.client()
        dry_run = bool(body.get("dry_run"))
        parts = []

        if rom_names:
            parts.append(self._one_part(self._rom_release(), client, dry_run,
                                        lambda files, piece: acquire.rom_selection(
                                            files, rom_names, piece_size=piece)))
        if disks:
            try:
                chd_release = self._chd_release()
            except MarqueeError as error:
                parts.append({"kind": "chds", "error": str(error)})
            else:
                parts.append(self._one_part(chd_release, client, dry_run,
                                            lambda files, piece: acquire.chd_selection(
                                                files, disks, piece_size=piece)))

        usable = [part for part in parts if not part.get("error")]
        if not usable:
            raise MarqueeError(parts[0].get("error") if parts else "Nothing to fetch.")
        total = sum(part["bytes"] for part in usable)
        return {"dry_run": dry_run, "chosen": len(chosen), "parts": parts,
                "machines": len(rom_names), "disks": len(disks),
                "bytes": total, "bytes_human": human_bytes(total),
                "set_bytes_human": human_bytes(sum(part["set_bytes"] for part in usable))}

    def _one_part(self, release, client, dry_run, select):
        """Price, and optionally commit, one release's share of a download."""
        infohash, files, added, ours = self._ensure_release(release, client)
        piece = None
        try:
            piece = (client.properties(infohash) or {}).get("piece_size") or None
        except MarqueeError:
            pass
        chosen = select(files, piece)
        part = {"kind": release.kind, "release": release.name, "infohash": infohash,
                "files": len(chosen.indices), "bytes": chosen.download_bytes,
                "bytes_human": human_bytes(chosen.download_bytes),
                "set_bytes": chosen.total_bytes,
                "missing_from_set": chosen.missing[:20],
                "missing_count": len(chosen.missing),
                "path": self._content_path(client, infohash),
                "client_path": self._client_path(client, infohash),
                "added_now": added}
        # Whether this process can see the place the client is writing into. The
        # torrent's own folder does not exist until the download starts, so the test is
        # the folder above it; when even that is not there, the download will finish
        # and the library step will find nothing -- which is a remote path mapping, not
        # a mystery.
        part["path_visible"] = bool(part["path"]) and (
            os.path.isdir(part["path"]) or os.path.isdir(os.path.dirname(part["path"])))
        if not chosen.indices:
            part["error"] = (f"{release.name} has none of them"
                             + (f" ({len(chosen.missing)} not in the set)"
                                if chosen.missing else ""))
            return part

        # The selection is applied even on a dry run, but only to a torrent this call
        # had to add: it arrives with every one of its 44,166 files selected, and
        # leaving it that way means anyone who presses play in qBittorrent gets 1.3 TB.
        # Priced and left stopped, narrowed to what was priced, it is inert either way.
        #
        # Whose torrent it is decides how. Ours -- filed under this tool's category,
        # now or on an earlier run -- is narrowed: wanted files and complete ones stay,
        # the rest drop to 0. Anybody else's is only ever added to: it may be the
        # user's own download of the whole set, half-way through, and dropping the
        # half they have not got yet is not this tool's call to make. `added` alone
        # was the wrong test, because pricing on one call made the next one think the
        # torrent was somebody else's.
        if dry_run and not added:
            return part

        if ours:
            result = client.narrow(infohash, chosen.indices)
            part["selected"] = result["selected"]
            part["skipped"] = result["skipped"]
            part["kept_complete"] = result["kept_complete"]
        else:
            result = client.include(infohash, chosen.indices)
            part["selected"] = len(chosen.indices)
            part["raised"] = result["raised"]
            part["shared"] = True
        if dry_run:
            return part

        client.start(infohash)
        part["started"] = True
        return part

    def queue_payload(self):
        """What the download client is doing with this tool's torrents.

        Everything else in the client is somebody else's business and is not listed.
        """
        config = self.current_config()
        if not config.download_client:
            return {"configured": False, "torrents": []}
        cached = self._queue_cache
        if cached["data"] is not None and time.time() - cached["at"] < 3:
            return cached["data"]
        try:
            client = self.client()
            entries = client.status()
        except MarqueeError as error:
            payload = {"configured": True, "error": str(error), "torrents": []}
        else:
            mine = [entry for entry in entries
                    if entry["category"] == acquisition.CATEGORY]
            if any(e["phase"] == "failed" for e in mine):
                # Only when something has failed: the reason lives in the client's
                # log, and reading it is one more request.
                reasons = getattr(client, "recent_errors", lambda: {})() or {}
                for entry in mine:
                    if entry["phase"] == "failed":
                        entry["error_message"] = reasons.get(entry["name"])
            left = sum(max(e["size"] - e["downloaded"], 0) for e in mine)
            speed = sum(e["speed"] for e in mine)
            payload = {
                "configured": True,
                "torrents": mine,
                "downloading": sum(1 for e in mine if e["phase"] == "downloading"),
                "seeding": sum(1 for e in mine if e["phase"] == "complete"),
                "bytes_left": left,
                "bytes_left_human": human_bytes(left),
                "speed": speed,
                "speed_human": human_bytes(speed) + "/s",
                # Whole seconds until the slowest of them is done, or None.
                "eta": max([e["eta"] for e in mine
                            if e["phase"] == "downloading" and 0 < e["eta"] < 8640000]
                           or [None]),
                "client": config.download_client,
            }
        self._queue_cache = {"at": time.time(), "data": payload}
        return payload

    # -- artwork ------------------------------------------------------------ #

    def art_payload(self):
        kind = self.art["kind"]
        return {**self.art,
                "cached": art.cached_count(kind),
                "bytes": art.cache_bytes(kind),
                "bytes_human": human_bytes(art.cache_bytes(kind)),
                "kinds": list(art.KINDS)}

    def start_art(self, body):
        if self.art["running"]:
            raise MarqueeError("Artwork is already downloading.")
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is a library to illustrate.")

        kind = (body or {}).get("kind") or art.DEFAULT_KIND
        if kind not in art.KINDS:
            raise MarqueeError(f"Unknown artwork kind: {kind}")
        descriptions = [item.description for item in plan.items]
        # Stop only flips the flag; the worker notices a moment later. A start in that
        # moment used to pass the guard, and the old worker's exit then switched the
        # new one off. Each run has a generation and only reads and writes its own.
        generation = self.art.get("generation", 0) + 1
        self.art["generation"] = generation

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

        self.art.update(running=True, kind=kind, done=0, total=len(descriptions),
                        summary=None, error=None)
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
            try:
                self.versions["data"] = fetch.version_catalogue()
                self.versions["error"] = None
            except Exception as error:  # noqa: BLE001 - shown on the page
                self.versions["error"] = str(error)
            finally:
                self.versions["running"] = False

        self.versions["running"] = True
        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def import_settings(self, body):
        """Read the filters a destination was built with back into the form."""
        described = self.destination_payload(body.get("copy_path") or None)
        if not described:
            raise MarqueeError("That folder has no Marquee record to import from.")
        return {"imported": described.get("settings") or {},
                "mame_version": described.get("mame_version")}

    def config_payload(self):
        config = self.current_config()
        return {
            "rom_dir": config.rom_dir or "",
            "chd_dir": config.chd_dir or "",
            "copy_path": config.copy_path or "",
            "allow_mature": config.allow_mature,
            "parents_only": config.parents_only,
            "write_gamelist": config.write_gamelist,
            "copy_artwork": config.copy_artwork,
            "mature_rom_folder": config.mature_rom_folder,
            "blacklist_genres": config.blacklist_genres,
            "blacklist_categories": config.blacklist_categories,
            "blacklist_roms": config.blacklist_roms,
            "settings_path": self.settings_path,
            "mame_version": config.mame_version,
            "download_client": config.download_client or "",
            "download_username": config.download_username or "",
            # Present so the form can show it is set, without handing it back out.
            "download_password": "••••••••" if config.download_password else "",
            "download_dir": config.download_dir or "",
            "remote_path_mappings": config.remote_path_mappings or "",
            "hardlink": config.hardlink,
        }

    def config_from(self, body):
        """Merge what the page sent over the saved settings.

        A field the page did not send is left alone, but one it sent empty is treated as
        cleared -- otherwise clearing a path in the form and pressing Plan would quietly
        run against the old saved value.
        """
        overrides = {}
        for key in ("rom_dir", "chd_dir", "copy_path", "download_client",
                    "download_username", "download_dir", "remote_path_mappings",
                    "mature_rom_folder"):
            if key in body:
                overrides[key] = (body[key] or "").strip()
        # The password is masked on the way out, so the mask coming back means
        # "unchanged" rather than "set it to a row of dots".
        if "download_password" in body and set(body["download_password"] or "") != {"\u2022"}:
            overrides["download_password"] = body["download_password"]
        for key in ("allow_mature", "hardlink", "parents_only", "write_gamelist",
                    "copy_artwork"):
            if body.get(key) is not None:
                overrides[key] = body[key]
        # A blacklist is only ever taken from something that is actually a list. The
        # page omits them entirely until it has read the saved ones, and anything else
        # arriving here is a bug somewhere, not an instruction to clear them.
        for key in ("blacklist_genres", "blacklist_categories", "blacklist_roms"):
            if isinstance(body.get(key), list):
                overrides[key] = body[key]
        # The chosen version belongs to the configuration too: it is what gets saved and
        # what the destination's record ends up naming.
        version = (body.get("mame_version") or "").strip() if "mame_version" in body else None
        if version:
            overrides["mame_version"] = version
        config = self.current_config().with_overrides(**overrides)
        if "mame_version" in body and not version:
            # The form's "Automatic": with_overrides ignores None, so cleared has to be
            # said explicitly or the old choice would survive every save.
            config.mame_version = None
        return config

    # -- actions ------------------------------------------------------------ #

    def save(self, body):
        with self._settings_lock:
            config = self.config_from(body)
            missing = config.missing_paths
            if missing:
                raise MarqueeError(f"Still missing: {', '.join(missing)}")
            configuration.write_settings_file(self.settings_path, config)
        return {"saved": self.settings_path}

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
            source_progress=lambda: self.source_progress(config))
        self.job.start_plan(config, options)
        return {"started": "plan"}

    def _torrent_files(self, config):
        """[(torrent status entry, [(this app's path, file record)])] for every torrent
        in the client, whoever added it. Read-only.

        A file is named from the client's save path down, and its first part is the
        torrent's own folder -- which is what `locate` can find by name under this
        app's download folder when no mapping says where the client's path lands.
        """
        if not config.download_client:
            return []
        try:
            client = self.client()
            entries = client.status()
        except MarqueeError:
            return []
        mappings = acquisition.parse_mappings(config.remote_path_mappings)
        out = []
        for entry in entries:
            infohash = entry.get("hash")
            if not infohash:
                continue
            try:
                files = client.files(infohash)
            except MarqueeError:
                continue
            save_path = entry.get("save_path") or ""
            roots, named = {}, []
            for record in files:
                head, _, tail = record["path"].replace("\\", "/").partition("/")
                if head not in roots:
                    roots[head] = acquisition.locate(
                        posixpath.join(save_path, head) if save_path else head,
                        mappings, config.download_dir)
                path = os.path.join(roots[head], tail) if tail else roots[head]
                named.append((os.path.normpath(path), record))
            out.append((entry, named))
        return out

    def source_progress(self, config=None):
        """{source path: fraction downloaded} for every file the client knows.

        The client knows which pieces it has; the file on disk is the right size from
        the first minute and carries its header early, so this is the check that
        cannot be fooled. Every file of every torrent, not just the unfinished
        torrents: a torrent reads 100% once its unwanted files are deselected, and a
        disk deselected at 96% is exactly the one that must not be copied. Empty when
        there is no client or it does not answer -- the plan then reads the files.
        """
        config = config or self.current_config()
        progress = {}
        for _entry, named in self._torrent_files(config):
            for path, record in named:
                progress[path] = float(record.get("progress", 0) or 0)
        return progress

    def incomplete_sources(self, config=None):
        """Source paths the download client is still fetching."""
        return {path for path, done in self.source_progress(config).items() if done < 1}

    def disk_pieces(self, config=None):
        """{(file name lower, size): (offset in torrent, piece size, [piece sha1])} for
        every .chd the client's torrents carry -- what a deep library check hashes
        against. Piece hashes are asked for once per torrent that has disks."""
        config = config or self.current_config()
        if not config.download_client:
            return {}
        try:
            client = self.client()
        except MarqueeError:
            return {}
        out = {}
        for entry, named in self._torrent_files(config):
            records = [record for _path, record in named]
            if not any(str(record.get("path", "")).lower().endswith(".chd")
                       for record in records):
                continue
            try:
                piece = int((client.properties(entry["hash"]) or {}).get("piece_size") or 0)
            except (MarqueeError, TypeError, ValueError):
                piece = 0
            hashes = client.piece_hashes(entry["hash"]) if piece else []
            if not hashes:
                continue
            offset = 0
            for record in sorted(records, key=lambda one: one.get("index", 0)):
                size = int(record.get("size") or 0)
                name = str(record.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                key = (name.lower(), size)
                if name.lower().endswith(".chd") and key not in out:
                    out[key] = (offset, piece, hashes)
                offset += size
        return out

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

    def copy(self, body):
        if self.job.plan is None:
            raise MarqueeError("Build a plan first, so there is something to copy.")
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
            try:
                # A flat romset keeps ROMs and CHDs in one folder; walking it twice
                # would double a ten second scan and show the same figure twice.
                paths = {"roms": config.rom_dir}
                if config.chd_dir and config.chd_dir != config.rom_dir:
                    paths["chds"] = config.chd_dir
                paths["destination"] = None if config.is_remote else config.copy_path
                data = sync.survey(paths)
                for entry in data.values():
                    entry["bytes_human"] = human_bytes(entry["bytes"])
                self.survey.update(data=data, **{"for": key})
            finally:
                self.survey["running"] = False

        self.survey["running"] = True
        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def cancel(self, _body):
        return {"cancelling": self.job.cancel()}

    def browse(self, path):
        """Directory listing, so paths can be picked instead of typed.

        A directory that cannot be read is reported as a note on an otherwise valid
        answer rather than as a failure: the picker has to stay usable when someone
        wanders into /root on the way somewhere else.
        """
        path = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
        if not os.path.isdir(path):
            path = os.path.dirname(path) or "/"

        names, note = [], None
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            names.append(entry.name)
                    except OSError:
                        pass
            names.sort(key=str.lower)
        except PermissionError:
            note = "No permission to read this folder."
        except OSError as error:
            note = f"Could not read this folder: {error.strerror or error}"

        parent = os.path.dirname(path.rstrip(os.sep)) or "/"
        return {"path": path, "parent": parent if parent != path else None,
                # Both shapes: `directories` is the bare names, `entries` carries the
                # full path so the picker does not have to join them itself and get
                # the separator wrong.
                "directories": names,
                "entries": [{"name": name, "path": os.path.join(path, name)}
                            for name in names],
                "note": note,
                "writable": os.access(path, os.W_OK),
                "roots": _roots()}



ALLOWED_FILTERS = ("query", "status", "genre", "category", "mature", "have",
                   "condition", "reason", "state")


def _filters(given):
    """Only the keys the library page filters on, so a stray one is not an error.

    The view is honoured exactly, `have` included: someone looking at what is already
    on disk and pressing Download is told that, rather than quietly being given the
    whole genre.
    """
    return {key: given[key] for key in ALLOWED_FILTERS if given.get(key)}


def newest_version(tag_names):
    """The highest release among tag names like v0.5.0; None when there is none."""
    best = None
    for name in tag_names:
        text = str(name or "").strip()
        if not text.startswith("v"):
            continue
        candidate = text[1:]
        if not all(part.isdigit() for part in candidate.split(".")):
            continue
        if best is None or _version_key(candidate) > _version_key(best):
            best = candidate
    return best


def build_label():
    """The build the image was made from: "master@7c17217", "v0.5.0@7c17217", or
    "source" for a checkout run directly."""
    raw = (os.environ.get("MARQUEE_BUILD") or "").strip()
    if not raw or raw == "local":
        return "local" if raw else "source"
    ref, _, sha = raw.partition("@")
    return f"{ref}@{sha[:7]}" if sha else ref


def _version_key(version):
    """"0.289" sorts after "0.9" -- compare the numbers, not the string."""
    try:
        return tuple(int(part) for part in str(version).split("."))
    except (TypeError, ValueError):
        return (0,)


def _roots():
    """Places worth starting from, so nobody has to type their way out of /config.

    In a container the interesting paths are the mounted volumes, and there is no
    home directory to fall back on.
    """
    found = []
    for candidate in ("/downloads", "/library", "/config", os.path.expanduser("~"), "/"):
        if candidate and os.path.isdir(candidate) and candidate not in found:
            found.append(candidate)
    return found


def _release_payload(release):
    return {"kind": release.kind, "version": release.version,
            "variant": release.variant, "name": release.name,
            "infohash": release.infohash, "from_version": release.from_version,
            "full_set": release.is_full_set, "magnet": release.magnet_uri(),
            "datfile": release.datfile_url}


