"""Configuration, as a value rather than a module-level dictionary.

The old `global_config` dict meant every function reached into module state, so two
runs could not coexist and nothing could be tested without mutating a global. Settings
now travel as a Config object built from defaults, then settings.ini, then whatever the
caller overrides.
"""
import ast
import configparser
import os
from dataclasses import dataclass, field, replace

from . import atomic, backends
from .errors import ConfigError

# The CHD folder is not required: left empty it is the ROM folder, and the CHD set is
# found inside it the same way the ROM set is. A first run used to stop on "missing
# chd_dir" for someone who has no disks at all.
REQUIRED_PATHS = ("rom_dir", "copy_path")
# Paths in settings.ini are resolved against the settings file, never the shell's cwd.
RESOLVED_PATHS = ("mame_xml", "catlist_ini", "category_ini", "screenless_ini",
                  "genre_ini", "rom_dir", "chd_dir")

SUGGESTED_BLACKLIST = ['Board Game', 'Calculator', 'Computer', 'Digital Simulator',
                       'Electromechanical', 'Game Console', 'Handheld',
                       'Medical Equipment', 'Printer', 'Radio', 'Slot Machine',
                       'System', 'Tablet', 'Telephone', 'Utilities']


@dataclass
class Config:
    rom_dir: str = None
    chd_dir: str = None
    copy_path: str = None

    allow_mature: bool = True
    # One game, one ROM: keep each family's parent and set its other versions aside.
    parents_only: bool = False
    # Write gamelist.xml into every destination folder, so EmulationStation shows
    # titles, years and artwork rather than a list of eight-letter filenames.
    write_gamelist: bool = True
    copy_artwork: bool = True
    mature_rom_folder: str = "ZZ-Adult"
    blacklist_genres: list = field(default_factory=list)
    # The finer level: "Shooter / Flying Vertical" rather than "Shooter".
    blacklist_categories: list = field(default_factory=list)
    blacklist_roms: list = field(default_factory=list)

    # The MAME version the source romset is. Chosen once and remembered; both the XML
    # and the catlist are then fetched for exactly this release.
    mame_version: str = None
    # The MAME the console actually runs, when it is older than the set. Batocera
    # ships what it ships -- 0.285 under a 0.289 library -- and a game that release
    # does not have, or whose zip lacks a ROM it wants, is filed apart rather than
    # listed among the ones that start. Empty is off.
    console_mame_version: str = None
    version_mismatch_folder: str = "ZZ-Version-Mismatch"
    missing_rom_folder: str = "ZZ-Missing-ROM"

    # --- acquisition -------------------------------------------------------- #
    # Where torrents are fetched. Empty means the romset is already on disk and this
    # tool only categorises and copies it, as it always did.
    download_client: str = None
    download_username: str = None
    download_password: str = None
    # The torrent folder as *this* process sees it. Often not the path the download
    # client reports, which is why the mappings below exist.
    download_dir: str = None
    # "remote -> local" per line, rewriting the client's view of a path into ours.
    remote_path_mappings: str = None
    # Hardlink the library out of the torrent folder when both sit on one filesystem:
    # the categorised copy then costs nothing and seeding carries on undisturbed.
    #
    # None is "automatic": on exactly when a link from the torrent folder into the
    # library actually works (see backends.effective_hardlink), which is the only
    # honest test -- two bind mounts of one disk share a device number and still
    # refuse. A first run should not have to know what a filesystem is to get the
    # 343 GB library that costs nothing extra. True and False pin it. Nothing in
    # Marquee writes into a library file in place, so the link cannot be written
    # through by us; an emulator that writes into a ROM would, which is why it can
    # still be turned off.
    hardlink: object = None

    # Optional overrides; everything here is discovered when left unset.
    mame_xml: str = None
    catlist_ini: str = None
    screenless_ini: str = None

    settings_path: str = None
    # Settings that were read but no longer do anything, so the CLI can say so.
    # A plain attribute would not survive dataclasses.replace().
    obsolete_keys: list = field(default_factory=list)

    def __post_init__(self):
        if not self.chd_dir and self.rom_dir:
            self.chd_dir = self.rom_dir

    @property
    def missing_paths(self):
        return [name for name in REQUIRED_PATHS if not getattr(self, name)]

    @property
    def is_remote(self):
        return backends.is_remote(self.copy_path)

    @property
    def acquires(self):
        """Whether this configuration downloads, or only works with local files."""
        return bool(self.download_client)

    @property
    def path_mappings(self):
        from .acquisition import parse_mappings
        return parse_mappings(self.remote_path_mappings)

    def with_overrides(self, **overrides):
        """A copy with the given non-None values applied."""
        values = {k: v for k, v in overrides.items() if v is not None}
        # A disk folder that is only the ROM folder by default follows it: moving
        # the ROM folder used to leave the disks being looked for in the old one.
        if "rom_dir" in values and "chd_dir" not in values \
                and self.chd_dir == self.rom_dir:
            values["chd_dir"] = None
        return replace(self, **values)


