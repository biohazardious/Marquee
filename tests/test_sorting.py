"""Every list can be ordered by name, size or genre, and the order is the whole list's.

A page shows 200 rows of thousands; sorting what came back would only reorder the
first page. So the server sorts before it cuts, and the trees' branches follow.
"""
import pytest

from marquee import pipeline
from marquee.web import views


@pytest.fixture(autouse=True)
def fixture_files(config, xml_path, catlist_path):
    config.mame_xml, config.catlist_ini = xml_path, catlist_path


@pytest.fixture
def planned(config, romset):
    built, _resolution = pipeline.build_plan(config)
    return built


def titles(rows):
    return [row["description"] for row in rows]


class TestTheLibrary:
    def test_by_genre_then_title(self, planned):
        rows = views.machine_rows(planned, sort="genre", descending=False, limit=500)["rows"]
        keys = [(row["genre"].lower(), row["description"].lower()) for row in rows]
        assert keys == sorted(keys)

    def test_by_name_either_way(self, planned):
        up = titles(views.machine_rows(planned, sort="description", descending=False)["rows"])
        down = titles(views.machine_rows(planned, sort="description", descending=True)["rows"])
        assert up == sorted(up, key=str.lower) and down == up[::-1]


class TestRows:
    ROWS = [{"name": "b", "description": "Bravo", "genre": "Maze", "bytes": 10, "year": "1990"},
            {"name": "a", "description": "alpha", "genre": "Shooter", "bytes": 30, "year": "1985"},
            {"name": "c", "description": "Charlie", "genre": "Maze", "bytes": 20, "year": ""}]

    def test_name_ignores_case(self):
        assert titles(views.sort_rows(list(self.ROWS), "name", False)) == ["alpha", "Bravo", "Charlie"]

    def test_size_largest_first(self):
        assert titles(views.sort_rows(list(self.ROWS), "size", True)) == ["alpha", "Charlie", "Bravo"]

    def test_genre_then_title(self):
        assert titles(views.sort_rows(list(self.ROWS), "genre", False)) == ["Bravo", "Charlie", "alpha"]

    def test_equal_sizes_still_read_a_to_z_when_largest_is_first(self):
        rows = [{"description": name, "bytes": 0} for name in ("Charlie", "alpha", "Bravo")]
        assert titles(views.sort_rows(rows, "size", True)) == ["alpha", "Bravo", "Charlie"]

    def test_an_unknown_key_is_size(self):
        assert titles(views.sort_rows(list(self.ROWS), "nonsense", True)) == ["alpha", "Charlie", "Bravo"]


class TestWanted:
    def test_the_order_is_the_whole_list_s_before_the_page_is_cut(self, config, romset):
        for name in ("goodgame", "impgame", "dotgame"):
            (romset["rom_dir"] / f"{name}.zip").unlink()
        planned, _resolution = pipeline.build_plan(config)
        names = sorted(planned.needed)
        assert len(names) >= 3, "the fixture plan wants games that are not on disk"
        sizes = {name: (index + 1) * 100 for index, name in enumerate(names)}
        page = views.missing_rows(planned, sizes, limit=2, sort="size", descending=True)
        biggest = sorted(names, key=lambda name: -sizes[name])[:2]
        assert [row["name"] for row in page["rows"]] == biggest
        assert page["total"] == len(names) and page["shown"] == 2

    def test_genres_follow_the_order(self, planned):
        page = views.missing_rows(planned, {}, sort="name", descending=False)
        names = [genre["name"].lower() for genre in page["genres"]]
        assert names == sorted(names)


class TestTransfer:
    @pytest.fixture
    def compared(self, planned, config):
        planned.sync = pipeline.compare_destination(planned, config)
        return planned

    def test_rows_and_branches_by_name(self, compared):
        payload = views.changes_payload(compared, kind="new", sort="name", descending=False)
        got = [row["description"].lower() for row in payload["rows"]]
        assert got == sorted(got)
        tree = views.changes_payload(compared, kind="new", group=True, sort="name",
                                     descending=False)["tree"]
        genres = [genre["name"].lower() for genre in tree]
        assert genres == sorted(genres)

    def test_rows_by_size_largest_first(self, compared):
        payload = views.changes_payload(compared, kind="new", sort="size", descending=True)
        sizes = [row["bytes"] for row in payload["rows"]]
        assert sizes == sorted(sizes, reverse=True)
