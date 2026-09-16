"""Working out what a run would copy, before any of it is copied.

Separating the plan from the copying is what makes --dry-run, the missing-file report,
the free-space check and the closing summary all read from the same decision rather
than each re-deriving it.
"""
import csv
import json
import os
import shutil
from dataclasses import dataclass, field

from . import backends, verify


@dataclass
class PlannedItem:
    name: str
    description: str
    category: str
    folder: str
    rom_source: str = None
    rom_bytes: int = 0
    chd_name: str = None
    # Individual .chd files rather than a directory: a source folder can hold disks
    # belonging to several clones, and copying it whole drags all of them along.
    chd_sources: list = field(default_factory=list)
    chd_sizes: list = field(default_factory=list)
    chd_bytes: int = 0
    genre: str = ""
    missing_disks: list = field(default_factory=list)
    # Every disk this machine needs, present or not. `missing_disks` is what has to be
    # fetched; this is what it needs to run, and a machine with no files at all still
    # has to be able to say so.
    disks: list = field(default_factory=list)

    # Descriptive, not operational: none of this changes what gets copied, it is what
    # lets the UI present a machine as a game rather than as an eight-letter filename.
    year: str = ""
    manufacturer: str = ""
    display: dict = None
    players: int = 0
    controls: list = field(default_factory=list)
    buttons: int = 0
    cloneof: str = None
    signature: str = ""
    # How well MAME says it runs. Everything planned has passed the working filter, so
    # `driver_status` is "good" or "imperfect"; `features` is what is imperfect about
    # it -- [["sound", "imperfect"], ["graphics", "unemulated"]].
    driver_status: str = ""
    emulation: str = ""
    savestate: str = ""
    features: list = field(default_factory=list)
    # Filed under the adult folder. Kept on the item so the library can be filtered by
    # it without re-deriving the rule from the category string.
    mature: bool = False
    # Why this machine is not in the library. Only set on the left-out catalogue.
    reason: str = ""
    # Its files are already at the destination, even though the source folder has none
    # of them. Filled in by sync.compare; see wanted_paths.
    in_library: bool = False
    # What checking the file against the release actually found. Empty until something
    # has looked; see marquee.verify.
    state: str = ""
    state_detail: list = field(default_factory=list)
    # Left out by a genre, category or name on the exclude list. Not wanted, but
    # still listable: the selection tree has to be able to show what is inside a
    # category before anyone can decide to put it back.
    excluded: bool = False
    # A file with this name is in the source folder but is not a finished file: a
    # torrent client's pre-allocated placeholder, or a download still in progress.
    # Counted as not downloaded, and said to be downloading rather than missing.
    partial: bool = False

    @property
    def is_clone(self):
        return bool(self.cloneof)

    @property
    def flawless(self):
        """Nothing MAME knows about is imperfect or missing."""
        return self.driver_status == "good" and not self.features

    def condition(self):
        """The caveats, shortest first: ["imperfect sound", "unemulated lan"]."""
        return [f"{status} {area}" for area, status in self.features]

    @property
    def is_vertical(self):
        return bool(self.display) and self.display.get("rotate") in (90, 270)

    @property
    def resolution(self):
        if not self.display or not self.display.get("width"):
            return ""
        return f"{self.display['width']}x{self.display['height']}"

    @property
    def total_bytes(self):
        return self.rom_bytes + self.chd_bytes

    def wanted_paths(self):
        """Where this machine's files belong, whether or not they are here yet.

        `files()` can only speak for what is in the source folder. This is what the
        library should hold, which is what lets an existing library be recognised
        rather than mistaken for thousands of files nobody wants.
        """
        yield f"{self.folder}/{self.name}.zip"
        holder = self.chd_name or self.name
        for disk in self.disks:
            yield f"{self.folder}/{holder}/{disk}.chd"

    def files(self):
        """(source path, destination path relative to the copy root, size) per file.

        Naming the destination explicitly is what lets a run be diffed against what is
        already there instead of blindly re-copying.
        """
        if self.rom_source:
            yield self.rom_source, f"{self.folder}/{self.name}.zip", self.rom_bytes
        for source, size in zip(self.chd_sources, self.chd_sizes):
            yield (source, f"{self.folder}/{self.chd_name}/{os.path.basename(source)}",
                   size)