def read_settings_file(path):
    """Parse a settings.ini into a Config. Raises ConfigError on anything unusable."""
    if not os.path.isfile(path):
        raise ConfigError(f"Settings file not found: {path}")

    # interpolation=None: a "%" anywhere -- in a password, in a path like
    # /media/100% full -- otherwise raises InterpolationSyntaxError and takes the
    # whole settings file with it. Nothing here has ever used %(name)s substitution.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    if not parser.has_section("config"):
        raise ConfigError(f"No [config] section in {path}")

    values = dict(parser.items("config"))
    config = Config(settings_path=path)

    def flag(key, fallback):
        # configparser hands back strings, so "False" used to be truthy and the
        # mature filter never fired. And "maybe" used to be a bare ValueError with a
        # traceback rather than a message naming the key.
        try:
            return parser.getboolean("config", key, fallback=fallback)
        except ValueError as error:
            raise ConfigError(f"'{key}' in {path} must be true or false, "
                              f"not '{values.get(key)}'.") from error

    config.allow_mature = flag("allow_mature", True)

    for key in ("blacklist_genres", "blacklist_categories", "blacklist_roms"):
        raw = values.get(key)
        if raw is None:
            continue
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError) as error:
            raise ConfigError(f"Could not parse '{key}' in {path}: {error}") from error
        if isinstance(parsed, str):
            # A lone name without brackets. Iterating the string would have made the
            # blacklist its letters, which matched nothing and said nothing.
            parsed = [parsed]
        if (not isinstance(parsed, (list, tuple))
                or not all(isinstance(entry, str) for entry in parsed)):
            raise ConfigError(f"'{key}' in {path} must be a list of names, "
                              f"like ['pacman', 'galaga'].")
        setattr(config, key, list(parsed))

    base_dir = os.path.dirname(os.path.abspath(path))
    for key in RESOLVED_PATHS:
        value = values.get(key)
        if not value:
            continue
        if not os.path.isabs(value):
            value = os.path.normpath(os.path.join(base_dir, value))
        # catlist.ini used to be configured under the misleading name category_ini.
        target = "catlist_ini" if key == "category_ini" else key
        if hasattr(config, target):
            setattr(config, target, value)

    for key in ("copy_path", "mature_rom_folder", "mame_version",
                "console_mame_version", "version_mismatch_folder", "missing_rom_folder",
                "download_client", "download_username", "download_password",
                "download_dir", "remote_path_mappings"):
        if values.get(key):
            setattr(config, key, values[key])

    for key, fallback in (("parents_only", False),
                          ("write_gamelist", True), ("copy_artwork", True)):
        if key in values:
            setattr(config, key, flag(key, fallback))
    # "auto", blank or absent is automatic; anything else is a yes or a no.
    if (values.get("hardlink") or "").strip().lower() not in ("", "auto", "automatic"):
        config.hardlink = flag("hardlink", False)

    config.obsolete_keys = [key for key in ("genre_ini",) if values.get(key)]
    return config


SETTINGS_NAME = "settings.ini"


def settings_file(path):
    """Accept either the settings file or the directory holding it.

    The container mounts a `/config` volume rather than a file, and that is the
    natural thing to point `--config` at.
    """
    if path and os.path.isdir(path):
        return os.path.join(path, SETTINGS_NAME)
    return path


def load(settings_path=None, **overrides):
    """Defaults, then settings.ini if it is there, then the caller's overrides."""
    settings_path = settings_file(settings_path)
    if settings_path and os.path.isfile(settings_path):
        config = read_settings_file(settings_path)
    else:
        config = Config(settings_path=settings_path)
    return config.with_overrides(**overrides)


