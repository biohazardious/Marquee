"""The qBittorrent connector.

Tested against a stand-in Web API rather than a mocked urlopen, so the cookie
session, the multipart upload and the pipe-separated batching are all exercised as
they will be against the real client.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from marquee.download.qbittorrent import (PRIO_NORMAL, PRIO_SKIP, QBittorrent,
                                             QBittorrentError, infohash_of_magnet)

HASH = "deadbeef00000000000000000000000000000008"


class FakeQbit:
    """Just enough of the Web API to drive the connector."""

    def __init__(self, app_version="v5.2.3", api_version="2.15.1", password="secret"):
        self.app_version = app_version
        self.api_version = api_version
        self.password = password
        # qBittorrent 5.x answers 204 with an empty body instead of "Ok.".
        self.login_style = "legacy"
        # True when the Web UI waves through this subnet without checking anything.
        self.bypass_auth = False
        self.files = [
            {"index": 0, "name": "MAME/aaa.zip", "size": 100, "priority": 1,
             "progress": 1.0, "piece_range": [0, 0]},
            {"index": 1, "name": "MAME/bbb.zip", "size": 200, "priority": 1,
             "progress": 0.0, "piece_range": [0, 1]},
            {"index": 2, "name": "MAME/ccc.zip", "size": 300, "priority": 1,
             "progress": 0.0, "piece_range": [1, 2]},
        ]
        self.added = []
        self.prio_calls = []
        self.started = []
        self.metadata_after = 0        # how many `files` calls return nothing first
        self._files_asked = 0
        self.modern_start = True

    # -- routing -------------------------------------------------------- #

    def handle(self, path, form, upload):
        if path == "auth/login":
            ok = (self.bypass_auth or
                  form.get("password", [None])[0] == self.password)
            self.authenticated = ok
            if self.login_style == "modern":
                # Says nothing about whether the password was right.
                return 204, b""
            return 200, b"Ok." if ok else b"Fails."
        if not getattr(self, "authenticated", True) and path.startswith("app/"):
            return getattr(self, "refusal", 403), b"Forbidden"
        if path == "app/webapiVersion":
            return 200, self.api_version.encode()
        if path == "app/version":
            return 200, self.app_version.encode()
        if path == "app/preferences":
            return 200, json.dumps({"save_path": "/downloads"}).encode()
        if path == "torrents/add":
            self.added.append({"form": form, "upload": upload})
            return 200, json.dumps({"added_torrent_ids": [HASH],
                                    "success_count": 1}).encode()
        if path == "torrents/files":
            self._files_asked += 1
            if self._files_asked <= self.metadata_after:
                return 404, b"Not found"
            return 200, json.dumps(self.files).encode()
        if path == "torrents/filePrio":
            ids = [int(x) for x in form["id"][0].split("|")]
            priority = int(form["priority"][0])
            self.prio_calls.append((ids, priority))
            for index in ids:
                self.files[index]["priority"] = priority
            return 200, b""
        if path in ("torrents/start", "torrents/resume"):
            if path.endswith("start") and not self.modern_start:
                return 404, b"Not found"
            self.started.append(path)
            return 200, b""
        if path == "torrents/info":
            selected = sum(f["size"] for f in self.files if f["priority"])
            return 200, json.dumps([{
                "hash": HASH, "name": "MAME 0.289 ROMs (non-merged)",
                "state": "downloading", "progress": 0.25, "completed": 150,
                "size": selected, "total_size": 600, "dlspeed": 1024, "eta": 60,
                "save_path": "/downloads", "content_path": "/downloads/MAME",
                "category": "marquee", "num_seeds": 3, "num_leechs": 9}]).encode()
        return 404, b"Not found"


@pytest.fixture
def server():
    fake = FakeQbit()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _run(self, form=None, upload=None):
            path = self.path.split("?", 1)[0].lstrip("/")
            path = path[len("api/v2/"):] if path.startswith("api/v2/") else path
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            status, body = fake.handle(path, {**query, **(form or {})}, upload)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 200 and path == "auth/login":
                self.send_header("Set-Cookie", "SID=abc123; path=/")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._run()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/"):
                self._run(_multipart_fields(raw), _multipart_upload(raw))
            else:
                self._run(parse_qs(raw.decode()))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, fake
    httpd.shutdown()


def _multipart_fields(raw):
    fields = {}
    for part in raw.split(b"\r\n--"):
        if b'name="' not in part or b"filename=" in part:
            continue
        name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0].decode()
        fields[name] = [value]
    return fields


def _multipart_upload(raw):
    for part in raw.split(b"\r\n--"):
        if b"filename=" in part:
            return part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    return None


def client(url, **kwargs):
    kwargs.setdefault("username", "admin")
    kwargs.setdefault("password", "secret")
    return QBittorrent(url, timeout=5, **kwargs)


class TestSession:
    def test_a_modern_client_answering_204_is_accepted(self, server):
        url, fake = server
        fake.login_style = "modern"
        assert client(url).test()["app_version"] == "v5.2.3"

    def test_a_modern_client_still_catches_a_bad_password(self, server):
        url, fake = server
        fake.login_style = "modern"
        # 204 tells us nothing, so the authenticated call afterwards is what decides.
        with pytest.raises(QBittorrentError, match="rejected those credentials"):
            client(url, password="wrong").test()

    def test_a_subnet_with_authentication_bypassed_works_anyway(self, server):
        url, fake = server
        fake.login_style = "modern"
        fake.bypass_auth = True
        # Nothing is verified because nothing can be; the point is that it connects.
        assert client(url, password="anything").test()["api_version"] == "2.15.1"

    def test_test_reports_the_versions(self, server):
        url, _ = server
        info = client(url).test()
        assert info["app_version"] == "v5.2.3"
        assert info["api_version"] == "2.15.1"
        assert info["save_path"] == "/downloads"

    def test_bad_credentials_say_so(self, server):
        url, _ = server
        with pytest.raises(QBittorrentError, match="rejected those credentials"):
            client(url, password="wrong").test()

    def test_an_unreachable_host_names_itself(self):
        with pytest.raises(QBittorrentError, match="Cannot reach qBittorrent"):
            QBittorrent("http://127.0.0.1:1", username="a", password="b",
                        timeout=2).test()

    def test_an_api_too_old_for_file_selection_is_refused(self, server):
        url, fake = server
        fake.api_version = "2.6.0"
        with pytest.raises(QBittorrentError, match="too old"):
            client(url).test()

    def test_a_url_that_already_names_the_api_is_not_doubled(self, server):
        url, _ = server
        assert client(url + "/api/v2").base.count("/api/v2") == 1

    def test_no_username_means_authentication_is_bypassed(self, server):
        url, _ = server
        bare = QBittorrent(url, timeout=5)
        assert bare.test()["app_version"] == "v5.2.3"


class TestAnExpiredSession:
    def test_a_401_from_a_proxy_logs_in_again_like_a_403(self, server):
        url, fake = server
        qbit = client(url)
        qbit.test()
        fake.authenticated, fake.refusal = False, 401
        assert qbit.test()["app_version"] == "v5.2.3"


class TestAdd:
    def test_a_magnet_stops_itself_once_the_metadata_lands(self, server):
        url, fake = server
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=MAME"
        assert client(url).add(magnet, save_path="/downloads") == HASH
        form = fake.added[0]["form"]
        # A magnet has no file list, and a torrent that is simply stopped never
        # fetches one -- so there would be nothing to select. stopCondition runs it
        # only as far as the metadata, then stops it before any content arrives.
        assert form["stopCondition"] == ["MetadataReceived"]
        assert form["stopped"] == ["false"]
        assert form["savepath"] == ["/downloads"]

    def test_a_torrent_file_needs_no_stop_condition(self, server):
        url, fake = server
        # The file list is already in the .torrent, so it can just be added stopped.
        client(url).add(b"d4:infod4:name4:MAMEee")
        form = fake.added[0]["form"]
        assert form["stopped"] == ["true"]
        assert "stopCondition" not in form

    def test_starting_immediately_asks_for_no_stop_condition(self, server):
        url, fake = server
        client(url).add(f"magnet:?xt=urn:btih:{HASH}", stopped=False)
        form = fake.added[0]["form"]
        assert form["stopped"] == ["false"]
        assert "stopCondition" not in form

    def test_a_torrent_file_is_uploaded(self, server):
        url, fake = server
        assert client(url).add(b"d4:infod4:name4:MAMEee") == HASH
        assert fake.added[0]["upload"] == b"d4:infod4:name4:MAMEee"

    def test_the_category_and_tags_are_passed_through(self, server):
        url, fake = server
        client(url).add(f"magnet:?xt=urn:btih:{HASH}", category="marquee",
                        tags=["roms", "0.289"])
        form = fake.added[0]["form"]
        assert form["category"] == ["marquee"]
        assert form["tags"] == ["roms,0.289"]

    def test_the_infohash_is_read_from_the_magnet_when_the_client_is_silent(self, server):
        url, fake = server
        fake.handle_add_silent = True
        magnet = f"magnet:?xt=urn:btih:{HASH.upper()}"
        assert infohash_of_magnet(magnet) == HASH


class TestSelection:
    def test_select_only_skips_everything_else_first(self, server):
        url, fake = server
        result = client(url).select_only(HASH, [1])
        assert result == {"selected": 1, "skipped": 2}
        # Order matters: dropping to 0 first is what stops a previous selection
        # leaking into the new one.
        assert fake.prio_calls[0][1] == PRIO_SKIP
        assert fake.prio_calls[-1][1] == PRIO_NORMAL
        assert [f["priority"] for f in fake.files] == [0, 1, 0]

    def test_deselecting_everything_is_refused(self, server):
        url, _ = server
        with pytest.raises(QBittorrentError, match="every file"):
            client(url).select_only(HASH, [])

    def test_large_selections_are_batched(self, server):
        url, fake = server
        fake.files = [{"index": i, "name": f"MAME/{i}.zip", "size": 1, "priority": 1,
                       "progress": 0.0, "piece_range": [i, i]} for i in range(5000)]
        client(url).select_only(HASH, range(2500))
        # 44,166 ids in one request is a 400 KB body; they go in batches instead.
        assert len(fake.prio_calls) >= 4
        assert all(len(ids) <= 2000 for ids, _ in fake.prio_calls)
        assert sum(len(ids) for ids, _ in fake.prio_calls) == 5000

    def test_setting_no_priorities_makes_no_request(self, server):
        url, fake = server
        client(url).set_priority(HASH, [], PRIO_SKIP)
        assert fake.prio_calls == []


class TestMetadata:
    def test_waiting_returns_the_files_once_they_arrive(self, server):
        url, fake = server
        fake.metadata_after = 2
        files = client(url).wait_for_metadata(HASH, timeout=20, interval=0.01)
        assert len(files) == 3

    def test_waiting_gives_up_with_a_useful_message(self, server):
        url, fake = server
        fake.metadata_after = 10_000
        with pytest.raises(QBittorrentError, match="No metadata"):
            client(url).wait_for_metadata(HASH, timeout=0)

    def test_files_are_normalised(self, server):
        url, _ = server
        first = client(url).files(HASH)[0]
        assert first == {"index": 0, "path": "MAME/aaa.zip", "size": 100,
                         "priority": 1, "progress": 1.0, "piece_range": [0, 0]}


class TestStatus:
    def test_status_separates_selected_size_from_total(self, server):
        url, _ = server
        entry = client(url).status()[0]
        # The gap between these two is the saving this project exists to make.
        assert entry["size"] == 600
        assert entry["total_size"] == 600
        assert entry["phase"] == "downloading"

    def test_selection_shrinks_the_reported_size(self, server):
        url, _ = server
        handle = client(url)
        handle.select_only(HASH, [1])
        assert handle.one(HASH)["size"] == 200

    def test_start_falls_back_to_the_pre_five_spelling(self, server):
        url, fake = server
        fake.modern_start = False
        client(url).start(HASH)
        assert fake.started == ["torrents/resume"]


class TestMagnetParsing:
    def test_a_hex_infohash_is_lowercased(self):
        assert infohash_of_magnet(f"magnet:?xt=urn:btih:{HASH.upper()}") == HASH

    def test_a_base32_infohash_is_decoded(self):
        assert infohash_of_magnet(
            "magnet:?xt=urn:btih:32W353YAAAAAAAAAAAAAAAAAAAAAAAAI") == HASH

    def test_something_that_is_not_a_magnet_returns_nothing(self):
        assert infohash_of_magnet("https://example.invalid/x.torrent") is None


class TestIncludeNeverDeselects:
    """Adding to a selection must not be able to stop a torrent seeding.

    The user's client holds 1.3 TB of complete MAME sets. `select_only` would drop
    every unlisted file to priority 0, which on a finished torrent means it stops
    seeding those files -- so wanting *more* has to be a different call.
    """

    def test_only_skipped_files_are_raised(self, server):
        url, fake = server
        fake.files[0]["priority"] = 0
        fake.files[1]["priority"] = 1
        fake.files[2]["priority"] = 0
        result = client(url).include(HASH, [0, 1])
        assert result == {"raised": 1, "already_selected": 1}
        assert [f["priority"] for f in fake.files] == [1, 1, 0]

    def test_files_outside_the_request_are_left_exactly_as_they_were(self, server):
        url, fake = server
        for entry in fake.files:
            entry["priority"] = 1
        client(url).include(HASH, [0])
        assert [f["priority"] for f in fake.files] == [1, 1, 1]

    def test_nothing_to_raise_makes_no_request(self, server):
        url, fake = server
        for entry in fake.files:
            entry["priority"] = 1
        client(url).include(HASH, [0, 1, 2])
        assert fake.prio_calls == []

    def test_an_empty_request_is_harmless(self, server):
        url, fake = server
        assert client(url).include(HASH, [])["raised"] == 0
        assert fake.prio_calls == []


class TestNarrow:
    """The safe way to shrink a selection, and the one the app uses.

    A freshly added set arrives with all 44,166 files at normal priority, so raising
    priorities raises nothing and starting it fetches 1.3 TB. A finished set is being
    seeded, so dropping any of it to 0 stops that. `narrow` is both answers at once:
    skip what is neither wanted nor already here, and nothing else.
    """

    def test_a_set_with_nothing_downloaded_is_cut_to_the_selection(self, server):
        url, fake = server
        for entry in fake.files:
            entry["progress"] = 0.0
        result = client(url).narrow(HASH, [1])
        assert [f["priority"] for f in fake.files] == [0, 1, 0]
        assert (result["selected"], result["skipped"]) == (1, 2)

    def test_a_complete_file_is_never_dropped(self, server):
        url, fake = server
        # The fixture's first file is already finished; this is the one being asked
        # for, and the one being protected, side by side.
        result = client(url).narrow(HASH, [1])
        assert [f["priority"] for f in fake.files] == [1, 1, 0]
        assert (result["wanted"], result["kept_complete"]) == (1, 1)

    def test_a_finished_set_loses_nothing_at_all(self, server):
        url, fake = server
        for entry in fake.files:
            entry["progress"] = 1.0
        result = client(url).narrow(HASH, [0])
        assert [f["priority"] for f in fake.files] == [1, 1, 1]
        assert result["skipped"] == 0

    def test_skipping_happens_before_selecting(self, server):
        url, fake = server
        fake.files[1]["priority"] = 0
        client(url).narrow(HASH, [1])
        # A previous selection must not leak into the new one.
        assert fake.prio_calls[0][1] == PRIO_SKIP
        assert fake.prio_calls[-1][1] == PRIO_NORMAL

    def test_deselecting_everything_is_refused(self, server):
        url, fake = server
        for entry in fake.files:
            entry["progress"] = 0.0
        with pytest.raises(QBittorrentError, match="every file"):
            client(url).narrow(HASH, [])

    def test_a_torrent_with_no_file_list_is_an_error(self, server):
        url, fake = server
        fake.files = []
        with pytest.raises(QBittorrentError, match="file list"):
            client(url).narrow(HASH, [0])

    def test_a_selection_that_has_not_moved_sends_nothing(self, server):
        url, fake = server
        client(url).narrow(HASH, [1])
        fake.prio_calls.clear()
        client(url).narrow(HASH, [1])
        assert fake.prio_calls == []

    def test_a_raised_priority_is_left_raised(self, server):
        """High (6) or Maximum (7), set by hand in qBittorrent, came back Normal."""
        url, fake = server
        fake.files[1]["priority"] = 7
        client(url).narrow(HASH, [1])
        assert fake.files[1]["priority"] == 7


class TestAClientThatStopsAnswering:
    def test_a_read_timeout_is_a_client_error_not_a_traceback(self):
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        accepted = []

        def hold():
            try:
                conn, _addr = listener.accept()
                accepted.append(conn)
                time.sleep(3)
            except OSError:
                pass

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        try:
            with pytest.raises(QBittorrentError, match="stopped answering"):
                QBittorrent(f"http://127.0.0.1:{port}", username="a", password="b",
                            timeout=0.5).test()
        finally:
            for conn in accepted:
                conn.close()
            listener.close()
