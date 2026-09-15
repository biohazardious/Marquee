"""Turning a selection into a download.

The library's shape and the torrent's shape are different: the torrent is a flat
`MAME 0.289 ROMs (non-merged)/galaga.zip` per machine, and `<machine>/<disk>.chd`
for disks. This module maps the machines a profile wants onto the file indices
inside those torrents, works out what is already on disk, and reports what the
download will really cost.

Nothing here talks to a download client; it takes a file list and returns a plan, so
it can be tested without a torrent in sight.
"""
from dataclasses import dataclass, field

from .plan import human_bytes

ROM_SUFFIX = ".zip"
CHD_SUFFIX = ".chd"


@dataclass
class TorrentFile:
    """One entry in a torrent, as every download client reports them."""
    index: int
    path: str
    size: int
    piece_range: tuple = None
    progress: float = 0.0

    @property
    def basename(self):
        return self.path.rsplit("/", 1)[-1]


@dataclass
class Selection:
    """Which files of one torrent to fetch, and what that costs."""
    infohash: str = None
    name: str = ""
    indices: list = field(default_factory=list)
    # Machines that wanted a file this torrent does not have. Routinely non-empty:
    # the CHD set lags the ROM set by a release or two, so a brand new machine's
    # disk simply is not published yet.
    missing: list = field(default_factory=list)
    # Files already complete on disk, excluded from the download but still selected
    # in the client so it keeps seeding them.
    present: list = field(default_factory=list)
    selected_bytes: int = 0
    present_bytes: int = 0
    total_bytes: int = 0
    piece_bytes: int = 0
    # Pieces still to fetch, i.e. those touching a file that is not yet complete. A
    # piece shared by a finished and an unfinished file is not finished, so it counts.
    remaining_bytes: int = 0

    @property
    def download_bytes(self):
        """What actually goes over the wire.

        An estimate: a piece shared with a file nobody selected is charged in full,
        which is exactly what happens. Once the torrent is added the client reports
        the true figure and that is what the UI shows from then on.
        """
        return self.remaining_bytes

    @property
    def overhead_bytes(self):
        return max(self.piece_bytes - self.selected_bytes, 0)

    def describe(self):
        return (f"{len(self.indices):,} files, {human_bytes(self.download_bytes)} to "
                f"fetch of {human_bytes(self.total_bytes)} in the set")


def index_files(files):
    """[TorrentFile] from whatever the client handed back."""
    made = []
    for entry in files:
        if isinstance(entry, TorrentFile):
            made.append(entry)
            continue
        piece = entry.get("piece_range")
        made.append(TorrentFile(
            index=entry["index"],
            path=entry.get("path") or entry.get("name"),
            size=entry["size"],
            piece_range=tuple(piece) if piece else None,
            progress=entry.get("progress", 0.0)))
    return made


def piece_length(files, total_pieces=None):
    """Infer the torrent's piece length from the file table.

    Clients report each file's `piece_range` but not the piece size, and the size is
    what turns a count of pieces into bytes. Ask the client for it where you can
    (qBittorrent exposes it on `torrents/properties`) and pass it in; this is the
    fallback.

    Only the last piece of a torrent is short, so `total / pieces` is at most the
    piece length and at worst a shade under it. Piece lengths are always powers of
    two, so rounding *up* to the next one recovers it exactly.
    """
    spanning = [f for f in files if f.piece_range and f.size]
    if not spanning:
        return 0
    pieces = total_pieces or max(f.piece_range[1] for f in spanning) + 1
    if pieces <= 0:
        return 0
    average = sum(f.size for f in files) / pieces
    for exponent in range(10, 29):           # 1 KiB .. 256 MiB
        candidate = 1 << exponent
        if candidate >= average:
            return candidate
    return 1 << 28


def _cost(chosen, files, piece_size):
    """Bytes the chosen files really pull, counting each shared piece once."""
    if not piece_size or any(f.piece_range is None for f in chosen):
        return sum(f.size for f in chosen)
    pieces = set()
    for handle in chosen:
        first, last = handle.piece_range
        pieces.update(range(first, last + 1))
    return len(pieces) * piece_size


