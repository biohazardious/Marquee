"""Cached screenshots.

libretro-thumbnails keys its files on MAME's own <description>, so the URL is
derivable and there is nothing to scrape. These tests never touch the network: the
fetcher is pointed at a local file server.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from marquee import art

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def source(monkeypatch):
    """Stands in for raw.githubusercontent.com."""
    served = {"Galaga.png", "Pac-Man _ Puck.png"}
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            from urllib.parse import unquote
            name = unquote(self.path.rsplit("/", 1)[-1])
            asked.append(name)
            if name in served:
                self.send_response(200)
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)
            elif name == "Rate Limited.png":
                self.send_error(429)
            else:
                self.send_error(404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(art, "REPO", f"http://127.0.0.1:{httpd.server_address[1]}")
    yield asked
    httpd.shutdown()


class TestNaming:
    def test_the_url_is_derived_from_the_description(self):
        assert art.url_for("Galaga (Namco rev. B)").endswith(
            "/Named_Titles/Galaga%20%28Namco%20rev.%20B%29.png")

    def test_characters_a_filesystem_dislikes_become_underscores(self):
        # This is libretro's own rule, not ours; getting it wrong 404s everything.
        assert art.safe_name('Pac-Man / Puck-Man "x"') == "Pac-Man _ Puck-Man _x_"

    def test_no_description_means_no_url(self):
        assert art.url_for("") is None
        assert art.path_for(None) is None

    def test_every_kind_has_its_own_folder(self):
        titles = art.art_dir("Named_Titles")
        snaps = art.art_dir("Named_Snaps")
        assert titles != snaps


class TestFetch:
    def test_a_published_picture_is_cached(self, cache, source):
        assert art.fetch_one("Galaga") is True
        assert art.have("Galaga")
        with open(art.path_for("Galaga"), "rb") as handle:
            assert handle.read() == PNG

    def test_a_machine_with_no_picture_is_not_an_error(self, cache, source):
        # Coverage is about three quarters of the set; the gaps are normal.
        assert art.fetch_one("Nothing Like This Exists") is False
        assert not art.have("Nothing Like This Exists")

    def test_a_cached_picture_is_not_asked_for_again(self, cache, source):
        art.fetch_one("Galaga")
        art.fetch_one("Galaga")
        assert source.count("Galaga.png") == 1

    def test_no_half_written_file_is_left_behind(self, cache, source):
        art.fetch_one("Galaga")
        leftovers = [name for name in os.listdir(art.art_dir()) if name.endswith(".part")]
        assert leftovers == []


class TestDownload:
    def test_it_reports_what_happened(self, cache, source):
        result = art.download(["Galaga", "Pac-Man / Puck", "Ghost Game"])
        assert result["total"] == 3
        assert result["fetched"] == 2
        assert result["missing"] == 1

    def test_duplicates_are_fetched_once(self, cache, source):
        result = art.download(["Galaga", "Galaga", "Galaga"])
        assert result["total"] == 1
        assert source.count("Galaga.png") == 1

    def test_progress_reaches_the_total(self, cache, source):
        seen = []
        art.download(["Galaga", "Ghost Game"], on_progress=lambda d, t: seen.append((d, t)))
        assert seen[-1] == (2, 2)

    def test_stopping_leaves_the_rest_alone(self, cache, source):
        art.download(["Galaga", "Pac-Man / Puck"], should_continue=lambda: False)
        assert art.cached_count() == 0

    def test_a_second_run_finds_everything_cached(self, cache, source):
        art.download(["Galaga"])
        again = art.download(["Galaga"])
        assert again["cached"] == 1
        assert again["fetched"] == 0

    def test_nothing_to_do_is_harmless(self, cache, source):
        assert art.download([])["total"] == 0


class TestCacheSize:
    def test_counting_an_empty_cache(self, cache):
        assert art.cached_count() == 0
        assert art.cache_bytes() == 0

    def test_counting_what_is_there(self, cache, source):
        art.download(["Galaga", "Pac-Man / Puck"])
        assert art.cached_count() == 2
        assert art.cache_bytes() == 2 * len(PNG)


class TestStopping:
    def test_a_stopped_run_still_reaches_its_total(self, cache, source):
        # A progress bar frozen half way looks hung; it has to finish.
        seen = []
        art.download(["Galaga", "Pac-Man / Puck"], should_continue=lambda: False,
                     on_progress=lambda done, total: seen.append((done, total)))
        assert seen[-1] == (2, 2)

    def test_stopping_partway_leaves_what_arrived(self, cache, source):
        calls = {"n": 0}

        def once_only():
            calls["n"] += 1
            return calls["n"] <= 1

        art.download(["Galaga", "Pac-Man / Puck"], should_continue=once_only,
                     workers=1)
        assert art.cached_count() <= 1


class TestASourceThatCannotBeAsked:
    def test_a_rate_limit_is_a_failure_not_a_missing_picture(self, cache, source):
        summary = art.download(["Rate Limited", "Nobody Drew This"])
        assert summary["failed"] == 1
        assert summary["missing"] == 1
        assert "429" in summary["error"]
        assert not art.have("Rate Limited")
