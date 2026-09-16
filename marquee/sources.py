"""Locating and reading the MAME data this tool depends on.

Two of the four MAME Extras ini files the script used to require are redundant:
genre.ini repeats the leading component of every catlist.ini section, and
screenless.ini repeats which machines the XML gives no <display> element. Both are
derived here instead, which leaves catlist.ini as the only external file and lets
the rest be found rather than configured.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from . import atomic
from .reporting import Reporter

CACHE_VERSION = 7

# ";; CATLIST.ini 0.252 / 06-Mar-23 / MAME 0.252 ;;"
INI_VERSION_RE = re.compile(r';;\s*([A-Za-z]+)\.ini\s+([\d.]+).*?MAME\s+([\d.]+)', re.IGNORECASE)
# build="0.252 (mame0252)"
BUILD_RE = re.compile(r'(\d+\.\d+)')

# Extra places to look for a mame*.xml, kept separate so tests can narrow them.
XML_EXTRA_DIRS = ("~/Downloads", ".")

SUPPORT_DIRS = (
    "~/.mame",
    "~/.mame/folders",
    "/usr/share/mame",
    "/usr/share/mame/folders",
    "/usr/local/share/mame",
    "/usr/local/share/mame/folders",
    "/userdata/system/configs/mame",          # Batocera
    "/userdata/roms/mame",
    "/opt/retropie/configs/mame",             # RetroPie
    "/recalbox/share/system/configs/mame",    # Recalbox
)


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "marquee")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #

def read_ini_version(path):
    """(file_version, mame_version) from a MAME Extras header, or (None, None)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                match = INI_VERSION_RE.search(line)
                if match:
                    return match.group(2), match.group(3)
    except OSError:
        pass
    return None, None


def read_xml_build(path):
    """MAME version from <mame build="...">, read without parsing the document."""
    try:
        with open(path, "rb") as handle:
            for _event, element in ET.iterparse(handle, events=("start",)):
                match = BUILD_RE.search(element.get("build") or "")
                return match.group(1) if match else None
    except (OSError, ET.ParseError):
        return None
    return None


def same_version(one, other):
    """0.289 and "0.289" and 0.2890 are one release; "0.9" and "0.289" are not.

    Compared as numbers because the version is written both ways in the wild, and as
    strings "0.9" sorts after "0.289" and is not a newer MAME.
    """
    if not one or not other:
        return False
    try:
        return float(one) == float(other)
    except (TypeError, ValueError):
        return str(one).strip() == str(other).strip()


def describe_version_match(xml_version, catlist_version):
    """Human-readable warning when the romset data and catlist disagree, else None."""
    if not xml_version or not catlist_version:
        return None
    if xml_version == catlist_version:
        return None
    return (f"Version mismatch: MAME XML is {xml_version} but catlist.ini is for MAME "
            f"{catlist_version}. Categories and filters will be wrong for machines that "
            f"changed between the two. Use matching MAME Extras files, or pass "
            f"--ignore-version-mismatch to continue anyway.")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _search_dirs(hint_dirs):
    seen, result = set(), []
    for directory in list(hint_dirs) + list(SUPPORT_DIRS):
        if not directory:
            continue
        expanded = os.path.abspath(os.path.expanduser(directory))
        if expanded not in seen and os.path.isdir(expanded):
            seen.add(expanded)
            result.append(expanded)
    return result


def _iglob_nocase(directory, pattern):
    """glob a directory case-insensitively, since Extras files ship in mixed case."""
    insensitive = "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in pattern)
    return sorted(glob.glob(os.path.join(glob.escape(directory), insensitive)))


def find_support_file(pattern, hint_dirs=(), wanted_version=None):
    """First file matching `pattern`, searching hint dirs then well-known locations.

    With `wanted_version`, a file for another MAME release is not a match. A stale
    catlist.ini lying in ~/.mame or beside the package is worse than none at all: the
    run stops on a version mismatch, and what it stops on is a file the user did not
    know was there. The nearest mismatched one is still reported, so the caller can
    say what it found and why it is not using it.
    """
    mismatched = None
    for directory in _search_dirs(hint_dirs):
        for candidate in _iglob_nocase(directory, pattern):
            if not os.path.isfile(candidate):
                continue
            if not wanted_version:
                return candidate
            _file_version, mame_version = read_ini_version(candidate)
            # No header at all: nothing says it is wrong, so it is still a candidate.
            if not mame_version or same_version(mame_version, wanted_version):
                return candidate
            mismatched = mismatched or (candidate, mame_version)
    if mismatched:
        raise WrongVersionOnDisk(*mismatched)
    return None


