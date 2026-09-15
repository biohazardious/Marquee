"""gamelist.xml, the file EmulationStation reads.

Without one a Batocera box shows "mslug"; with one it shows Metal Slug, its year, its
manufacturer and its picture. Everything in it is already known, so nothing here
fetches or guesses.
"""
import os
import xml.etree.ElementTree as ET

import pytest

from marquee import art, gamelist
from marquee.backends.local import LocalCopy
from marquee.plan import CopyPlan, PlannedItem


def machine(name="mslug", **overrides):
    fields = dict(
        name=name, description="Metal Slug - Super Vehicle-001",
        category="Platform / Shooter Scrolling", genre="Platform",
        folder="Platform/Shooter Scrolling", year="1996", manufacturer="Nazca",
        players=2, display={"type": "raster", "rotate": 0}, rom_source=f"/x/{name}.zip",
        rom_bytes=1000)
    fields.update(overrides)
    return PlannedItem(**fields)


def field(game, tag):
    found = game.find(tag)
    return found.text if found is not None else None


class TestEntry:
    def test_the_path_includes_the_genre_folder(self):
        # One gamelist at the library root, the way EmulationStation reads a system.
        game = gamelist.entry(machine())
        assert field(game, "path") == "./Platform/Shooter Scrolling/mslug.zip"

    def test_it_uses_mames_own_title(self):
        assert field(gamelist.entry(machine()), "name") == \
            "Metal Slug - Super Vehicle-001"

    def test_the_year_becomes_a_release_date(self):
        # ES wants a timestamp; MAME gives a year.
        assert field(gamelist.entry(machine()), "releasedate") == "19960101T000000"

    def test_an_unusable_year_is_left_out(self):
        for year in ("19??", "", None, "198?"):
            assert field(gamelist.entry(machine(year=year)), "releasedate") is None

    def test_the_manufacturer_fills_both_credits(self):
        game = gamelist.entry(machine())
        assert field(game, "developer") == field(game, "publisher") == "Nazca"

    def test_the_genre_is_the_catlist_category(self):
        assert field(gamelist.entry(machine()), "genre") == "Platform / Shooter Scrolling"

    def test_it_falls_back_to_the_genre_when_there_is_no_category(self):
        assert field(gamelist.entry(machine(category="")), "genre") == "Platform"

    def test_the_description_says_something_useful(self):
        text = field(gamelist.entry(machine()), "desc")
        assert "Nazca, 1996." in text
        assert "2 players." in text

    def test_a_clone_says_what_it_is_a_version_of(self):
        text = field(gamelist.entry(machine(cloneof="mslugx")), "desc")
        assert "A version of mslugx." in text

    def test_a_vertical_screen_is_mentioned(self):
        item = machine(display={"type": "raster", "rotate": 90})
        assert "Vertical raster" in field(gamelist.entry(item), "desc")

    def test_a_chd_is_mentioned(self):
        item = machine(chd_sources=["/x/dlair.chd"], chd_sizes=[10])
        assert "Needs a CHD." in field(gamelist.entry(item), "desc")

    def test_adult_titles_are_flagged(self):
        assert field(gamelist.entry(machine(mature=True)), "adult") == "true"
        assert field(gamelist.entry(machine()), "adult") is None

    def test_an_image_is_only_written_when_there_is_one(self):
        assert field(gamelist.entry(machine(), "./images/mslug.png"), "image") == \
            "./images/mslug.png"
        assert field(gamelist.entry(machine()), "image") is None


class TestDocument:
    def test_games_are_listed_alphabetically_by_title(self):
        root = gamelist.document([
            machine("zed", description="Zed"),
            machine("alpha", description="Alpha"),
        ])
        assert [field(game, "name") for game in root] == ["Alpha", "Zed"]

    def test_it_is_a_gamelist_element(self):
        assert gamelist.document([machine()]).tag == "gameList"

    def test_it_renders_as_a_parseable_document(self):
        body = gamelist.render(gamelist.document([machine()]))
        assert body.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert ET.fromstring(body.split("?>", 1)[1]).tag == "gameList"

    def test_an_empty_folder_still_produces_a_document(self):
        assert list(gamelist.document([])) == []