@dataclass
class CopyPlan:
    items: list = field(default_factory=list)
    # Wanted, and not a single file of it is here. These are full records rather than
    # names because the library page lists them alongside what is on disk: on a fresh
    # install they are the entire catalogue, and nothing can be chosen from a list of
    # eight-letter names.
    absent_items: list = field(default_factory=list)
    # Machines the exclude list leaves out. They count towards a category's `wanted`
    # and used to be kept nowhere at all, so the tree said "Board Game / Cards: 1" and
    # opening it found nothing.
    excluded_items: list = field(default_factory=list)
    # Machines whose zip is in the source folder as a placeholder or a part-download.
    partial_roms: list = field(default_factory=list)
    missing_roms: list = field(default_factory=list)
    missing_chds: list = field(default_factory=list)
    mature_filtered: list = field(default_factory=list)
    # Every genre and category with what it weighs, excluded ones included, so a caller
    # can show what leaving one in or out actually costs.
    genres: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    # Filled in once the destination has been looked at; see marquee.sync.
    sync: object = None
    # The machines that did not make it, as a catalogue of their own. See
    # pipeline._left_out; None when nothing was dropped.
    left_out: object = None
    # {state: count} from the last check of the library against the release, or None
    # when nothing has looked. See marquee.verify.
    checked: dict = None

    @property
    def absent(self):
        """The machines with no ROM here, described.

        The same machines as `missing_roms`. A bare list of 1,500 names is not
        something anyone can act on; knowing what they are and which genre they belong
        to turns it into a shopping list.
        """
        return [{"name": item.name, "description": item.description,
                 "genre": item.genre, "category": item.category,
                 "chd": bool(item.disks)}
                for item in sorted(self.wanted, key=lambda entry: entry.name)
                if not item.rom_source]

    @property
    def needed(self):
        """What is actually worth downloading.

        Not `missing_roms`, which only knows about the source folder: a machine already
        sitting correctly in the library needs nothing, however empty the torrent
        folder happens to be.
        """
        out = []
        for item in self.wanted:
            if item.rom_source:
                continue                       # the source folder already has it
            if item.in_library and item.state not in (verify.STALE, verify.DAMAGED):
                continue                       # the library has it, and nothing says it is wrong
            out.append(item.name)
        return sorted(out)

    @property
    def library_items(self):
        """What the destination holds after a run: copied now, or already there.

        The gamelist and the manifest used to be built from `items` alone, so a run
        against a partial source folder rewrote gamelist.xml down to the games that
        happened to be in the torrent folder and struck every other game off the
        console's list.
        """
        return self.items + [item for item in self.absent_items if item.in_library]

    @property
    def wanted(self):
        """Everything the selection asks for, here or not, in one list.

        `items` is what can be copied now and `absent_items` what cannot; the library
        page shows a catalogue, which is both.
        """
        return self.items + self.absent_items

    @property
    def total_bytes(self):
        return sum(size for _source, _relpath, size in self.files())

    @property
    def file_count(self):
        return sum(1 for _ in self.files())

    def files(self):
        """Every distinct destination file, once.

        A merged clone and its parent name the same disk on purpose, so without this
        both the file count and the total size counted it twice.
        """
        seen = set()
        for item in self.items:
            for source, relpath, size in item.files():
                if relpath in seen:
                    continue
                seen.add(relpath)
                yield source, relpath, size


def _file_size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def resolve_disks(machine, parent, disks, chd_dir):
    """Locate each named .chd file, returning (found_paths, missing_disk_names).

    A disk is looked for under the machine's own folder and then under the parent's,
    because CHD collections routinely keep a clone's unique disk alongside the
    parent's -- MAME searches the parent set too, so there is no reason for them to
    split it out. Searching for a *folder* named after the machine, as this used to,
    reported every such clone as missing.
    """
    found, missing = [], []
    for disk in disks:
        filename = disk + ".chd"
        for holder in (machine, parent):
            if not holder:
                continue
            path = os.path.join(chd_dir, holder, filename)
            if os.path.isfile(path):
                found.append(path)
                break
        else:
            missing.append(disk)
    return found, missing


def _leaf_label(category):
    """The part of a category that names it, without the genre or the adult marker.

    "Tabletop / Mahjong * Mature *" -> "Mahjong", which is what the folder is called.
    """
    from . import sources
    label = category.replace(sources.MATURE_MARKER, "").strip()
    head, separator, tail = label.partition(" / ")
    return (tail if separator else head).strip() or label


ZIP_END_RECORD = b"PK\x05\x06"
CHD_MAGIC = b"MComprHD"


