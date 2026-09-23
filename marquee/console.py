"""Which games the console's own MAME can actually run from this set.

A library is built from the newest set Pleasuredome publishes, and the console runs
whatever MAME its distribution ships -- Batocera 43 has 0.285 under a 0.289 library.
Most zips are the same bytes either way, but not all: on that library 714 of 10,022
games are machines 0.285 does not have, and 67 are ones whose 0.289 zip lacks a ROM
0.285 still asks for (a chip since redumped). Neither starts. Older full sets are not
to be had, so the honest thing is to file those games apart rather than list them
among the ones that work.

MAME finds a ROM in a zip by CRC, so a zip serves an older release when every ROM
that release names for the machine is in it by CRC -- extra files and new labels do
not matter. Disks are held to their SHA-1 the same way. Both are read from the two
releases' XML; nothing in the library is opened.
"""
import json
import os
import xml.etree.ElementTree as ET

from . import atomic, sources

# Not in the console's MAME at all: a machine added since.
NEWER = "newer"
# In it, but the set's zip does not hold every ROM (or disk) it wants.
MISSING = "missing"

DEFAULT_FOLDERS = {NEWER: "ZZ-Version-Mismatch", MISSING: "ZZ-Missing-ROM"}


def media(xml_file):
    """{machine: [{crc: rom name}, {sha1: disk name}, [devices]]} for one release, cached.

    Only what can be checked: a ROM or disk with no dump has nothing to compare.
    Devices are named so their ROMs can be counted too: a non-merged zip carries
    them (galaga.zip holds namco51's 51xx.bin), and a device ROM redumped between
    two releases stops the game as surely as one of its own.
    """
    stat = os.stat(xml_file)
    cache = os.path.join(sources.cache_dir(), "console",
                         f"v2-{os.path.basename(xml_file)}-{stat.st_size}-{int(stat.st_mtime)}.json")
    try:
        with open(cache, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        pass
    found = {}
    for _event, element in ET.iterparse(xml_file):
        if element.tag != "machine":
            continue
        roms = {rom.get("crc").lower(): rom.get("name") for rom in element.iter("rom")
                if rom.get("crc") and rom.get("status") != "nodump"}
        disks = {disk.get("sha1").lower(): disk.get("name") for disk in element.iter("disk")
                 if disk.get("sha1") and disk.get("status") != "nodump"}
        devices = sorted({ref.get("name") for ref in element.iter("device_ref")
                          if ref.get("name")})
        found[element.get("name")] = [roms, disks, devices]
        element.clear()
    try:
        atomic.write_json(cache, found)
    except OSError:
        pass
    return found


def classify(set_media, console_media, names):
    """{machine: (NEWER or MISSING, [what the console wants and the set lacks])}.

    Only the machines the console cannot run are in the answer.
    """
    out = {}
    for name in names:
        wants = console_media.get(name)
        if wants is None:
            out[name] = (NEWER, [])
            continue
        has = set_media.get(name) or [{}, {}, []]
        want_roms = _with_devices(wants, console_media)
        has_roms = _with_devices(has, set_media)
        lacking = sorted([rom for crc, rom in want_roms.items() if crc not in has_roms]
                         + [f"{disk}.chd" for sha1, disk in wants[1].items()
                            if sha1 not in has[1]])
        if lacking:
            out[name] = (MISSING, lacking)
    return out


def _with_devices(entry, release):
    """A machine's ROMs by CRC, with the ROMs of every device it names."""
    roms = dict(entry[0])
    for device in (entry[2] if len(entry) > 2 else ()):
        roms.update((release.get(device) or [{}])[0])
    return roms


def folder_for(kind, config):
    """The folder a kind of mismatch is filed under, as the settings name it."""
    if kind == NEWER:
        return (config.version_mismatch_folder or "").strip() or DEFAULT_FOLDERS[NEWER]
    return (config.missing_rom_folder or "").strip() or DEFAULT_FOLDERS[MISSING]