class TestWrite:
    @pytest.fixture
    def library(self, tmp_path):
        return LocalCopy(str(tmp_path / "library")), tmp_path / "library"

    def test_one_gamelist_lands_at_the_library_root(self, library):
        backend, root = library
        plan = CopyPlan(items=[machine("a", folder="Maze"),
                               machine("b", folder="Shooter")])
        result = gamelist.write(plan, backend, copy_images=False)
        assert result["games"] == 2
        assert (root / "gamelist.xml").exists()
        # Not one per genre folder: those folders have to stay prunable when the last
        # game moves out of them.
        assert not (root / "Maze" / "gamelist.xml").exists()

    def test_it_covers_every_folder_in_one_file(self, library):
        backend, root = library
        plan = CopyPlan(items=[machine("a", folder="Maze", description="A"),
                               machine("b", folder="Shooter", description="B")])
        gamelist.write(plan, backend, copy_images=False)
        body = (root / "gamelist.xml").read_text()
        assert "./Maze/a.zip" in body
        assert "./Shooter/b.zip" in body

    def test_what_it_writes_is_valid_xml(self, library):
        backend, root = library
        gamelist.write(CopyPlan(items=[machine()]), backend, copy_images=False)
        body = (root / "gamelist.xml").read_text()
        root_element = ET.fromstring(body.split("?>", 1)[1])
        assert [game.find("name").text for game in root_element] == \
            ["Metal Slug - Super Vehicle-001"]

    def test_cached_artwork_is_placed_beside_the_roms(self, library, tmp_path,
                                                      monkeypatch):
        backend, root = library
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        picture = art.path_for("Metal Slug - Super Vehicle-001")
        os.makedirs(os.path.dirname(picture), exist_ok=True)
        with open(picture, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nx")

        result = gamelist.write(CopyPlan(items=[machine()]), backend)
        assert result["images"] == 1
        assert (root / "images" / "mslug.png").exists()
        assert "./images/mslug.png" in (root / "gamelist.xml").read_text()

    def test_a_machine_with_no_artwork_still_gets_an_entry(self, library, tmp_path,
                                                          monkeypatch):
        backend, root = library
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        result = gamelist.write(CopyPlan(items=[machine()]), backend)
        assert result["images"] == 0
        body = (root / "gamelist.xml").read_text()
        assert "<image>" not in body
        assert "Metal Slug" in body

    def test_rewriting_does_not_recopy_the_pictures(self, library, tmp_path,
                                                     monkeypatch):
        backend, _root = library
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        picture = art.path_for("Metal Slug - Super Vehicle-001")
        os.makedirs(os.path.dirname(picture), exist_ok=True)
        with open(picture, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nx")
        gamelist.write(CopyPlan(items=[machine()]), backend)
        again = gamelist.write(CopyPlan(items=[machine()]), backend)
        assert again["images"] == 0

    def test_progress_reaches_the_total(self, library, tmp_path, monkeypatch):
        backend, _root = library
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        seen = []
        plan = CopyPlan(items=[machine("a", folder="Maze"),
                               machine("b", folder="Shooter")])
        gamelist.write(plan, backend,
                       on_progress=lambda done, total: seen.append((done, total)))
        assert seen[-1] == (2, 2)


class TestMatureMarker:
    """catlist marks adult titles by appending a literal " * Mature * " to the section.

    That is syntax, not a genre. Leaking it into the console's genre list is the kind
    of detail that makes a library look machine-generated.
    """

    def item(self):
        return machine(category="Puzzle / Multi-Games * Mature *", mature=True)

    def test_the_marker_is_not_shown_as_a_genre(self):
        assert field(gamelist.entry(self.item()), "genre") == "Puzzle / Multi-Games"

    def test_nor_in_the_description(self):
        assert "Mature" not in field(gamelist.entry(self.item()), "desc")

    def test_the_flag_still_says_it(self):
        assert field(gamelist.entry(self.item()), "adult") == "true"

    def test_an_ordinary_category_is_untouched(self):
        assert field(gamelist.entry(machine()), "genre") == "Platform / Shooter Scrolling"

    def test_a_category_that_is_only_the_marker_falls_back_to_the_genre(self):
        item = machine(category="* Mature *", genre="Puzzle")
        assert field(gamelist.entry(item), "genre") == "Puzzle"
