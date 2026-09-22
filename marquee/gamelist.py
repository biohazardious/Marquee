"""gamelist.xml, the file EmulationStation reads.

Without one, a Batocera box shows a folder of `mslug.zip` as "mslug". With one it
shows *Metal Slug - Super Vehicle-001*, 1996, Nazca, 2 players, Platform, next to its
title screen.

Every field is already in hand -- MAME's own description, year and manufacturer, the
catlist genre, the `<input>` player count -- and the images are whatever the artwork
cache holds. Nothing is fetched and nothing is guessed.

**One file, at the root of the library**, with paths that include the genre folders.
That is how EmulationStation reads a system, and it is also the only arrangement that
cleans up after itself: a gamelist dropped into every genre folder would keep those
folders alive after the last game moved out of them, because the folder is no longer
empty and nothing may prune it.
"""
import os
import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import art, sources
from .errors import MarqueeError
from .reporting import Reporter

NAME = "gamelist.xml"
# Where the pictures go, relative to the library root.
IMAGE_DIR = "images"
# What this module writes into an entry, and so what a rewrite may replace. Everything
# else -- <favorite>, <playcount>, <lastplayed>, <hidden>, a scraper's <rating> or
# <video> -- was put there by EmulationStation or the user and is kept.
OWNED = ("path", "name", "desc", "releasedate", "developer", "publisher", "genre",
         "players")
# Written when there is something to say, but never taken away: a picture a scraper
# placed is better than none, and an adult flag somebody set by hand stays set.
OWNED_WHEN_SET = ("image", "adult")


def _text(parent, tag, value):
    if value in (None, "", 0):
        return
    ET.SubElement(parent, tag).text = str(value)


def _release_date(year):
    """ES wants a full timestamp; MAME gives a year, sometimes an approximate one."""
    digits = "".join(char for char in str(year or "") if char.isdigit())[:4]
    if len(digits) != 4:
        return None
    return f"{digits}0101T000000"


def entry(item, image=None):
    """One <game> element for a planned machine."""
    game = ET.Element("game")
    _text(game, "path", f"./{item.folder}/{item.name}.zip")
    _text(game, "name", item.description)
    _text(game, "desc", _describe(item))
    _text(game, "releasedate", _release_date(item.year))
    _text(game, "developer", item.manufacturer)
    _text(game, "publisher", item.manufacturer)
    _text(game, "genre", _genre(item))
    _text(game, "players", item.players)
    if image:
        _text(game, "image", image)
    if item.mature:
        _text(game, "adult", "true")
    return game


def _genre(item):
    """The category as a person would read it.

    catlist marks adult titles by appending a literal " * Mature * " to the section
    name. That is syntax, not a genre, and the <adult> flag already says it.
    """
    label = (item.category or "").replace(sources.MATURE_MARKER, "")
    label = label.replace("  ", " ").strip(" /").strip()
    # A category that was nothing but the marker leaves the genre to speak for it.
    return label or (item.genre or "")


def _describe(item):
    """A sentence about the machine, from what the XML already says about it."""
    parts = []
    if item.year and item.manufacturer:
        parts.append(f"{item.manufacturer}, {item.year}.")
    elif item.manufacturer:
        parts.append(f"{item.manufacturer}.")
    category = _genre(item)
    if category:
        parts.append(f"{category}.")
    if item.players:
        parts.append(f"{item.players} player{'s' if item.players > 1 else ''}.")
    if item.display:
        screen = item.display.get("type", "")
        if item.is_vertical:
            screen = f"vertical {screen}".strip()
        if screen:
            parts.append(f"{screen.capitalize()} display.")
    if item.chd_sources:
        parts.append("Needs a CHD.")
    if item.is_clone:
        parts.append(f"A version of {item.cloneof}.")
    return " ".join(parts)


def document(items, images=None):
    """The whole library as one gameList."""
    root = ET.Element("gameList")
    for item in sorted(items, key=lambda entry: entry.description.lower()):
        root.append(entry(item, (images or {}).get(item.name)))
    return root


def render(root):
    ET.indent(root, space="  ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode") + "\n")


