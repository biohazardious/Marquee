"""qBittorrent Web API v2 client.

The one thing that makes this project possible is per-file priority: a MAME
non-merged set is 44,166 one-machine zips in a single torrent, so setting the
32,000 unwanted ones to priority 0 turns a 163 GB download into 51 GB. Everything
else here exists to support that call.

Verified against qBittorrent 5.2.3 / Web API 2.15.1.
"""
import binascii
import http.client
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

from ..errors import MarqueeError
from ..reporting import Reporter

# libtorrent's four levels. Only 0 and 1 are used: the point is inclusion, not ordering.
PRIO_SKIP = 0
PRIO_NORMAL = 1
PRIO_HIGH = 6
PRIO_MAXIMUM = 7

# qBittorrent's states, grouped the way a UI cares about them.
DOWNLOADING = {"downloading", "metaDL", "forcedDL", "allocating", "checkingDL",
               "queuedDL", "stalledDL"}
COMPLETE = {"uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP",
            "stoppedUP", "checkingUP"}
STOPPED = {"pausedDL", "stoppedDL"}
FAILED = {"error", "missingFiles", "unknown"}

# A torrent added from a magnet has no file list until metadata arrives from peers.
METADATA_TIMEOUT = 180
# qBittorrent runs the torrent just far enough to pull the metadata, then stops it
# on its own. Adding plainly stopped instead never fetches metadata at all, so the
# file list never appears and there is nothing to select; adding it running means
# content starts arriving before the selection lands. This is the only way to get
# the file list without downloading anything, and it needs Web API 2.8.19.
STOP_ON_METADATA = "MetadataReceived"


class QBittorrentError(MarqueeError):
    pass


class QBittorrentAuthError(QBittorrentError):
    """The session lapsed or the credentials are wrong: the one case worth a retry."""