class WrongVersionOnDisk(Exception):
    """Found, and for the wrong release. Carries what was found so a caller can say so."""

    def __init__(self, path, version):
        super().__init__(f"{path} is for MAME {version}")
        self.path, self.version = path, version


# "mame0252.xml" encodes 0.252 with no separator, so BUILD_RE cannot read it.
FILENAME_VERSION_RE = re.compile(r'mame[_-]?(\d{2,4})', re.IGNORECASE)


def xml_version_key(path):
    """(is_real_listxml, version) for ranking candidate XMLs.

    A file whose root carries a build attribute is something MAME actually wrote, so
    it outranks one whose version was only guessed from its name.
    """
    build = read_xml_build(path)
    if build:
        try:
            return (1, float(build))
        except ValueError:
            pass

    match = FILENAME_VERSION_RE.search(os.path.basename(path))
    if match:
        digits = match.group(1)
        # "0252" is 0.252, not 0.0252.
        if len(digits) > 1 and digits.startswith("0"):
            digits = digits.lstrip("0") or "0"
        try:
            return (0, float(f"0.{digits}"))
        except ValueError:
            pass
    return (0, 0.0)


def _counts(directory, suffix, limit=400):
    """How many files with this suffix are directly inside, up to `limit`."""
    found = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.lower().endswith(suffix) and entry.is_file():
                    found += 1
                    if found >= limit:
                        break
    except OSError:
        return 0
    return found


def _holds_disks(directory, limit=40):
    """Whether this looks like a CHD set: subfolders with .chd files in them."""
    seen = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                seen += 1
                if _counts(entry.path, ".chd", limit=1):
                    return True
                if seen >= limit:
                    break
    except OSError:
        return False
    return False


def holds_disks(directory):
    """Whether a folder is a CHD set: subfolders with .chd files in them."""
    return bool(directory) and os.path.isdir(directory) and _holds_disks(directory)


FOLDER_VERSION_RE = re.compile(r"\bMAME\s+(0\.\d{2,3})\b", re.IGNORECASE)


def version_in_path(path):
    """The MAME release a folder name says it holds, or None.

    Pleasuredome names its sets "MAME 0.289 ROMs (non-merged)" and a download client
    saves each torrent under its own name, so on a fresh install the release is
    written on the source folder even when nothing else says it.
    """
    for part in reversed(str(path or "").replace("\\", "/").split("/")):
        match = FOLDER_VERSION_RE.search(part)
        if match:
            return match.group(1)
    return None


def locate_set(directory, kind="roms", version=None):
    """The folder that actually holds the set, one level down if that is where it is.

    A download client saves a torrent into a folder named after it, so the folder
    chosen in settings -- the one qBittorrent was pointed at -- is usually the parent
    of the set rather than the set. Descending to the subfolder that holds the files
    is what was meant, and is the difference between a working first run and a plan
    that reports all 44,166 machines missing.

    One level, on purpose. A download folder can hold a hand-made backup of the full
    set further down ("/downloads/mame/MAME 0.288 CHDs (merged)"), and a plan that
    quietly reads from it ties the library to something Marquee does not manage.

    With `version`, a folder named for that release ("MAME 0.289 ROMs (non-merged)")
    is preferred over one named for another, and either over the fullest folder: an
    upgrade's download folder holds the release being upgraded from as well as the
    one being upgraded to, and the fuller one is the old one.

    Returns the path unchanged when it already holds the set, or when no subfolder
    obviously does.
    """
    if not directory or not os.path.isdir(directory):
        return directory
    if kind == "roms":
        if _counts(directory, ".zip", limit=1):
            return directory
        measure = lambda path: _counts(path, ".zip")   # noqa: E731
    else:
        if _holds_disks(directory):
            return directory
        measure = lambda path: 1 if _holds_disks(path) else 0  # noqa: E731

    best, best_key = None, None
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                found = measure(entry.path)
                if not found:
                    continue
                named = version_in_path(entry.name)
                if not version or named is None:
                    rank = 1
                else:
                    rank = 2 if same_version(named, version) else 0
                key = (rank, found)
                if best_key is None or key > best_key:
                    best, best_key = entry.path, key
    except OSError:
        return directory
    return best or directory


