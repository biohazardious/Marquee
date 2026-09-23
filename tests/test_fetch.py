"""Fetching version-matched support files, with the network stood in for."""
import json
import os

import pytest

from marquee import fetch as MameFetch

COMMITS = [
    {"sha": "sha289", "commit": {"message": "catver: Bump 0.289 version."}},
    {"sha": "sha288", "commit": {"message": "catver: Bump 0.288 version."}},
    {"sha": "sha262", "commit": {"message": "catver: Bump 0.262 version."}},
    {"sha": "shaxxx", "commit": {"message": "Fix a typo in the readme"}},
]


def catlist_bytes(version):
    return (f"[FOLDER_SETTINGS]\n\n;; CATLIST.ini {version} / 01-Jan-25 / MAME {version} ;;\n"
            f"\n[ROOT_FOLDER]\n[Arcade: Maze / Misc.]\ngoodgame\n").encode()


@pytest.fixture
def fake_network(monkeypatch):
    """Serve the commit list and raw files without touching GitHub."""
    calls = []

    def fake_request(url, _timeout):
        calls.append(url)
        if "api.github.com" in url:
            return json.dumps(COMMITS).encode()
        for version, sha in (("0.289", "sha289"), ("0.288", "sha288"), ("0.262", "sha262")):
            if f"/{sha}/" in url:
                return catlist_bytes(version)
        if "/main/" in url:
            return catlist_bytes("0.289")
        raise MameFetch.SupportFileError(f"unexpected url {url}")

    monkeypatch.setattr(MameFetch, "_request", fake_request)
    return calls


class TestVersionListing:
    def test_lists_versions_oldest_first(self, fake_network):
        assert MameFetch.available_versions() == ["0.262", "0.288", "0.289"]

    def test_commits_without_a_version_are_ignored(self, fake_network):
        assert "shaxxx" not in str(MameFetch.available_versions())

    def test_list_is_cached_after_the_first_call(self, fake_network):
        MameFetch.available_versions()
        before = len(fake_network)
        MameFetch.available_versions()
        assert len(fake_network) == before

    def test_refresh_refetches(self, fake_network):
        MameFetch.available_versions()
        before = len(fake_network)
        MameFetch.available_versions(refresh=True)
        assert len(fake_network) == before + 1


class TestFetch:
    def test_fetches_the_requested_version(self, fake_network):
        path = MameFetch.fetch_support_file("catlist.ini", "0.288")
        assert "0.288" in open(path).read()
        assert any("/sha288/" in url for url in fake_network)

    def test_latest_uses_the_main_branch(self, fake_network):
        MameFetch.fetch_support_file("catlist.ini")
        assert any("/main/" in url for url in fake_network)

    def test_second_request_comes_from_the_cache(self, fake_network):
        MameFetch.fetch_support_file("catlist.ini", "0.288")
        before = len(fake_network)
        MameFetch.fetch_support_file("catlist.ini", "0.288")
        assert len(fake_network) == before

    def test_version_outside_the_archive_names_the_range(self, fake_network):
        with pytest.raises(MameFetch.SupportFileError) as error:
            MameFetch.fetch_support_file("catlist.ini", "0.252")
        message = str(error.value)
        assert "0.262" in message and "0.289" in message
        assert MameFetch.ARCHIVE_URL in message

    def test_unknown_support_file(self, fake_network):
        with pytest.raises(MameFetch.SupportFileError, match="Unknown support file"):
            MameFetch.fetch_support_file("nosuch.ini", "0.289")

    def test_wrong_version_in_the_payload_is_rejected(self, monkeypatch, fake_network):
        """A reorganised archive must not quietly hand over another romset's categories."""
        monkeypatch.setattr(MameFetch, "_request",
                            lambda url, _t: json.dumps(COMMITS).encode()
                            if "api.github" in url else catlist_bytes("0.999"))
        with pytest.raises(MameFetch.SupportFileError, match="reports MAME 0.999"):
            MameFetch.fetch_support_file("catlist.ini", "0.288")

    def test_a_rejected_download_leaves_no_partial_file(self, monkeypatch, fake_network):
        monkeypatch.setattr(MameFetch, "_request",
                            lambda url, _t: json.dumps(COMMITS).encode()
                            if "api.github" in url else catlist_bytes("0.999"))
        with pytest.raises(MameFetch.SupportFileError):
            MameFetch.fetch_support_file("catlist.ini", "0.288")
        target = MameFetch.cached_support_file("catlist.ini", "0.288")
        assert not os.path.exists(target)
        assert not os.path.exists(target + ".part")


class TestErrors:
    def test_rate_limit_explains_itself(self, monkeypatch):
        import urllib.error

        def rate_limited(url, timeout):
            raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)

        monkeypatch.setattr(MameFetch.urllib.request, "urlopen", rate_limited)
        with pytest.raises(MameFetch.SupportFileError, match="60 requests an hour"):
            MameFetch._request("https://api.github.com/x", 5)

    def test_unreachable_host_is_reported(self, monkeypatch):
        import urllib.error

        def unreachable(_request, timeout):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(MameFetch.urllib.request, "urlopen", unreachable)
        with pytest.raises(MameFetch.SupportFileError, match="Could not reach"):
            MameFetch._request("https://example.invalid/x", 5)


class TestAListxmlTheArchiveNoLongerHolds:
    """The archive's 0.287 commit is the one that removed arcade.xml: the version is
    listed, and its raw URL answers 404. Say that, not "HTTP 404 fetching <url>"."""

    def test_it_says_what_went_wrong(self, monkeypatch):
        from marquee import fetch
        from marquee.errors import SourceNotFoundError
        monkeypatch.setattr(fetch, "_commit_history",
                            lambda **kw: [("0.287", "a" * 40), ("0.286", "b" * 40)])

        def refused(url, timeout):
            raise SourceNotFoundError(f"HTTP 404 fetching {url}")
        monkeypatch.setattr(fetch, "_request", refused)
        with pytest.raises(SourceNotFoundError) as error:
            fetch.fetch_xml("0.287")
        assert "not there to download" in str(error.value)

    def test_a_later_commit_for_the_same_release_is_tried(self, monkeypatch, tmp_path):
        from marquee import fetch
        from marquee.errors import SourceNotFoundError
        monkeypatch.setattr(fetch, "_commit_history",
                            lambda **kw: [("0.287", "a" * 40), ("0.287", "c" * 40)])
        body = b'<?xml version="1.0"?><mame build="0.287 (mame0287)"></mame>'

        def answer(url, timeout):
            if "a" * 40 in url:
                raise SourceNotFoundError(f"HTTP 404 fetching {url}")
            return body
        monkeypatch.setattr(fetch, "_request", answer)
        path = fetch.fetch_xml("0.287")
        with open(path, "rb") as handle:
            assert handle.read() == body
