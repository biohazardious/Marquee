"""Errors the pipeline raises instead of exiting.

The old code called sys.exit() from deep inside the parsing and copying helpers, which
made it impossible to drive from anything but a terminal. Every one of those is now an
exception that the caller decides what to do with.
"""


class MarqueeError(Exception):
    """Something the user can act on: a missing file, a bad setting, a failed fetch."""


class ConfigError(MarqueeError):
    """The configuration is incomplete or cannot be read."""


class SourceNotFoundError(MarqueeError):
    """A required MAME data file could not be located or fetched."""


class VersionMismatchError(MarqueeError):
    """The MAME XML and the catlist are for different MAME versions."""
