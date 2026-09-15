"""One downloadable set."""
from dataclasses import dataclass, field

ROMS = "roms"
CHDS = "chds"
BIOS = "bios"
SL_ROMS = "sl_roms"
SL_CHDS = "sl_chds"
EXTRAS = "extras"
UPDATE = "update"
# The previous version of every file a release changed: how you go backwards.
ROLLBACK = "rollback"

# Only these two are used to build a library. The others are indexed so the UI can
# show what exists, and so a future software-list library has somewhere to start.
BUILDABLE = (ROMS, CHDS)

MERGED = "merged"
NON_MERGED = "non-merged"
SPLIT = "split"


@dataclass(frozen=True)
class Release:
    kind: str
    version: str
    infohash: str
    name: str
    variant: str = None
    datfile_url: str = None
    # Update sets bridge exactly one step, e.g. 0.288 -> 0.289.
    from_version: str = None
    magnet: str = None
    trackers: tuple = field(default_factory=tuple)

    @property
    def is_full_set(self):
        return self.kind in BUILDABLE and not self.from_version

    @property
    def key(self):
        return (self.kind, self.version, self.variant or "")

    def magnet_uri(self):
        if self.magnet:
            return self.magnet
        from urllib.parse import quote
        uri = f"magnet:?xt=urn:btih:{self.infohash}&dn={quote(self.name)}"
        return uri + "".join(f"&tr={quote(tracker)}" for tracker in self.trackers)