class QBittorrent:
    def __init__(self, url, username=None, password=None, timeout=60, reporter=None):
        self.base = url.rstrip("/")
        if not self.base.endswith("/api/v2"):
            self.base += "/api/v2"
        self.origin = self.base[:-len("/api/v2")]
        self.username = username
        self.password = password
        self.timeout = timeout
        self.reporter = reporter or Reporter()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._authenticated = False

    # -- transport ---------------------------------------------------------- #

    def _request(self, path, data=None, files=None, raw=False, with_status=False):
        url = f"{self.base}/{path}"
        headers = {"Referer": self.origin, "Origin": self.origin}

        if files is not None:
            body, content_type = _multipart(data or {}, files)
            headers["Content-Type"] = content_type
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = None

        request = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST" if body is not None else "GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            if error.code == 403:
                # The session expired; one silent re-login, then give up.
                self._authenticated = False
                raise QBittorrentAuthError(
                    "qBittorrent refused the request (403). Check the username and "
                    "password, and that this host is allowed by the Web UI's access "
                    "rules.")
            detail = error.read().decode("utf-8", "replace").strip()
            raise QBittorrentError(f"qBittorrent returned {error.code} for {path}"
                                   f"{': ' + detail if detail else ''}")
        except urllib.error.URLError as error:
            raise QBittorrentError(f"Cannot reach qBittorrent at {self.origin}: "
                                   f"{error.reason}")
        except (OSError, http.client.HTTPException) as error:
            # A read that times out, a connection reset mid-reply, a truncated body:
            # urllib does not wrap these, and a stalled client used to come out of the
            # web layer as a traceback rather than a sentence.
            raise QBittorrentError(f"qBittorrent at {self.origin} stopped answering: "
                                   f"{error.__class__.__name__}: {error}")
        decoded = payload if raw else payload.decode("utf-8", "replace")
        return (decoded, status) if with_status else decoded

    def _call(self, path, data=None, files=None):
        """A request that re-authenticates once if the session has lapsed.

        Only a 403 earns the retry. Repeating any failed call meant a 404 from an
        older client was tried twice, and a connection dropped in the middle of
        torrents/add re-sent the add.
        """
        if not self._authenticated:
            self.login()
        try:
            return self._request(path, data, files)
        except QBittorrentAuthError:
            if not self.username:
                raise
            self._authenticated = False
            self.login()
            return self._request(path, data, files)

    def _json(self, path, data=None):
        text = self._call(path, data)
        try:
            return json.loads(text)
        except ValueError:
            raise QBittorrentError(f"qBittorrent gave a non-JSON reply to {path}: "
                                   f"{text[:120]!r}")

    # -- session ------------------------------------------------------------ #

    def login(self):
        """Open a session.

        Older builds answer "Ok." or "Fails."; 5.x answers 204 with an empty body and
        says nothing about whether the credentials were any good. And when the Web UI
        bypasses authentication for this subnet -- a common setup -- even a wrong
        password is accepted here. So a bare login proves nothing: the real check is
        whether an authenticated call goes through afterwards, which is what `test`
        does.
        """
        if not self.username:
            self._authenticated = True
            return
        reply, status = self._request(
            "auth/login", {"username": self.username, "password": self.password},
            with_status=True)
        reply = reply.strip()
        if reply.startswith("Fails"):
            raise QBittorrentAuthError("qBittorrent rejected those credentials.")
        self._authenticated = True

    def test(self):
        """Reachability, credentials and API level in one go. Returns a description.

        The authenticated call after the login is what actually proves the
        credentials: see `login`.
        """
        self._authenticated = False
        self.login()
        try:
            api = self._request("app/webapiVersion").strip()
        except QBittorrentError as error:
            if "403" in str(error) or "refused" in str(error):
                raise QBittorrentError(
                    "qBittorrent rejected those credentials.") from error
            raise
        app = self._call("app/version").strip()
        if _version_tuple(api) < (2, 8, 2):
            raise QBittorrentError(
                f"qBittorrent Web API {api} is too old; 2.8.2 or newer is needed for "
                f"per-file selection. Upgrade qBittorrent (yours reports {app}).")
        return {"app_version": app, "api_version": api,
                "save_path": self.preferences().get("save_path")}

    def preferences(self):
        return self._json("app/preferences")

    # -- torrents ----------------------------------------------------------- #

    def add(self, source, save_path=None, category=None, tags=None, stopped=True,
            sequential=False):
        """Add a magnet URI or .torrent bytes. Returns the infohash.

        `stopped` means "do not download any content", not "do nothing": a magnet
        carries no file list, so the torrent has to run long enough to fetch its
        metadata from peers before there is anything to select. `stopCondition` makes
        the client do exactly that and stop itself -- measured on the real 634 MB
        bios-devices set, metadata arrived in under six seconds with zero content
        bytes fetched.

        A .torrent file already contains the file list, so it can simply be added
        stopped.
        """
        form = {"autoTMM": "false"}
        if stopped and isinstance(source, bytes):
            # `paused` is the pre-5.0 spelling; sending both keeps one code path.
            form["stopped"] = form["paused"] = "true"
        elif stopped:
            form["stopCondition"] = STOP_ON_METADATA
            form["stopped"] = form["paused"] = "false"
        else:
            form["stopped"] = form["paused"] = "false"
        if save_path:
            form["savepath"] = save_path
        if category:
            form["category"] = category
        if tags:
            form["tags"] = tags if isinstance(tags, str) else ",".join(tags)
        if sequential:
            form["sequentialDownload"] = "true"

        if isinstance(source, bytes):
            known = None
            reply = self._call("torrents/add", form,
                               files={"torrents": ("upload.torrent", source)})
        else:
            known = infohash_of_magnet(source)
            form["urls"] = source
            reply = self._call("torrents/add", form)

        reply = reply.strip()
        if reply and reply != "Ok.":
            try:
                added = json.loads(reply).get("added_torrent_ids") or []
            except ValueError:
                added = []
            if added:
                return added[0].lower()
        if known:
            return known
        raise QBittorrentError(
            "qBittorrent accepted the torrent but did not say which one it is. "
            "Add it from a magnet link, or use a client new enough to report ids.")

    def wait_for_metadata(self, infohash, timeout=METADATA_TIMEOUT, on_wait=None,
                          interval=2):
        """Block until the file list is known.

        A magnet carries no file list; it has to be pulled from peers first. Until
        that lands there is nothing to select, so every caller needs this.
        """
        deadline = time.monotonic() + timeout
        while True:
            files = self.files(infohash)
            if files:
                return files
            entry = self.one(infohash)
            if entry is None:
                raise QBittorrentError(
                    f"The torrent {infohash[:12]} vanished from the client while its "
                    f"metadata was being fetched.")
            if time.monotonic() >= deadline:
                raise QBittorrentError(
                    f"No metadata for {infohash[:12]} after {timeout}s. The torrent may "
                    f"have no reachable peers.")
            if on_wait:
                on_wait()
            time.sleep(interval)

    def files(self, infohash):
        try:
            raw = self._json("torrents/files", {"hash": infohash})
        except QBittorrentError:
            return []
        if not isinstance(raw, list):
            return []
        return [{"index": entry.get("index", position),
                 "path": entry["name"],
                 "size": entry["size"],
                 "priority": entry["priority"],
                 "progress": entry.get("progress", 0.0),
                 "piece_range": entry.get("piece_range")}
                for position, entry in enumerate(raw)]

    def set_priority(self, infohash, indices, priority):
        """Set one priority across many file indices.

        qBittorrent takes the ids pipe-separated in the body; 44,166 of them is a
        400 KB request, so they go in batches.
        """
        indices = list(indices)
        if not indices:
            return
        for batch in _chunks(indices, 2000):
            self._call("torrents/filePrio",
                       {"hash": infohash,
                        "id": "|".join(str(index) for index in batch),
                        "priority": priority})

    def select_only(self, infohash, indices):
        """Download exactly these files and nothing else.

        Everything is dropped to 0 first: a torrent re-added or re-planned must not
        inherit a previous selection.
        """
        wanted = set(indices)
        current = self.files(infohash)
        if not current:
            raise QBittorrentError(f"No file list for {infohash[:12]} yet.")
        skip = [f["index"] for f in current if f["index"] not in wanted]
        keep = [f["index"] for f in current if f["index"] in wanted]
        if not keep:
            raise QBittorrentError(
                "Refusing to deselect every file: qBittorrent treats a torrent with "
                "nothing selected as an error.")
        self.set_priority(infohash, skip, PRIO_SKIP)
        self.set_priority(infohash, keep, PRIO_NORMAL)
        return {"selected": len(keep), "skipped": len(skip)}

    def narrow(self, infohash, indices):
        """Fetch these files, keep whatever is already complete, and skip the rest.

        The safe form of `select_only`, and the one to reach for. A file that is
        finished stays selected whatever happens, so nothing that is being seeded is
        ever stopped -- while everything nobody wants and nobody has drops to priority
        0, which is what stops a freshly added 1.3 TB set from fetching all of it.

        On a complete set this does exactly what `include` does, because there is
        nothing incomplete left to skip. On a set with nothing downloaded it does
        exactly what `select_only` does. Both are the right answer for their case, and
        neither has to be chosen in advance.
        """
        wanted = set(indices)
        current = self.files(infohash)
        if not current:
            raise QBittorrentError(f"No file list for {infohash[:12]} yet.")

        keep, skip = [], []
        for entry in current:
            if entry["index"] in wanted or entry.get("progress", 0.0) >= 1.0:
                keep.append(entry["index"])
            else:
                skip.append(entry["index"])
        if not keep:
            raise QBittorrentError(
                "Refusing to deselect every file: qBittorrent treats a torrent with "
                "nothing selected as an error.")

        self.set_priority(infohash, skip, PRIO_SKIP)
        self.set_priority(infohash, keep, PRIO_NORMAL)
        return {"selected": len(keep), "skipped": len(skip),
                "wanted": len(wanted & set(keep)),
                "kept_complete": len(keep) - len(wanted & set(keep))}

    def include(self, infohash, indices):
        """Raise these files to normal priority, and lower nothing.

        The safe counterpart to `select_only`. A torrent already in the client may be
        complete and seeding; dropping any of its files to 0 would stop it seeding
        them, and on a 1.3 TB set that is not recoverable cheaply. Adding files to a
        selection never has that effect, so wanting more is always allowed while
        wanting less is a separate, deliberate act.
        """
        wanted = set(indices)
        current = self.files(infohash)
        if not current:
            raise QBittorrentError(f"No file list for {infohash[:12]} yet.")
        raise_these = [f["index"] for f in current
                       if f["index"] in wanted and f["priority"] == PRIO_SKIP]
        self.set_priority(infohash, raise_these, PRIO_NORMAL)
        return {"raised": len(raise_these),
                "already_selected": len(wanted) - len(raise_these)}

    def start(self, infohash):
        self._resume_or_start(infohash, "start", "resume")

    def stop(self, infohash):
        self._resume_or_start(infohash, "stop", "pause")

    def _resume_or_start(self, infohash, modern, legacy):
        # Renamed in qBittorrent 5.0; older builds only know the old spelling.
        try:
            self._call(f"torrents/{modern}", {"hashes": infohash})
        except QBittorrentError:
            self._call(f"torrents/{legacy}", {"hashes": infohash})

    def recheck(self, infohash):
        self._call("torrents/recheck", {"hashes": infohash})

    def delete(self, infohash, delete_files=False):
        self._call("torrents/delete",
                   {"hashes": infohash,
                    "deleteFiles": "true" if delete_files else "false"})

    def status(self, infohashes=None):
        data = {}
        if infohashes:
            data["hashes"] = "|".join(infohashes)
        raw = self._json("torrents/info", data or None)
        return [_status(entry) for entry in raw]

    def one(self, infohash):
        found = self.status([infohash])
        return found[0] if found else None

    def properties(self, infohash):
        return self._json("torrents/properties", {"hash": infohash})

    def export(self, infohash):
        """The .torrent file itself, so a file list can be cached without peers."""
        return self._request("torrents/export", {"hash": infohash}, raw=True)

    def categories(self):
        return self._json("torrents/categories")

    def ensure_category(self, name, save_path=None):
        if name in self.categories():
            return
        self._call("torrents/createCategory",
                   {"category": name, "savePath": save_path or ""})


