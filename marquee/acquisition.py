"""The acquisition pipeline: from a selection to bytes on disk.

`plan()` answers "what would this cost" without touching the download client, so a
UI can show the figure before anyone commits. `start()` then does it: add the
torrent stopped, select exactly the wanted files, and let it go.

The library itself is built by the existing copy pipeline from whatever lands in the
torrent folder -- this module stops at the download.
"""
import os
from dataclasses import dataclass, field

from . import acquire, sources
from .errors import ConfigError, MarqueeError
from .indexers.release import CHDS, ROMS
from .plan import human_bytes
from .reporting import Reporter

# Torrents this tool created, so it never touches anything else in the client.
CATEGORY = "marquee"


@dataclass
class AcquisitionPlan:
    """What a download would fetch, before anything is added to the client."""
    roms: acquire.Selection = None
    chds: acquire.Selection = None
    # Machines already at the right version on disk; the whole point of an upgrade.
    unchanged: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    added: list = field(default_factory=list)
    from_version: str = None
    to_version: str = None

    @property
    def parts(self):
        return [(ROMS, self.roms), (CHDS, self.chds)]

    @property
    def download_bytes(self):
        return sum(part.download_bytes for _kind, part in self.parts if part)

    @property
    def library_bytes(self):
        """What the finished library weighs, whether or not it has to be fetched."""
        return sum(part.selected_bytes for _kind, part in self.parts if part)

    @property
    def set_bytes(self):
        return sum(part.total_bytes for _kind, part in self.parts if part)

    @property
    def missing(self):
        out = []
        for kind, part in self.parts:
            out.extend(f"{kind}:{name}" for name in (part.missing if part else ()))
        return out

    @property
    def is_upgrade(self):
        return bool(self.from_version) and self.from_version != self.to_version

    def describe(self):
        saved = (1 - self.download_bytes / self.set_bytes) * 100 if self.set_bytes else 0
        head = (f"{human_bytes(self.download_bytes)} to download"
                f" of {human_bytes(self.set_bytes)} published ({saved:.0f}% less)")
        if self.is_upgrade:
            head += (f"; {len(self.unchanged):,} machines already correct, "
                     f"{len(self.changed):,} changed, {len(self.added):,} new")
        return head


def signatures(xml_path, reporter=None):
    """{machine: content digest} for one release."""
    data = sources.load_machines(xml_path, reporter=reporter)
    return {machine["n"]: machine["sig"] for machine in data["machines"]}


def records(xml_path, reporter=None):
    data = sources.load_machines(xml_path, reporter=reporter)
    return {machine["n"]: machine for machine in data["machines"]}


def plan(machines, target_xml, rom_files, chd_files=None, current_signatures=None,
         from_version=None, to_version=None, rom_piece=None, chd_piece=None,
         reporter=None):
    """Cost a download without touching the client.

    `machines` is the profile's selection at the target release. `current_signatures`
    is what the library already holds, keyed by machine; passing it turns a fresh
    install into an upgrade and drops every machine whose bytes have not moved.
    """
    reporter = reporter or Reporter()
    known = records(target_xml, reporter)
    target = {name: record["sig"] for name, record in known.items()}

    if current_signatures:
        split = acquire.delta(current_signatures, target, machines)
        fetch_list = split["changed"] + split["added"]
    else:
        split = {"changed": [], "added": sorted(machines), "unchanged": []}
        fetch_list = sorted(machines)

    roms = acquire.rom_selection(rom_files, fetch_list, piece_size=rom_piece)

    chds = None
    if chd_files is not None:
        # A CHD is fetched whenever the machine is wanted and the disk is not already
        # there: disks are not rebuilt between releases the way zips are, so the
        # signature diff says nothing useful about them.
        disks = acquire.disks_for(sorted(machines), known)
        chds = acquire.chd_selection(chd_files, disks, piece_size=chd_piece)

    return AcquisitionPlan(roms=roms, chds=chds, unchanged=split["unchanged"],
                           changed=split["changed"], added=split["added"],
                           from_version=from_version, to_version=to_version)


def start(client, release, selection, save_path, reporter=None, category=CATEGORY,
          on_wait=None):
    """Add one release to the download client and select exactly `selection.indices`.

    Added stopped, selected, then started -- in that order, because a torrent that
    starts before the selection lands begins on all 163 GB of it.
    """
    reporter = reporter or Reporter()
    if not selection.indices:
        raise MarqueeError(f"Nothing to fetch from {release.name}.")

    client.ensure_category(category, save_path)
    infohash = client.add(release.magnet_uri(), save_path=save_path,
                          category=category, tags=[release.kind, release.version],
                          stopped=True)
    reporter.stage(f"Waiting for {release.name} metadata...")
    client.wait_for_metadata(infohash, on_wait=on_wait)

    # narrow, not select_only: this call added the torrent, so nothing in it can be
    # complete yet and the two do the same thing -- but if `add` handed back a
    # torrent that was already there, select_only would have stopped it seeding
    # every file it did not list.
    result = client.narrow(infohash, selection.indices)
    reporter.info(f"{release.name}: {result['selected']:,} of "
                  f"{result['selected'] + result['skipped']:,} files selected.")
    client.start(infohash)
    return infohash


def resume(client, infohash, indices, reporter=None):
    """Re-apply a selection to a torrent the client already has.

    Adding a genre back, or moving to a new profile, changes which files are wanted
    without changing the torrent. Re-selecting is far cheaper than re-adding.
    """
    reporter = reporter or Reporter()
    result = client.narrow(infohash, indices)
    client.start(infohash)
    reporter.info(f"Re-selected {result['selected']:,} files.")
    return result


def torrent_root(client, infohash, mappings=None):
    """Where this tool can read the torrent's files.

    qBittorrent reports paths as *it* sees them. When it runs in another container or
    on another host, that path means nothing here, so it is rewritten. Without this
    the import step silently finds an empty folder.
    """
    entry = client.one(infohash)
    if entry is None:
        raise MarqueeError(f"The download client has no torrent {infohash[:12]}.")
    return remap(entry["content_path"] or entry["save_path"], mappings)


def remap(path, mappings=None):
    for remote, local in (mappings or ()):
        remote = remote.rstrip("/")
        if path == remote or path.startswith(remote + "/"):
            tail = path[len(remote):].strip("/")
            # os.path.join(local, "") leaves a trailing separator, which then fails
            # every later equality test against the same directory.
            return os.path.join(local, tail) if tail else local
    return path


def parse_mappings(text):
    """"remote -> local" per line, as the settings file and the UI both store them."""
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for separator in ("->", "=>", "|"):
            if separator in line:
                remote, local = line.split(separator, 1)
                pairs.append((remote.strip(), local.strip()))
                break
        else:
            raise ConfigError(f"Remote path mapping needs 'remote -> local': {line!r}")
    return pairs