def find_mame_xml(hint_dirs=(), wanted_version=None):
    """Newest-looking mame*.xml available, preferring the highest MAME version.

    With `wanted_version`, only an XML whose <mame build> is that release counts:
    a chosen release used to go straight to the download even when the very file
    was already on disk, which made --offline fail for no reason.
    """
    found = []
    for directory in _search_dirs(list(hint_dirs) + list(XML_EXTRA_DIRS)):
        for candidate in _iglob_nocase(directory, "mame*.xml"):
            if not os.path.isfile(candidate):
                continue
            if wanted_version and not same_version(read_xml_build(candidate),
                                                   wanted_version):
                continue
            found.append((xml_version_key(candidate), os.path.getmtime(candidate), candidate))
    if not found:
        return None
    found.sort()
    return found[-1][2]


def mame_binary_version(mame_binary="mame"):
    """Version reported by the mame executable on PATH, or None."""
    executable = shutil.which(mame_binary)
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-version"], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = BUILD_RE.search(result.stdout or result.stderr or "")
    return match.group(1) if match else None


def generate_listxml(mame_binary="mame", reporter=None):
    """Produce -listxml output from the installed MAME and cache it. Returns a path."""
    executable = shutil.which(mame_binary)
    if not executable:
        return None

    version = mame_binary_version(mame_binary) or "unknown"
    destination = os.path.join(cache_dir(), f"listxml-{version}.xml")
    if os.path.isfile(destination) and os.path.getsize(destination) > 0:
        return destination

    reporter = reporter or Reporter()
    reporter.info(f"Generating MAME XML from {executable} (this takes a minute)..")
    partial = destination + ".part"
    try:
        with open(partial, "wb") as out:
            subprocess.run([executable, "-listxml"], stdout=out, check=True, timeout=1800)
        os.replace(partial, destination)
    except (OSError, subprocess.SubprocessError) as error:
        reporter.warn(f"Could not run '{executable} -listxml': {error}")
        if os.path.exists(partial):
            os.remove(partial)
        return None
    return destination


# --------------------------------------------------------------------------- #
# Derived data
# --------------------------------------------------------------------------- #

CATLIST_SKIP_SECTIONS = ("FOLDER_SETTINGS", "ROOT_FOLDER")
MATURE_MARKER = "* Mature *"
# Sections read "Genre / Subcategory", but a genre can contain a slash of its own --
# "Game Console/Computer", "Videocassette Player/Recorder" -- so only the spaced form
# separates the two levels. Splitting on a bare "/" mis-derives those.
CATEGORY_SEPARATOR = " / "


def clean_category(section):
    """'Arcade: Maze / Misc.' -> 'Maze / Misc.'"""
    return section.removeprefix("Arcade:").strip()


def genre_of(section):
    """Leading component of a catlist section, which is exactly what genre.ini holds.

    Checked against MAME Extras 0.252 (48 sections, 45,379 machines) and 0.289 (59
    sections, 50,368 machines): both match exactly, so genre.ini carries no information
    catlist.ini does not already have.
    """
    body = clean_category(section).replace(MATURE_MARKER, "").strip()
    return body.split(CATEGORY_SEPARATOR)[0].strip()


def derive_genres(catlist):
    """{genre: {machine, ...}} built from a parsed catlist.ini."""
    genres = {}
    for section in catlist.sections():
        if section in CATLIST_SKIP_SECTIONS:
            continue
        bucket = genres.setdefault(genre_of(section), set())
        for machine_name, _ in catlist.items(section):
            bucket.add(machine_name)
    return genres


# --------------------------------------------------------------------------- #
# XML extraction and cache
# --------------------------------------------------------------------------- #

