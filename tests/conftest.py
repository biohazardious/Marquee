import configparser
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
sys.path.insert(0, PROJECT_ROOT)

from marquee import sources  # noqa: E402
from marquee.config import Config  # noqa: E402
from marquee.reporting import CollectingReporter  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep every test out of the user's real ~/.cache/marquee."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def isolated_search(monkeypatch, tmp_path):
    """Keep discovery tests off whatever MAME files the host machine happens to have."""
    monkeypatch.setattr(sources, "SUPPORT_DIRS", ())
    monkeypatch.setattr(sources, "XML_EXTRA_DIRS", ())
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def reporter():
    return CollectingReporter()


@pytest.fixture
def xml_path():
    return os.path.join(FIXTURES, "mame.xml")


@pytest.fixture
def catlist_path():
    return os.path.join(FIXTURES, "catlist.ini")


@pytest.fixture
def screenless_path():
    return os.path.join(FIXTURES, "screenless.ini")


@pytest.fixture
def machines(xml_path):
    return sources.extract_machines(xml_path)["machines"]


@pytest.fixture
def catlist(catlist_path):
    parser = configparser.ConfigParser(allow_no_value=True)
    parser.read(catlist_path)
    return parser


@pytest.fixture
def config():
    return Config(allow_mature=False,
                  blacklist_genres=["Board Game"],
                  blacklist_roms=["blockedgame"],
                  rom_dir="/nonexistent/roms",
                  chd_dir="/nonexistent/chds",
                  copy_path="/nonexistent/out")


@pytest.fixture
def romset(tmp_path, config):
    """A tiny on-disk romset, with the Config pointed at it."""
    rom_dir = tmp_path / "roms"
    chd_dir = tmp_path / "chds"
    out_dir = tmp_path / "out"
    for directory in (rom_dir, chd_dir, out_dir):
        directory.mkdir()

    # boardgame1 is present but blacklisted by genre: its weight still has to be
    # measurable so the Selection tab can show what excluding it saves.
    for name in ("goodgame", "impgame", "dotgame", "maturegame", "nocat", "boardgame1",
                 "twodisk", "parentchd", "clonemerged", "cloneown", "clonestray"):
        (rom_dir / f"{name}.zip").write_bytes(b"rom" * 100)

    (chd_dir / "twodisk").mkdir()
    (chd_dir / "twodisk" / "ok.chd").write_bytes(b"chd" * 200)
    (chd_dir / "parentchd").mkdir()
    (chd_dir / "parentchd" / "pdisk.chd").write_bytes(b"chd" * 200)
    (chd_dir / "cloneown").mkdir()
    (chd_dir / "cloneown" / "odisk.chd").write_bytes(b"chd" * 300)
    # clonestray has a disk of its own, but the collection keeps it in the parent's
    # folder -- which is what real CHD sets do.
    (chd_dir / "parentchd" / "sdisk.chd").write_bytes(b"chd" * 400)

    config.rom_dir = str(rom_dir)
    config.chd_dir = str(chd_dir)
    config.copy_path = str(out_dir) + os.sep
    return {"rom_dir": rom_dir, "chd_dir": chd_dir, "out_dir": out_dir}


@pytest.fixture
def categorised(machines, catlist, config):
    """The keeper dict, filtered and categorised, ready to plan from."""
    from marquee import catalog
    screenless = catalog.derive_screenless(machines)
    kept = catalog.filter_machines(machines, screenless, config)
    return catalog.categorize(kept, catlist, config)
