"""The Pleasuredome index.

The fixture is a trimmed copy of the live page taken on 2026-09-14, keeping every link
shape and none of the prose. Its infohashes are deliberately fake -- the parser reads a
page's shape, never a torrent, so there is no reason for real ones to live in a public
repository.
"""
import os

import pytest

from marquee.indexers import ROLLBACK
from marquee.indexers.pleasuredome import (Pleasuredome, datfile_name, parse)
from marquee.indexers.release import CHDS, NON_MERGED, ROMS, SL_ROMS, Release

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pleasuredome.html")


@pytest.fixture(scope="module")
def page():
    with open(FIXTURE, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def releases(page):
    return parse(page)


class TestParse:
    def test_every_magnet_on_the_page_becomes_a_release(self, releases):
        assert len(releases) == 17

    def test_infohashes_are_forty_hex_lowercase(self, releases):
        for release in releases:
            assert len(release.infohash) == 40
            assert release.infohash == release.infohash.lower()
            int(release.infohash, 16)

    def test_the_non_merged_rom_set_is_recognised(self, releases):
        found = [r for r in releases if r.kind == ROMS and r.variant == NON_MERGED]
        assert len(found) == 1
        assert found[0].version == "0.289"
        assert found[0].infohash == "deadbeef00000000000000000000000000000008"

    def test_all_three_rom_variants_are_distinguished(self, releases):
        variants = {r.variant for r in releases if r.kind == ROMS and r.is_full_set}
        assert variants == {"merged", "non-merged", "split"}

    def test_update_sets_record_both_ends(self, releases):
        updates = [r for r in releases if r.from_version]
        assert updates
        rom_update = [r for r in updates if r.kind == ROMS][0]
        assert (rom_update.from_version, rom_update.version) == ("0.288", "0.289")
        assert not rom_update.is_full_set

    def test_update_sets_keep_the_kind_they_update(self, releases):
        kinds = {r.kind for r in releases if r.from_version}
        # ROMs, CHDs, software list ROMs and CHDs, and EXTRAs all publish updates.
        assert {ROMS, CHDS, SL_ROMS} <= kinds

    def test_rollback_sets_are_not_mistaken_for_full_sets(self, releases):
        rollbacks = [r for r in releases if r.kind == ROLLBACK]
        assert len(rollbacks) == 2
        # A rollback CHD set at 0.287 must never outrank the real 0.288 CHD set.
        assert all(r.kind not in (ROMS, CHDS) for r in rollbacks)

    def test_software_list_sets_are_kept_apart_from_arcade_sets(self, releases):
        arcade = [r for r in releases if r.kind == ROMS]
        assert all("Software List" not in r.name for r in arcade)

    def test_datfiles_are_attached_by_set_name(self, releases):
        non_merged = [r for r in releases
                      if r.kind == ROMS and r.variant == NON_MERGED][0]
        assert non_merged.datfile_url.endswith("(non-merged).zip")

    def test_dir2dat_datfiles_are_ignored(self, releases):
        # Each set has at most one datfile, and it is never the dir2dat variant.
        for release in releases:
            assert "dir2dat" not in (release.datfile_url or "")

    def test_a_page_with_no_magnets_yields_nothing(self):
        assert parse("<html><body><p>nothing here</p></body></html>") == []

    def test_a_base32_infohash_is_converted(self):
        # The base32 form of the fixture's 0.289 non-merged infohash.
        page = ('<a href="magnet:?xt=urn:btih:'
                '32W353YAAAAAAAAAAAAAAAAAAAAAAAAI">MAME 0.289 ROMs (non-merged)</a>')
        found = parse(page)
        assert len(found) == 1
        assert found[0].infohash == "deadbeef00000000000000000000000000000008"


class TestCurrent:
    def test_picks_the_newest_non_merged_roms_and_merged_chds(self, page):
        current = Pleasuredome().current(page)
        assert current["roms"].name == "MAME 0.289 ROMs (non-merged)"
        assert current["chds"].name == "MAME 0.288 CHDs (merged)"

    def test_roms_and_chds_are_allowed_to_be_different_versions(self, page):
        current = Pleasuredome().current(page)
        # This is the normal state of the world, not an error to reconcile.
        assert current["roms"].version != current["chds"].version

    def test_missing_sets_come_back_as_none_rather_than_raising(self):
        current = Pleasuredome().current("<html></html>")
        assert current == {"roms": None, "chds": None}


class TestRelease:
    def test_magnet_uri_is_rebuilt_when_only_the_hash_is_known(self):
        release = Release(kind=ROMS, version="0.289", infohash="ab" * 20,
                          name="MAME 0.289 ROMs (non-merged)",
                          trackers=("udp://tracker.example:1337/announce",))
        uri = release.magnet_uri()
        assert uri.startswith(f"magnet:?xt=urn:btih:{'ab' * 20}")
        assert "tracker.example" in uri

    def test_the_original_magnet_is_preferred_when_present(self):
        release = Release(kind=ROMS, version="0.289", infohash="ab" * 20,
                          name="x", magnet="magnet:?xt=urn:btih:original")
        assert release.magnet_uri() == "magnet:?xt=urn:btih:original"

    def test_datfile_name_is_url_decoded(self):
        url = ("https://github.com/x/raw/gh-pages/mame/"
               "MAME%200.289%20ROMs%20(non-merged).zip")
        assert datfile_name(url) == "MAME 0.289 ROMs (non-merged).zip"


class TestWhereTheListingIsRead:
    def test_the_published_page_by_default(self, monkeypatch):
        from marquee.indexers import pleasuredome
        monkeypatch.delenv("MARQUEE_INDEX_URL", raising=False)
        assert pleasuredome.Pleasuredome().url == pleasuredome.INDEX_URL

    def test_a_mirror_by_environment(self, monkeypatch):
        from marquee.indexers import pleasuredome
        monkeypatch.setenv("MARQUEE_INDEX_URL", "http://mirror.lan/mame/index.html")
        assert pleasuredome.Pleasuredome().url == "http://mirror.lan/mame/index.html"