def _machine_record(machine):
    """Everything this tool needs from one <machine>, independent of user settings.

    Kept free of filter decisions so that changing blacklists or the mature setting
    does not invalidate the cache.
    """
    driver = machine.find("driver")
    disks = [disk for disk in machine.findall("disk") if disk.get("status") != "nodump"]
    features = machine.findall("feature")
    manufacturer = machine.find("manufacturer")
    description = machine.find("description")
    year = machine.find("year")
    display = machine.find("display")
    inputs = machine.find("input")
    controls = machine.findall("input/control")

    return {
        "n": machine.get("name"),
        "d": (description.text if description is not None else None) or "",
        "c": machine.get("cloneof"),
        "r": machine.get("romof"),
        "m": (manufacturer.text if manufacturer is not None else None) or "",
        "st": driver.get("status") if driver is not None else None,
        "em": driver.get("emulation") if driver is not None else None,
        "sv": driver.get("savestate") if driver is not None else None,
        # What MAME says is imperfect or missing about this machine, by area:
        # graphics, sound, protection, palette and so on. 6,038 of them in 0.289.
        # "status good" alone says a machine runs, not how well.
        # Lists, not tuples: this record is cached as JSON and has to come back the
        # same shape it went in as.
        "ft": sorted([feature.get("type") or "",
                      feature.get("status") or feature.get("overall") or "imperfect"]
                     for feature in features if feature.get("type")),
        "dr": driver is not None,
        "bios": machine.get("isbios") == "yes",
        "dev": machine.get("isdevice") == "yes",
        "mech": machine.get("ismechanical") == "yes",
        "run": machine.get("runnable") != "no",
        # No <display> is what screenless.ini records, so it needs no ini file.
        "scr": display is None,
        # Enough to describe the machine to somebody choosing between 12,000 of them:
        # a list that says only "galaga" is a list nobody can read.
        "y": (year.text if year is not None else "") or "",
        "dp": None if display is None else {
            "type": display.get("type") or "",
            "rotate": int(display.get("rotate") or 0),
            "width": int(display.get("width") or 0),
            "height": int(display.get("height") or 0),
            "refresh": round(float(display.get("refresh") or 0), 2),
        },
        "pl": int((inputs.get("players") if inputs is not None else 0) or 0),
        "ct": sorted({control.get("type") for control in controls
                      if control.get("type")}),
        "bt": max([int(control.get("buttons") or 0) for control in controls] or [0]),
        # Disk names matter, not just whether there are any: a clone's own CHD is
        # routinely stored inside the parent's folder, so the file has to be looked
        # for by name rather than by guessing a directory.
        "dk": [[disk.get("name"), disk.get("merge")] for disk in disks],
        # Everything needed to tell whether this machine's zip changed between two
        # MAME releases. Kept out of the cached record -- see `_sign`, which folds it
        # into one digest and then drops it, because the raw lists are ~15x bigger
        # than everything else in the cache put together.
        "_roms": sorted((rom.get("name") or "",
                         rom.get("sha1") or rom.get("crc") or "",
                         rom.get("size") or "")
                        for rom in machine.findall("rom")
                        if rom.get("status") != "nodump"),
        "_devs": sorted({ref.get("name") for ref in machine.findall("device_ref")
                         if ref.get("name")}),
    }


def extract_machines(xml_file, on_progress=None):
    """Stream every <machine> out of the XML.

    iterparse plus clearing the root keeps peak memory flat; ET.parse held the whole
    ~180 MB document as a tree.
    """
    total_size = os.path.getsize(xml_file)
    machines = []
    build = None

    with open(xml_file, "rb") as handle:
        context = ET.iterparse(handle, events=("start", "end"))
        _event, root = next(context)
        build = BUILD_RE.search(root.get("build") or "")
        build = build.group(1) if build else None

        for event, element in context:
            if event != "end" or element.tag != "machine":
                continue
            machines.append(_machine_record(element))
            # Clearing the element alone would still leak it as a child of root.
            root.clear()
            if on_progress and len(machines) % 2000 == 0:
                on_progress(handle.tell(), total_size, len(machines))

    if on_progress:
        on_progress(total_size, total_size, len(machines))
    _sign(machines)
    return {"cache_version": CACHE_VERSION, "build": build, "machines": machines}


def rom_manifests(xml_file, wanted=None, on_progress=None):
    """{machine: [(rom name, crc, shared with the parent)]} straight from the XML.

    Kept out of the parse cache on purpose -- the raw ROM lists are about fifteen times
    everything else in it put together, which is why `_sign` folds them into a digest
    and drops them. This reads them back for the machines that are actually being
    checked, which is a deliberate act and can afford one pass over the file.

    `merge` says the ROM is inherited from the parent. In a merged or split set it
    lives in the parent's zip instead, so its absence is not proof of anything.
    """
    total_size = os.path.getsize(xml_file)
    manifests = {}
    with open(xml_file, "rb") as handle:
        context = ET.iterparse(handle, events=("start", "end"))
        _event, root = next(context)
        for event, element in context:
            if event != "end" or element.tag != "machine":
                continue
            name = element.get("name")
            if wanted is None or name in wanted:
                roms = []
                for rom in element.findall("rom"):
                    if rom.get("status") == "nodump" or not rom.get("name"):
                        continue
                    roms.append((rom.get("name"), (rom.get("crc") or "").lower(),
                                 bool(rom.get("merge"))))
                if roms:
                    manifests[name] = roms
            root.clear()
            if on_progress and len(manifests) % 2000 == 0:
                on_progress(handle.tell(), total_size, len(manifests))
    if on_progress:
        on_progress(total_size, total_size, len(manifests))
    return manifests