def rom_selection(files, machines, total_pieces=None, piece_size=None):
    """Files for `machines` inside a non-merged ROM torrent.

    A non-merged set is one self-contained zip per machine, so the mapping is the
    machine name and nothing more.
    """
    handles = index_files(files)
    by_name = {}
    for handle in handles:
        name = handle.basename
        if name.lower().endswith(ROM_SUFFIX):
            by_name[name[:-len(ROM_SUFFIX)]] = handle

    chosen, missing = [], []
    for machine in machines:
        handle = by_name.get(machine)
        (chosen if handle else missing).append(handle if handle else machine)

    return _finish(chosen, missing, handles, total_pieces, piece_size)


def chd_selection(files, disks, total_pieces=None, piece_size=None):
    """Files for `disks` inside a merged CHD torrent.

    `disks` is [(machine, parent, disk_name[, merge_name])]. In a merged set a
    clone's disk lives in the parent's folder, so the file has to be found by name
    with a fallback to the parent's directory. Looking only in the machine's own
    folder reports every clone as missing -- which it is not. And where the XML says
    the disk is merged under another name, it is that name in the parent's folder:
    eight machines in 0.289 have one, and they read as missing without it.
    """
    handles = index_files(files)
    by_path = {}
    for handle in handles:
        parts = handle.path.split("/")
        if len(parts) >= 2 and handle.path.lower().endswith(CHD_SUFFIX):
            by_path["/".join(parts[-2:])] = handle

    chosen, missing, seen = [], [], set()
    for machine, parent, disk, *rest in disks:
        merge = rest[0] if rest else None
        handle = None
        for owner, name in ((machine, disk), (parent, disk), (parent, merge)):
            if not owner or not name:
                continue
            handle = by_path.get(f"{owner}/{name}{CHD_SUFFIX}")
            if handle:
                break
        if handle is None:
            missing.append(f"{machine}/{disk}")
        elif handle.index not in seen:
            # Several clones share one merged disk; it is fetched once.
            seen.add(handle.index)
            chosen.append(handle)

    return _finish(chosen, missing, handles, total_pieces, piece_size)


def _finish(chosen, missing, handles, total_pieces, piece_size=None):
    if piece_size is None:
        piece_size = piece_length(handles, total_pieces)
    present = [f for f in chosen if f.progress >= 1.0]
    outstanding = [f for f in chosen if f.progress < 1.0]
    return Selection(
        indices=sorted(f.index for f in chosen),
        missing=sorted(missing),
        present=sorted(f.index for f in present),
        selected_bytes=sum(f.size for f in chosen),
        present_bytes=sum(f.size for f in present),
        total_bytes=sum(f.size for f in handles),
        piece_bytes=_cost(chosen, handles, piece_size),
        remaining_bytes=_cost(outstanding, handles, piece_size))


def disks_for(machines, records):
    """[(machine, parent, disk, merge)] for the machines that need a CHD.

    `records` is the parsed XML keyed by machine name.
    """
    wanted = []
    for machine in machines:
        record = records.get(machine)
        if not record:
            continue
        for disk, merge in record.get("dk") or ():
            wanted.append((machine, record.get("c"), disk, merge or None))
    return wanted


def delta(current, target, machines):
    """Which of `machines` need fetching to move a library from one release to another.

    `current` and `target` map machine name to content signature. A machine whose
    signature is unchanged is already the right bytes on disk -- TorrentZip makes
    that literal -- so it is skipped no matter how many releases were crossed.
    """
    changed, added, unchanged = [], [], []
    for machine in machines:
        if machine not in current:
            added.append(machine)
        elif current[machine] != target.get(machine):
            changed.append(machine)
        else:
            unchanged.append(machine)
    return {"changed": sorted(changed), "added": sorted(added),
            "unchanged": sorted(unchanged)}
