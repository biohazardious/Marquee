"""Files in the library that are not Marquee's to touch.

A Batocera `roms/mame` folder holds things Marquee did not put there: a BIOS pack
drops `neogeo.zip`, `pgm.zip`, `cpzn1.zip` and the like at its top level for the older
libretro cores that want split sets. To the diff they are zips nothing wants, one tick
away from deletion. The ignore list takes them out of the diff altogether: never
deleted, never moved, never counted.

A pattern is a path from the library's top, `/` between folders. `*` and `?` stand
for any characters within one name and never cross a folder, so `*.zip` is every zip
at the top level and nothing below it, and `Unlisted/*` is what sits directly in
Unlisted. Case does not matter: a share on a console may not preserve it.
"""
import fnmatch


def normalize(pattern):
    """A pattern as the matcher reads it; empty when there is nothing to match."""
    text = (pattern or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/").lower()


def patterns(values):
    """The usable patterns of a list, normalised and without duplicates, in order."""
    seen, out = set(), []
    for value in values or ():
        pattern = normalize(value)
        if pattern and pattern not in seen:
            seen.add(pattern)
            out.append(pattern)
    return out


def matches(relpath, compiled):
    """Whether a library-relative path is covered by any of the (normalised) patterns."""
    parts = relpath.replace("\\", "/").lower().split("/")
    for pattern in compiled:
        wanted = pattern.split("/")
        if len(wanted) == len(parts) and all(
                fnmatch.fnmatchcase(part, piece) for part, piece in zip(parts, wanted)):
            return True
    return False


def split(existing, compiled, wanted_paths):
    """(what the diff should see, [ignored paths]) from an index {relpath: size}.

    A path the selection itself wants is still Marquee's whatever the list says: a
    pattern written too wide must not make a game's own zip look absent, and so
    have the next run copy over it.
    """
    if not compiled:
        return existing, []
    kept, ignored = {}, []
    for relpath, size in existing.items():
        if relpath not in wanted_paths and matches(relpath, compiled):
            ignored.append(relpath)
        else:
            kept[relpath] = size
    return kept, sorted(ignored)