SETTINGS_TEMPLATE = """; Written by Marquee. Safe to edit by hand.
[config]
; catlist.ini and mameXXXX.xml are found automatically, and downloaded to match your
; romset version when they are not on disk. Set catlist_ini or mame_xml to override.

rom_dir = {rom_dir}
chd_dir = {chd_dir}
copy_path = {copy_path}
{overrides}
mame_version = {mame_version}

allow_mature = {allow_mature}
mature_rom_folder = {mature_rom_folder}

; The MAME the console runs, when it is older than the set. Games it does not have
; go under the first folder, games whose zip lacks a ROM it wants under the second.
; Leave it empty when the console runs the set's own release.
console_mame_version = {console_mame_version}
version_mismatch_folder = {version_mismatch_folder}
missing_rom_folder = {missing_rom_folder}

; One game, one ROM: keep each family's parent and leave its other versions out.
parents_only = {parents_only}

; What the console sees: one gamelist.xml at the library root, and the artwork
; beside the games.
write_gamelist = {write_gamelist}
copy_artwork = {copy_artwork}

blacklist_genres = {blacklist_genres}

blacklist_categories = {blacklist_categories}

blacklist_roms = {blacklist_roms}

; --- acquisition -----------------------------------------------------------
; Leave download_client empty to work only with a romset already on disk.
{acquisition}"""


ACQUISITION_EXAMPLE = """;download_client = http://192.168.1.10:8080/
;download_username = admin
;download_password =
;download_dir = /downloads
; One "remote -> local" per line when the client sees a different path than we do.
;remote_path_mappings = /downloads -> /mnt/nas/downloads
hardlink = {hardlink}
"""


def _indent(text):
    """Continuation lines need indenting, or configparser reads them as new keys."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return ("\n" + " " * 4).join(lines)


def _space(text):
    """A leading space only when there is something after it."""
    value = _indent(text)
    return f" {value}" if value else ""


def _acquisition_block(config):
    """The download-client settings, written out rather than dropped.

    This block used to be commented-out examples only, so saving from the web UI --
    which rewrites the whole file -- silently wiped the configured download client,
    its credentials and the path mappings.
    """
    if not config.download_client:
        return ACQUISITION_EXAMPLE.format(hardlink=_hardlink(config.hardlink))
    return (
        f"download_client = {config.download_client}\n"
        f"download_username = {config.download_username or ''}\n"
        f"download_password = {config.download_password or ''}\n"
        f"download_dir = {config.download_dir or ''}\n"
        f"; One \"remote -> local\" per line when the client sees a different path.\n"
        f"remote_path_mappings ={_space(config.remote_path_mappings)}\n"
        f"hardlink = {_hardlink(config.hardlink)}\n")


def _hardlink(value):
    """How the setting is written: auto, True or False."""
    return "auto" if value is None else str(bool(value))


def _blank(value):
    """An unset path is an empty value, not the four letters "None".

    Writing str(None) put a folder called None into the settings, which the reader then
    resolved against the settings directory and planned against quite happily.
    """
    return "" if value is None else str(value)


def _overrides(config):
    """The discovery overrides, written back out only when they are set.

    The template tells the reader they can set these; dropping them on every save --
    and the web UI rewrites the whole file on every save -- made that untrue.
    """
    lines = [f"{key} = {getattr(config, key)}"
             for key in ("mame_xml", "catlist_ini", "screenless_ini")
             if getattr(config, key)]
    return ("\n".join(lines) + "\n") if lines else ""


def _format_list(values):
    if not values:
        return "[]"
    if len(values) == 1:
        return repr(list(values))
    joined = ",\n                   ".join(repr(value) for value in values)
    return f"[{joined}]"


def write_settings_file(path, config):
    """Write a Config back out as a settings.ini."""
    body = SETTINGS_TEMPLATE.format(
        rom_dir=_blank(config.rom_dir),
        # Blank when it is the ROM folder: that is what blank means, and writing the
        # derived value out pinned it there after the ROM folder moved.
        chd_dir=_blank(None if config.chd_dir == config.rom_dir else config.chd_dir),
        copy_path=_blank(config.copy_path), overrides=_overrides(config),
        allow_mature=config.allow_mature,
        mature_rom_folder=_blank(config.mature_rom_folder),
        console_mame_version=config.console_mame_version or "",
        version_mismatch_folder=_blank(config.version_mismatch_folder),
        missing_rom_folder=_blank(config.missing_rom_folder),
        parents_only=config.parents_only,
        write_gamelist=config.write_gamelist, copy_artwork=config.copy_artwork,
        mame_version=config.mame_version or "",
        blacklist_genres=_format_list(config.blacklist_genres),
        blacklist_categories=_format_list(config.blacklist_categories),
        blacklist_roms=_format_list(config.blacklist_roms),
        acquisition=_acquisition_block(config))
    # Atomic: a save interrupted half-way must not leave a settings file that
    # cannot be read back.
    return atomic.write_text(path, body)