def write(items, backend, kind=art.DEFAULT_KIND, copy_images=True, reporter=None,
          on_progress=None):
    """Write the library's gamelist for `items` and place their artwork.

    `items` is what the destination holds -- `plan.library_items`, not `plan.items`,
    or a partial source folder strikes every already-present game off the list.
    `backend` is the same copy backend the transfer used, so this works to a local
    folder, an SMB share, FTP or SFTP without knowing which.
    """
    reporter = reporter or Reporter()
    items = list(getattr(items, "library_items", items))
    # Read first: a list that cannot be read is left alone rather than replaced, and
    # nothing is worth placing pictures for until that is known.
    existing = _existing(backend)
    images, pictures = {}, 0
    if copy_images:
        images, pictures = _place_images(items, backend, kind, on_progress)

    fresh = document(items, images)
    body = render(merge(existing, fresh) if existing is not None else fresh)
    backend.write_text(NAME, body)

    reporter.info(f"Wrote {NAME} for {len(items)} game(s)"
                  + (f" and placed {pictures} picture(s)." if copy_images else "."))
    return {"games": len(items), "images": pictures,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _existing(backend):
    """The gamelist already in the library, parsed, or None when there is none.

    EmulationStation writes favourites, play counts and hidden flags into this same
    file. Rebuilding it from scratch wiped them on every transfer, so a list that is
    there but cannot be read stops the write instead of being overwritten.
    """
    body = backend.read_file(NAME)
    if body is None or not body.strip():
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError as error:
        raise MarqueeError(
            f"{NAME} in the library could not be read ({error}); it was left as it "
            f"is, since it holds EmulationStation's favourites and play counts. "
            f"Move it aside to have a new one written.") from error


def _path_of(game):
    path = (game.findtext("path") or "").strip().replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def merge(existing, fresh):
    """`fresh` with everything EmulationStation or the user added to `existing` kept.

    An entry is matched on its path, then -- for a game that moved genre folder
    between releases -- on its file name when only one entry has it. What is matched
    keeps every field this module does not write and its attributes. Entries that
    match nothing (a game that left the library, a <folder>, anything another tool
    added) are kept as they were: EmulationStation skips a path whose file is gone,
    and a game that comes back finds its favourite still set.
    """
    old = [child for child in existing if child.tag == "game"]
    by_path, by_file = {}, {}
    for game in old:
        path = _path_of(game)
        by_path.setdefault(path, game)
        by_file.setdefault(posixpath.basename(path).lower(), []).append(game)

    new = list(fresh)
    matched, used = {}, set()
    for index, game in enumerate(new):
        found = by_path.get(_path_of(game))
        if found is not None and id(found) not in used:
            matched[index] = found
            used.add(id(found))
    for index, game in enumerate(new):
        if index in matched:
            continue
        name = posixpath.basename(_path_of(game)).lower()
        candidates = [entry for entry in by_file.get(name, ()) if id(entry) not in used]
        if len(candidates) == 1:
            matched[index] = candidates[0]
            used.add(id(candidates[0]))

    root = ET.Element(existing.tag or "gameList", dict(existing.attrib))
    for index, game in enumerate(new):
        previous = matched.get(index)
        if previous is not None:
            game.attrib = {**previous.attrib, **game.attrib}
            written = {child.tag for child in game}
            for child in previous:
                if child.tag in OWNED:
                    continue
                if child.tag in OWNED_WHEN_SET and child.tag in written:
                    continue
                game.append(child)
        root.append(game)
    for child in existing:
        if id(child) not in used:
            root.append(child)
    return root


def _place_images(items, backend, kind, on_progress=None):
    """Copy cached artwork into the library and return {machine: relative path}."""
    images, moved = {}, 0
    for index, item in enumerate(items, start=1):
        source = art.path_for(item.description, kind)
        if source and os.path.isfile(source):
            images[item.name] = f"./{IMAGE_DIR}/{item.name}.png"
            if backend.put_file(source, f"{IMAGE_DIR}/{item.name}.png"):
                moved += 1
        if on_progress and index % 50 == 0:
            on_progress(index, len(items))
    if on_progress:
        on_progress(len(items), len(items))
    return images, moved