def _sign(machines):
    """Give every machine a digest of the bytes its non-merged zip would contain.

    Pleasuredome's non-merged sets embed the BIOS and device ROMs inside each game's
    zip, so the zip changes when any of them changes -- not just the machine's own
    ROMs. The closure is walked here once, and only the digest is kept: comparing two
    releases then costs one string compare per machine instead of a set diff over
    350,000 ROM entries.

    Because every zip is TorrentZipped, identical content really is an identical file,
    which is what makes an upgrade a delta rather than a re-download.
    """
    index = {machine["n"]: machine for machine in machines}
    cache = {}

    def content(name, seen):
        if name in seen or name not in index:
            return ()
        seen.add(name)
        machine = index[name]
        parts = [tuple(rom) for rom in machine["_roms"]]
        # `romof` points at either a parent or a BIOS; only a BIOS contributes files
        # to a non-merged zip, because a parent's ROMs are already listed in full.
        parent = index.get(machine["r"])
        if parent is not None and parent["bios"]:
            parts.extend(content(machine["r"], seen))
        for device in machine["_devs"]:
            parts.extend(content(device, seen))
        return parts

    for machine in machines:
        name = machine["n"]
        if name not in cache:
            digest = hashlib.sha1(
                repr(sorted(set(content(name, set())))).encode()).hexdigest()
            cache[name] = digest[:16]
        machine["sig"] = cache[name]

    for machine in machines:
        del machine["_roms"]
        del machine["_devs"]


def cache_path_for(xml_file):
    stat = os.stat(xml_file)
    key = f"{os.path.abspath(xml_file)}|{stat.st_mtime_ns}|{stat.st_size}|{CACHE_VERSION}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir(), f"machines-{digest}.json")


CACHE_GLOB = "machines-*.json"


def prune_caches(source, keep):
    """Drop earlier parse caches of the same XML.

    The filename is a digest of the path, the file's timestamp *and* CACHE_VERSION, so
    every version bump writes a new one and the old one is never looked at again --
    5 MB apiece, once per bump, for ever. Nothing pruned them because nothing recorded
    which XML a cache file came from; now the payload says, and this sweeps the rest.
    """
    removed = 0
    for path in glob.glob(os.path.join(cache_dir(), CACHE_GLOB)):
        if os.path.abspath(path) == os.path.abspath(keep):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            continue
        # Written before this app recorded which XML a cache came from. One whose
        # version no longer matches can never be loaded again, so it is dead whatever
        # it was parsed from; one that does match may still be in use, so it stays.
        if "source" not in stored:
            if stored.get("cache_version") == CACHE_VERSION:
                continue
        elif stored["source"] != source:
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if data.get("cache_version") != CACHE_VERSION:
        return None
    return data


def save_cache(path, data, reporter=None):
    try:
        atomic.write_json(path, data, separators=(",", ":"))
    except OSError as error:
        (reporter or Reporter()).warn(f"Could not write parse cache: {error}")


def load_machines(xml_file, use_cache=True, refresh=False, on_progress=None, reporter=None):
    """Machine records for `xml_file`, from cache when the file is unchanged."""
    reporter = reporter or Reporter()
    cache_file = cache_path_for(xml_file) if use_cache else None

    if cache_file and not refresh:
        cached = load_cache(cache_file)
        if cached is not None:
            reporter.info(f"Reusing cached parse of {os.path.basename(xml_file)} "
                          f"({len(cached['machines'])} machines).")
            return cached

    reporter.stage("Parsing Mame XML..")
    data = extract_machines(xml_file, on_progress=on_progress)
    reporter.info(f"Parse completed.. {len(data['machines'])} machines read.")
    if cache_file:
        # Recorded so the sweep below knows which XML this cache belongs to.
        data["source"] = os.path.abspath(xml_file)
        save_cache(cache_file, data, reporter)
        dropped = prune_caches(data["source"], cache_file)
        if dropped:
            reporter.info(f"Removed {dropped} superseded parse cache(s).")
    return data
