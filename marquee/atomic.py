"""Write a file all at once, or not at all.

Everything Marquee persists -- settings.ini, the parse cache, the manifest in the
library, the release catalogue -- used to be opened with "w" and written in place.
A crash, a full disk or a killed container part-way through left a truncated file,
and a truncated settings.ini is one nobody can read back. Writing beside the target
and renaming over it is atomic on every filesystem the tool runs on.
"""
import json
import os


def write_text(path, text, encoding="utf-8"):
    """Write `text` to `path` via a sibling .part file and an atomic rename."""
    partial = path + ".part"
    try:
        with open(partial, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(partial, path)
    except BaseException:
        _discard(partial)
        raise
    return path


def write_json(path, data, **dump_options):
    return write_text(path, json.dumps(data, **dump_options))


def _discard(partial):
    try:
        os.remove(partial)
    except OSError:
        pass
