"""The download client: pricing a release, fetching a selection, and what is arriving."""
import json
import os
import posixpath
import time

from .. import acquire, acquisition, atomic, download, indexers, sources
from . import fetches
from ..errors import LibraryUnreachable, MarqueeError
from ..plan import human_bytes
from .views import filtered, library_sizes, missing_rows
from .releases import _version_key


class DownloadsMixin:
    """Part of `Application`; see marquee.web.application."""

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
        username = values.get("download_username") or config.download_username
        password = values.get("download_password")
        if not password or set(password) == {"\u2022"}:
            # The saved password only ever goes to the saved client as the saved
            # user. Paired with any address a request names, it would be sent to
            # whoever answers there.
            same = ((url or "").rstrip("/") == (config.download_client or "").rstrip("/")
                    and (username or "") == (config.download_username or ""))
            password = config.download_password if same else None
        # One client, and so one session, per address and credentials. A new one per
        # call logged in again every time -- the queue alone asks every few seconds,
        # which was a fresh qBittorrent session each time, each kept for an hour.
        key = (url, username, password)
        with self._task_lock:
            cached = self._clients.get(key)
            if cached is None:
                cached = download.for_url(url, username=username, password=password)
                if len(self._clients) > 8:
                    self._clients.clear()
                self._clients[key] = cached
        return cached

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

        Returns (infohash, files, fresh, ours). A torrent is ours when it sits in
        this tool's category -- this call filed it there, or an earlier one did.
        Anything else in the client is the user's own download, possibly of the
        whole set, and may only ever have files added to it.

        `fresh` means ours and still as qBittorrent added it: every file selected,
        not a byte fetched. That is what must never be left behind, and "this call
        added it" was not enough to catch it -- a metadata wait that timed out, or a
        crash between add and narrow, left the whole set selected for the next call
        to find and, having not added it, leave alone.
        """
        infohash = release.infohash
        entry = client.one(infohash)
        added = entry is None
        if added:
            client.ensure_category(acquisition.CATEGORY)
            infohash = client.add(release.magnet_uri(),
                                  category=acquisition.CATEGORY,
                                  tags=[release.kind, release.version], stopped=True)
            try:
                client.wait_for_metadata(infohash)
            except MarqueeError:
                # Nothing has been fetched and nothing can be narrowed yet; when the
                # metadata does arrive it would sit there with all 1.3 TB selected.
                # It was added a moment ago, by this call, so it goes again.
                try:
                    client.delete(infohash, delete_files=False)
                except MarqueeError:
                    pass
                raise
        ours = added or (entry or {}).get("category") == acquisition.CATEGORY
        files = client.files(infohash)
        if not files:
            raise MarqueeError(
                f"{release.name}: the download client has no file list for it yet.")
        fresh = added or (ours and _untouched(files))
        return infohash, files, fresh, ours

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
            client = self.client()
            release = self._rom_release()
            infohash, files, fresh, _ours = self._ensure_release(release, client)
            if fresh:
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
                chd_hash, chd_files, chd_fresh, _ours = self._ensure_release(
                    chd, client)
                if chd_fresh:
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

        # Whatever it raises is shown: this thread once died on an unpacking error
        # with no word to anyone, leaving no prices and no reason.
        if not self._start(self.catalogue, work, error=None):
            raise MarqueeError("Already reading the release.")
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

        A game the library already holds weighs what its files there weigh. That
        needs no release at all -- and without it a library of ten thousand games
        that Marquee had never downloaded read "0 B" on every page.
        """
        plan = self.job.plan
        roms, disks = self.catalogue["roms"], self.catalogue["disks"]
        if plan is None:
            return roms or {}
        left_out = getattr(plan, "left_out", None)
        key = (len(plan.items), len(left_out.wanted) if left_out else 0,
               self.catalogue["fetched_at"])
        # The plan itself is held, not id(plan): CPython reuses a freed address, and a
        # re-plan of the same size could have read back the old plan's sizes. The
        # diff likewise: a transfer drops it and the next plan brings a new one.
        if self._sizes["key"] == key and self._sizes["plan"] is plan \
                and self._sizes.get("sync") is plan.sync:
            return self._sizes["data"]

        # The left-out catalogue too: it is browsed in the same tree and a tree whose
        # every row reads 0 B is not worth drawing.
        every = plan.wanted + (left_out.wanted if left_out else []) if roms else []
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
        sizes.update(library_sizes(plan))
        self._sizes = {"key": key, "plan": plan, "sync": plan.sync, "data": sizes}
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

    def missing_payload(self, sort="name", descending=False):
        plan = self.job.plan
        if plan is None:
            # No plan is not "nothing missing": it may be that the library could not be
            # reached, and the page must not say every game is on disk.
            return {"planned": False, "busy": self.job.busy, "reason": self.job.error,
                    "waiting": isinstance(self.job.failure, LibraryUnreachable),
                    "total": 0, "bytes": 0, "genres": [], "rows": []}
        # The catalogue, if it has been read, is the better answer: it prices every
        # machine in the release rather than only the ones a torrent in the client
        # happens to cover.
        sizes = self.release_sizes() or self.torrent_sizes()
        payload = missing_rows(plan, sizes, priced=bool(sizes), sort=sort,
                               descending=descending)
        payload["releases"] = self.releases.get("data")
        # Three different answers, and they need different words: no client at all,
        # a client that does not hold the release, or a real figure.
        payload["client"] = bool(self.current_config().download_client)
        return payload

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
        # An upgrade names its release: the newest set is not the one it was priced
        # against when the move is to anything but the newest.
        release = self._rom_release((body or {}).get("version") or None)
        dry_run = bool((body or {}).get("dry_run"))
        part = self._one_part(release, client, dry_run,
                              lambda files, piece: acquire.rom_selection(
                                  files, machines, piece_size=piece))
        if part.get("error"):
            raise MarqueeError(part["error"])
        if not dry_run:
            self.record_fetch([part], f"{len(machines):,} missing games")
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

    def _release_of(self, kind, variant, what, version=None):
        found = self.releases.get("data")
        if not found:
            self.refresh_releases()
            raise MarqueeError("Reading the release list; try again in a moment.")
        candidates = [entry for entry in found
                      if entry["kind"] == kind and entry["variant"] == variant
                      and entry["full_set"]
                      and (version is None or entry["version"] == version)]
        if not candidates:
            raise MarqueeError(f"No {what} is listed"
                               + (f" for MAME {version}." if version else "."))
        newest = max(candidates, key=lambda entry: _version_key(entry["version"]))
        return indexers.Release(kind=newest["kind"], version=newest["version"],
                                variant=newest["variant"], infohash=newest["infohash"],
                                name=newest["name"], magnet=newest["magnet"])

    def _rom_release(self, version=None):
        """The newest full non-merged ROM set, from the indexer -- or the one for
        `version`, which is what moving a library to that release fetches from."""
        return self._release_of("roms", "non-merged", "non-merged ROM set", version)

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
                # Which machine asked for each disk: a merged set files a clone's
                # disk in its parent's folder, and the folder is not the game.
                owners = {disk: machine for machine, _parent, disk in disks}
                parts.append(self._one_part(chd_release, client, dry_run,
                                            lambda files, piece: acquire.chd_selection(
                                                files, disks, piece_size=piece),
                                            owners=owners))

        usable = [part for part in parts if not part.get("error")]
        if not usable:
            raise MarqueeError(parts[0].get("error") if parts else "Nothing to fetch.")
        if not dry_run:
            self.record_fetch(parts, f"{len(chosen):,} games from the Library")
        total = sum(part["bytes"] for part in usable)
        return {"dry_run": dry_run, "chosen": len(chosen), "parts": parts,
                "machines": len(rom_names), "disks": len(disks),
                "bytes": total, "bytes_human": human_bytes(total),
                "set_bytes_human": human_bytes(sum(part["set_bytes"] for part in usable))}

    def _one_part(self, release, client, dry_run, select, owners=None):
        """Price, and optionally commit, one release's share of a download."""
        infohash, files, fresh, ours = self._ensure_release(release, client)
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
                "added_now": fresh}
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
            # Nothing to select is routine -- the wanted disks are not in this CHD
            # set yet -- but a set added just to find that out still arrives with
            # everything selected.
            if fresh:
                self._disarm(client, infohash, files)
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
        if dry_run and not fresh:
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
        # Written down, so Activity can follow these games rather than a percentage.
        part["_record"] = fetches.record_of(release, infohash, files, chosen, owners)
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
            entries = client.status(category=acquisition.CATEGORY)
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
            # The downloads Marquee started, as jobs: which games, how many are in,
            # what is left, and what the release never had.
            try:
                jobs = self.fetch_jobs(client)
            except MarqueeError:
                jobs = []
            payload["jobs"] = jobs
            payload["finished_since_plan"] = self.finished_since_plan(jobs)
        self._queue_cache = {"at": time.time(), "data": payload}
        return payload

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


def _untouched(files):
    """A torrent as qBittorrent adds one: every file selected, nothing fetched.

    What this tool itself leaves in its category never looks like that -- `narrow()`
    skips what is not wanted, and anything wanted starts arriving -- so finding one
    means an earlier call added it and was cut short before narrowing it.
    """
    return len(files) > 1 and all(
        entry.get("priority", 1) > 0 and not entry.get("progress") for entry in files)


ALLOWED_FILTERS = ("query", "status", "genre", "category", "mature", "have",
                   "condition", "reason", "state", "console")


def _filters(given):
    """Only the keys the library page filters on, so a stray one is not an error.

    The view is honoured exactly, `have` included: someone looking at what is already
    on disk and pressing Download is told that, rather than quietly being given the
    whole genre.
    """
    return {key: given[key] for key in ALLOWED_FILTERS if given.get(key)}