# --------------------------------------------------------------------------- #
# helpers


def _status(entry):
    state = entry.get("state", "unknown")
    return {
        "hash": entry.get("hash", "").lower(),
        "name": entry.get("name", ""),
        "state": state,
        "phase": ("complete" if state in COMPLETE else
                  "stopped" if state in STOPPED else
                  "failed" if state in FAILED else "downloading"),
        "progress": entry.get("progress", 0.0),
        "downloaded": entry.get("completed", 0),
        # `size` is the selected bytes, `total_size` the whole torrent. The gap
        # between them is the whole point of this project, so both are kept.
        "size": entry.get("size", 0),
        "total_size": entry.get("total_size", 0),
        "speed": entry.get("dlspeed", 0),
        "eta": entry.get("eta", 0),
        "save_path": entry.get("save_path", ""),
        "content_path": entry.get("content_path", ""),
        "category": entry.get("category", ""),
        "seeds": entry.get("num_seeds", 0),
        "peers": entry.get("num_leechs", 0),
    }


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _version_tuple(text):
    parts = []
    for piece in str(text).strip().lstrip("v").split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def infohash_of_magnet(magnet):
    """The btih out of a magnet URI, lowercased, as qBittorrent keys torrents by it."""
    if not magnet.startswith("magnet:"):
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
    for topic in query.get("xt", []):
        if topic.lower().startswith("urn:btih:"):
            value = topic[len("urn:btih:"):]
            if len(value) == 40:
                return value.lower()
            if len(value) == 32:
                # base32 v1 infohash
                import base64
                try:
                    return base64.b32decode(value.upper()).hex()
                except (binascii.Error, ValueError):
                    return None
    return None


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode()
    for name, (filename, content) in files.items():
        guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f"Content-Type: {guessed}\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"