def looks_complete(path):
    """Whether a file in the source folder is a finished file, not a placeholder.

    A zip ends with its central directory and an end-of-archive record; a torrent
    client's pre-allocated file, or one still arriving, ends in zeros. The record is
    in the last 22 bytes of a plain zip and a little further in from a TorrentZip'd
    one (which carries a comment), so the last 128 bytes are enough. A CHD is
    recognised by its header; a placeholder has none. One small read per file:
    fourteen thousand of them take well under a second.
    """
    lower = path.lower()
    try:
        with open(path, "rb") as handle:
            if lower.endswith(".zip"):
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 128))
                return ZIP_END_RECORD in handle.read()
            if lower.endswith(".chd"):
                return handle.read(len(CHD_MAGIC)) == CHD_MAGIC
    except OSError:
        return False
    return True


def build(mame_list, rom_dir, chd_dir, folder_for, allow_mature):
    """Decide what would be copied. `folder_for` maps a category to (folder, is_mature).

    Machines excluded by genre are still measured, so the genre breakdown reflects the
    whole romset rather than only the part currently selected.
    """
    plan = CopyPlan()
    seen_missing_chds = set()
    genres = {}
    categories = {}
    # A merged clone names its parent's disk on purpose; counting it under the genre
    # twice would inflate exactly the number the caller is trying to decide on.
    counted = set()

    for name, info in mame_list.items():
        folder, is_mature = folder_for(info["category"])
        item = PlannedItem(name=name, description=info.get("description", ""),
                           category=info["category"], folder=folder,
                           genre=info.get("genre", ""),
                           year=info.get("year", ""),
                           manufacturer=info.get("manufacturer", ""),
                           display=info.get("display"),
                           players=info.get("players", 0),
                           controls=info.get("controls") or [],
                           buttons=info.get("buttons", 0),
                           cloneof=info.get("cloneof"),
                           signature=info.get("signature", ""),
                           driver_status=info.get("driver_status", ""),
                           emulation=info.get("emulation", ""),
                           savestate=info.get("savestate", ""),
                           features=info.get("features") or [],
                           mature=is_mature)

        rom_path = os.path.join(rom_dir, name + ".zip")
        try:
            size = os.stat(rom_path).st_size
        except OSError:
            size = None
        if size is not None:
            # Being there is not being finished. qBittorrent allocates every selected
            # file at its full size before a byte arrives, so a folder can hold
            # thousands of zips that are nothing but zeros -- 5,442 of 14,279 on the
            # set this was found on -- and a size check calls every one downloaded.
            if looks_complete(rom_path):
                item.rom_bytes = size
                item.rom_source = rom_path
            else:
                item.partial = True

        if info.get("chd_req"):
            item.chd_name = info.get("chd_folder")
            item.disks = list(info.get("chd_disks") or [])
            found, missing = resolve_disks(name, info.get("parent"),
                                           item.disks, chd_dir)
            finished = [path for path in found if looks_complete(path)]
            if len(finished) < len(found):
                item.partial = True
                missing = missing + [os.path.splitext(os.path.basename(path))[0]
                                     for path in found if path not in finished]
            item.chd_sources = finished
            item.chd_sizes = [_file_size(path) for path in finished]
            item.chd_bytes = sum(item.chd_sizes)
            item.missing_disks = missing

        if is_mature and not allow_mature:
            # Not weighed against its genre either: no genre choice would bring it back.
            plan.mature_filtered.append(name)
            continue

        here = bool(item.rom_source or item.chd_sources)

        # Every genre and category the selection touches gets an entry, whether or not
        # a byte of it is on disk: on a fresh install nothing is, and a selection tree
        # with no rows in it is one nobody can choose from. `machines` and `bytes` stay
        # what the run could copy today; `wanted` is what the selection asks for.
        bucket = genres.setdefault(item.genre, {"name": item.genre, "machines": 0,
                                                "bytes": 0, "wanted": 0,
                                                "excluded": False})
        leaf = categories.setdefault(item.category,
                                     {"name": item.category, "genre": item.genre,
                                      # catlist marks adult categories in the category
                                      # itself, so "Tabletop / Mahjong * Mature *" is a
                                      # different category from "Tabletop / Mahjong" --
                                      # already split, and filed under its own folder.
                                      # `label` is what to call it on screen, which is
                                      # what the folder on disk is called.
                                      "mature": is_mature,
                                      "label": _leaf_label(item.category),
                                      "machines": 0, "bytes": 0, "wanted": 0,
                                      "excluded": None})
        # A category is excluded when every game that could be in it is. Alternate
        # versions set aside by parents_only carry `excluded` too, but they say nothing
        # about the category -- and clones sort after their parents, so the last word
        # used to be theirs and most categories read as excluded.
        if not info.get("clone_of_kept"):
            verdict = bool(info.get("excluded"))
            leaf["excluded"] = (verdict if leaf["excluded"] is None
                                else leaf["excluded"] and verdict)
        bucket["wanted"] += 1
        leaf["wanted"] += 1
        if here:
            bucket["machines"] += 1
            leaf["machines"] += 1
            for _source, relpath, size in item.files():
                if relpath in counted:
                    continue
                counted.add(relpath)
                bucket["bytes"] += size
                leaf["bytes"] += size

        if info.get("excluded"):
            item.excluded = True
            plan.excluded_items.append(item)
            continue

        if item.partial:
            plan.partial_roms.append(name)
        if item.rom_source is None:
            plan.missing_roms.append(name)
        for disk in item.missing_disks:
            if disk not in seen_missing_chds:
                seen_missing_chds.add(disk)
                plan.missing_chds.append(disk)

        (plan.items if here else plan.absent_items).append(item)

    plan.missing_roms.sort()
    plan.missing_chds.sort()
    plan.partial_roms.sort()
    plan.absent_items.sort(key=lambda item: item.name)
    plan.excluded_items.sort(key=lambda item: item.name)
    # Weight first, because that is what a size decision is made on -- but on a set
    # nothing has been downloaded for yet every weight is zero, so the count breaks
    # the tie and the name breaks that.
    order = lambda entry: (-entry["bytes"], -entry["wanted"], entry["name"])  # noqa: E731
    for leaf in categories.values():
        leaf["excluded"] = bool(leaf["excluded"])
    plan.genres = sorted(genres.values(), key=order)
    plan.categories = sorted(categories.values(), key=order)
    # A genre counts as excluded only when nothing inside it survives.
    included = {entry["genre"] for entry in plan.categories if not entry["excluded"]}
    for entry in plan.genres:
        entry["excluded"] = entry["name"] not in included
    return plan


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #

def bytes_already_present(plan, copy_path):
    """How much of the plan is already at the destination at the right size."""
    present = 0
    listings = {}

    def listing(directory):
        if directory not in listings:
            entry_sizes = {}
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_file():
                            entry_sizes[entry.name] = entry.stat().st_size
            except OSError:
                pass
            listings[directory] = entry_sizes
        return listings[directory]

    for _source, relpath, size in plan.files():
        target = os.path.join(copy_path, relpath.replace("/", os.sep))
        if listing(os.path.dirname(target)).get(os.path.basename(target)) == size:
            present += size
    return present


def check_free_space(plan, copy_path):
    """(needed, free) in bytes for a local destination, or None when unknowable."""
    if backends.is_remote(copy_path):
        return None

    probe = copy_path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe.rstrip(os.sep))
        if parent == probe:
            return None
        probe = parent
    if not probe:
        return None

    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None

    # The sync report already knows precisely what has to move; fall back to a
    # size comparison when the destination has not been indexed.
    if plan.sync is not None:
        needed = plan.sync.to_transfer
    else:
        needed = plan.total_bytes - bytes_already_present(plan, copy_path)
    return max(needed, 0), free


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def human_bytes(count):
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def write_report(plan, path):
    """Write the plan to .json or .csv, chosen by the file's extension."""
    if path.lower().endswith(".csv"):
        _write_csv(plan, path)
    else:
        _write_json(plan, path)


def _write_json(plan, path):
    payload = {
        "summary": {
            "machines": len(plan.items),
            "files": plan.file_count,
            "bytes": plan.total_bytes,
            "missing_roms": len(plan.missing_roms),
            "missing_chds": len(plan.missing_chds),
            "mature_filtered": len(plan.mature_filtered),
        },
        "missing_roms": plan.missing_roms,
        "missing_chds": plan.missing_chds,
        "mature_filtered": sorted(plan.mature_filtered),
        "items": [
            {"name": item.name, "description": item.description, "category": item.category,
             "folder": item.folder, "rom": item.rom_source, "rom_bytes": item.rom_bytes,
             "chd": item.chd_name, "chd_sources": item.chd_sources,
             "chd_bytes": item.chd_bytes}
            for item in plan.items
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _write_csv(plan, path):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["status", "machine", "description", "category", "folder", "bytes"])
        for item in plan.items:
            writer.writerow(["copy", item.name, item.description, item.category,
                             item.folder, item.total_bytes])
        for name in plan.missing_roms:
            writer.writerow(["missing-rom", name, "", "", "", 0])
        for name in plan.missing_chds:
            writer.writerow(["missing-chd", name, "", "", "", 0])
        for name in sorted(plan.mature_filtered):
            writer.writerow(["mature-filtered", name, "", "", "", 0])
