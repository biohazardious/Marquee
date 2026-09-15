"""Where releases are found.

An indexer turns a publisher's listing into `Release` records: what set, which MAME
version, and the magnet that fetches it. It never downloads anything itself.
"""
from .release import (Release, ROMS, CHDS, BIOS, SL_ROMS, SL_CHDS, EXTRAS,
                      UPDATE, ROLLBACK)

__all__ = ["Release", "ROMS", "CHDS", "BIOS", "SL_ROMS", "SL_CHDS", "EXTRAS",
           "UPDATE", "ROLLBACK", "default"]


def default():
    from .pleasuredome import Pleasuredome
    return Pleasuredome()
