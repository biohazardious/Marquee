"""Running the job, without assuming a terminal is attached.

Split deliberately in two: `build_plan` decides everything and touches nothing, then
`execute` carries the plan out. Whoever is driving -- the CLI, a web UI -- gets to show
the plan and ask before the second half happens.
"""
import configparser
import os
import posixpath
import time
from dataclasses import dataclass, field, replace

from . import (backends, catalog, fetch, gamelist, manifest, plan as planning,
               sources, sync)
from .errors import MarqueeError, SourceNotFoundError, VersionMismatchError
from .reporting import Reporter

PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_ROOT)


@dataclass
class SourceOptions:
    """Where to look for MAME data and how hard to try."""
    # The MAME version the source romset is, chosen by the user and remembered. When set,
    # both the XML and the catlist are fetched for exactly this version.
    version: str = None
    xml: str = None
    catlist: str = None
    mame_binary: str = "mame"
    fetch_support_files: bool = False
    offline: bool = False
    ignore_version_mismatch: bool = False
    use_cache: bool = True
    refresh_cache: bool = False
    search_dirs: tuple = ()
    # Index the destination so the run can tell new from changed from already-there.
    compare_destination: bool = True

    def hints(self, config):
        found = [os.path.join(PROJECT_ROOT, "MameFiles"), PROJECT_ROOT]
        if config.settings_path:
            found.append(os.path.dirname(os.path.abspath(config.settings_path)))
        if config.mame_xml:
            found.insert(0, os.path.dirname(config.mame_xml))
        return list(self.search_dirs) + found


@dataclass
class Resolution:
    # What the destination already holds, read from its own record.
    destination: dict = None
    # Whether the library folder is actually there. A new library is a legitimate
    # thing to create, so this is a warning and not a refusal -- but it has to be
    # said. A path that exists on the host and not inside a container plans happily,
    # reports nothing already there, and then writes hundreds of gigabytes somewhere
    # nobody meant.
    destination_exists: bool = True
    # Where the sets were actually found, which is not always where the settings
    # point: a torrent lands in a folder named after itself.
    rom_dir: str = None
    chd_dir: str = None
    # None until a plan wanted a disk; then whether the CHD folder holds any.
    chd_set_found: bool = None
    xml_file: str = None
    xml_version: str = None
    catlist_file: str = None
    catlist_version: str = None
    machine_count: int = 0
    kept_count: int = 0
    # Why the rest did not make it: {reason: count}. Without this the System page can
    # only say 16,350 read and 12,027 kept, and 4,323 machines vanish unexplained.
    filtered: dict = field(default_factory=dict)
    # Every genre this catlist knows about, so a UI can offer them to blacklist
    # instead of asking the user to type them.
    genres: list = field(default_factory=list)


@dataclass
class Summary:
    machines: int = 0
    files: int = 0
    copied_bytes: int = 0
    seconds: float = 0.0
    missing_roms: list = field(default_factory=list)
    missing_chds: list = field(default_factory=list)
    cancelled: bool = False
    copied: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0
    # Files a copy or delete raised on. One full disk or one vanished source used to
    # take the whole run with it, manifest included.
    failed: int = 0
    # What the destination holds afterwards, as opposed to what this run moved.
    destination_bytes: int = 0
    gamelists: int = 0
    artwork: int = 0

    @property
    def rate(self):
        return self.copied_bytes / self.seconds if self.seconds > 0 else 0


# --------------------------------------------------------------------------- #
# Locating the MAME data
# --------------------------------------------------------------------------- #

def _destination_exists(copy_path):
    """Whether the library is a folder we can already see."""
    if not copy_path:
        return False
    if copy_path.startswith(backends.SCHEMES):
        return True          # only a connection attempt could tell, and that is later
    return os.path.isdir(copy_path)


def chosen_version(config, options):
    """The release this run is for.

    A version named in the settings counts exactly as much as one passed on the command
    line: reading only the latter meant a configuration that said "0.289" still went
    hunting the disk for any XML it could find, and failed.
    """
    return options.version or config.mame_version or None


def resolve_mame_xml(config, options, reporter):
    if options.xml and not os.path.isfile(options.xml):
        # Given on the command line and not there: falling back to whatever else is
        # lying around would run against a release the user did not ask for.
        raise SourceNotFoundError(f"The MAME XML given as --xml does not exist: {options.xml}")
    if config.mame_xml and not options.xml and not os.path.isfile(config.mame_xml):
        reporter.warn(f"Configured mame_xml does not exist: {config.mame_xml}")

    version = chosen_version(config, options)
    if version and not (options.xml or config.mame_xml):
        # The chosen release is what matters, not where the file comes from: one
        # already on disk for exactly that release is as good as a fresh download,
        # and the only thing --offline can use.
        found = sources.find_mame_xml(options.hints(config), wanted_version=version)
        if found:
            reporter.info(f"Using MAME XML: {found}")
            return found
        if options.offline:
            # What an earlier online run fetched is on disk too, in the cache: an
            # offline plan for a release planned before has no reason to stop.
            cached = fetch.cached_xml(version)
            if os.path.isfile(cached) and os.path.getsize(cached) > 0:
                reporter.info(f"Using MAME XML: {cached}")
                return cached
            raise SourceNotFoundError(
                f"MAME {version} was chosen but no XML for it is on disk and "
                f"--offline was given. Pass --xml PATH.")
        return fetch.fetch_xml(version, reporter=reporter)

    for candidate in (options.xml, config.mame_xml):
        if candidate and os.path.isfile(candidate):
            return candidate

    found = sources.find_mame_xml(options.hints(config))
    if found:
        reporter.info(f"Using MAME XML: {found}")
        return found

    generated = sources.generate_listxml(options.mame_binary, reporter=reporter)
    if generated:
        reporter.info(f"Using MAME XML generated by {options.mame_binary}: {generated}")
        return generated

    # Nothing chosen, nothing on disk, no MAME to ask -- but the source folder is
    # named after its release. A fresh install used to stop here and tell the user
    # to go and download an XML by hand, when the answer was in the path.
    guessed = sources.version_in_path(sources.locate_set(config.rom_dir, "roms"))
    if guessed and not options.offline:
        reporter.info(f"No MAME version is set; the ROM folder is named for MAME "
                      f"{guessed}, so that release is used. Set it in Settings to "
                      f"choose another.")
        return fetch.fetch_xml(guessed, reporter=reporter)

    raise SourceNotFoundError(
        "No MAME version is chosen and no mame*.xml is on disk. Pick the release in "
        "Settings and the XML is downloaded for you; or pass --xml PATH, or set "
        "mame_xml in settings.ini.")


def resolve_catlist(config, options, xml_version, reporter):
    if options.catlist or config.catlist_ini:
        candidate = options.catlist or config.catlist_ini
        if not os.path.isfile(candidate):
            raise SourceNotFoundError(f"catlist.ini not found: {candidate}")
        return candidate

    # A chosen version is explicit intent: fetch that one rather than whatever happens
    # to be lying around, which may be for another release entirely.
    version = chosen_version(config, options)
    wanted = version or xml_version
    stale = None
    if not options.fetch_support_files:
        try:
            found = sources.find_support_file("catlist.ini", options.hints(config),
                                              wanted_version=wanted)
        except sources.WrongVersionOnDisk as other:
            # Found one, for another release. Fetching the right one is better than
            # stopping on a mismatch the user did not know they had -- the file is
            # often something MAME itself left in ~/.mame years ago.
            stale = other
            found = None
        if found:
            reporter.info(f"Using catlist: {found}")
            return found
    if stale:
        reporter.info(f"Ignoring {stale.path}: it is for MAME {stale.version}, "
                      f"not {wanted}.")

    if options.offline:
        if stale:
            # Offline, and the only one here is for another release. Using it is a
            # decision, not a default: say so and let the mismatch check refuse it.
            reporter.warn(f"Offline: falling back to {stale.path}, which is for MAME "
                          f"{stale.version}.")
            return stale.path
        raise SourceNotFoundError(
            "Could not find catlist.ini and --offline was given. Put it in "
            "./MameFiles/, or pass --catlist PATH.")
    if not wanted:
        raise SourceNotFoundError(
            "Could not find catlist.ini, and the MAME XML does not say which version it "
            "is, so the right one cannot be fetched. Pass --catlist PATH.")

    path = fetch.fetch_support_file("catlist.ini", wanted,
                                    refresh=options.fetch_support_files, reporter=reporter)
    reporter.info(f"Using catlist: {path}")
    return path


def check_versions(xml_version, catlist_version, ignore_mismatch, reporter):
    reporter.info(f"MAME XML version {xml_version or 'unknown'}, "
                  f"catlist for MAME {catlist_version or 'unknown'}.")
    warning = sources.describe_version_match(xml_version, catlist_version)
    if not warning:
        return
    if ignore_mismatch:
        reporter.warn(warning)
    else:
        raise VersionMismatchError(warning)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def _parse_reporter(reporter):
    def report(position, total, machine_count):
        reporter.progress("parse", position, total, f"{machine_count} machines read")
    return report


def build_plan(config, options=None, reporter=None):
    """Work out what the run would do. Copies nothing."""
    options = options or SourceOptions()
    reporter = reporter or Reporter()
    resolution = Resolution()

    resolution.destination_exists = _destination_exists(config.copy_path)
    if not resolution.destination_exists:
        reporter.warn(f"The library folder does not exist yet: {config.copy_path}. "
                      f"It will be created. If you expected it to be there already, "
                      f"check the path -- inside a container it has to be the path the "
                      f"container sees.")
    resolution.destination = manifest.describe(manifest.read(config.copy_path))
    if resolution.destination and resolution.destination.get("mame_version"):
        reporter.info(f"Destination currently holds MAME "
                      f"{resolution.destination['mame_version']} "
                      f"({resolution.destination.get('machines') or 0} machines).")

    resolution.xml_file = resolve_mame_xml(config, options, reporter)
    resolution.xml_version = sources.read_xml_build(resolution.xml_file)
    resolution.catlist_file = resolve_catlist(config, options, resolution.xml_version, reporter)
    _file_version, resolution.catlist_version = sources.read_ini_version(resolution.catlist_file)
    check_versions(resolution.xml_version, resolution.catlist_version,
                   options.ignore_version_mismatch, reporter)

    data = sources.load_machines(resolution.xml_file, use_cache=options.use_cache,
                                 refresh=options.refresh_cache,
                                 on_progress=_parse_reporter(reporter), reporter=reporter)
    machines = data["machines"]
    resolution.machine_count = len(machines)

    screenless = catalog.derive_screenless(machines, config.screenless_ini, reporter)
    rejects = {}
    mame_list = catalog.filter_machines(machines, screenless, config,
                                        resolution.filtered, rejects)
    resolution.kept_count = len(mame_list)
    reporter.info(f"{len(mame_list)} of {len(machines)} machines kept.")

    catlist = configparser.ConfigParser(allow_no_value=True)
    catlist.read(resolution.catlist_file)

    reporter.stage("It's time to categorize that huge list!.")
    resolution.genres = sorted(sources.derive_genres(catlist))
    mame_list = catalog.categorize(mame_list, catlist, config, reporter)
    if config.parents_only:
        mame_list = catalog.collapse_clones(mame_list, reporter)

    rom_dir = sources.locate_set(config.rom_dir, "roms", version=resolution.xml_version)
    chd_dir = sources.locate_set(config.chd_dir, "chds", version=resolution.xml_version)
    for label, chosen, given in (("ROM", rom_dir, config.rom_dir),
                                 ("CHD", chd_dir, config.chd_dir)):
        if chosen != given:
            reporter.info(f"{label} folder: using {chosen} -- the set is in there, not "
                          f"directly in {given}.")
    resolution.rom_dir, resolution.chd_dir = rom_dir, chd_dir

    built = planning.build(mame_list, rom_dir, chd_dir,
                           catalog.folder_namer(config), config.allow_mature)
    built.left_out = _left_out(rejects, catlist, config, rom_dir, chd_dir, reporter)

    if options.compare_destination:
        built.sync = compare_destination(built, config, reporter)
    else:
        # Declined, not deferred. `execute` used to compare anyway, so --no-compare
        # changed what the dry run printed and nothing else.
        built.sync = sync.blind(built)

    # Disks wanted and none to be had is worth a sentence of its own: 311 "missing"
    # disks read as nothing at all when the CHD set had simply never been fetched,
    # and a library sat a hundred gigabytes short for it.
    if any(item.disks for item in built.wanted):
        resolution.chd_set_found = sources.holds_disks(chd_dir)
    to_fetch = built.disks_to_fetch
    if to_fetch and resolution.chd_set_found is False:
        reporter.warn(f"No CHD set under {config.chd_dir}: {len(to_fetch)} wanted disks "
                      f"are neither there nor in the library. Fetch them from the "
                      f"Wanted page, or point the CHD folder in Settings at a CHD set.")
    return built, resolution


def _left_out(rejects, catlist, config, rom_dir, chd_dir, reporter):
    """The machines that did not make it, as a catalogue of their own.

    Built the same way as the library so the page can show them in the same shape --
    genre, category, adult folder and all. Categorised with no blacklists applied:
    these are already out, and marking them out again would say nothing.
    """
    if not rejects:
        return None
    reasons = {name: entry.pop("reason", "") for name, entry in rejects.items()}
    bare = replace(config, blacklist_genres=[], blacklist_categories=[],
                   blacklist_roms=[])
    listed = catalog.categorize(rejects, catlist, bare)
    plan = planning.build(listed, rom_dir, chd_dir,
                          catalog.folder_namer(config), allow_mature=True)
    for item in plan.wanted:
        item.reason = reasons.get(item.name, "")
    reporter.info(f"{len(plan.wanted)} machines left out of the library.")
    return plan


def compare_destination(built, config, reporter=None):
    """Index the destination and work out what actually has to move."""
    reporter = reporter or Reporter()
    reporter.stage("Checking what is already at the destination..")
    backend = None
    try:
        backend = backends.for_destination(config.copy_path, reporter=reporter,
                                           hardlink=config.hardlink)
        existing = backend.index()
    except Exception as error:  # noqa: BLE001 - a destination that cannot be read is
        # not fatal; the run simply cannot tell moves from new files.
        reporter.warn(f"Could not read the destination ({error}); "
                      f"treating everything as new.")
        existing = {}
    finally:
        if backend is not None:
            backend.close()

    report = sync.compare(built, existing)
    reporter.info(
        f"{report.counts[sync.KEEP]} already there, {report.counts[sync.NEW]} new, "
        f"{report.counts[sync.UPDATE]} changed, {report.counts[sync.MOVE]} moved, "
        f"{report.counts[sync.ORPHAN]} no longer wanted.")
    return report


# --------------------------------------------------------------------------- #
# Executing
# --------------------------------------------------------------------------- #

def execute(built, config, reporter=None, backend=None, should_continue=None,
            delete_orphans=False, mame_version=None):
    """Carry out a plan. The caller has already decided this should happen.

    Works from the sync report rather than the raw item list, so a machine that only
    changed category is renamed instead of re-copied, and files nothing wants any more
    can be removed.

    `should_continue` is checked between files so a long run can be stopped from a UI
    without killing the process; the next run picks up whatever already arrived.
    """
    reporter = reporter or Reporter()
    owned = backend is None
    backend = backend or backends.for_destination(config.copy_path, reporter=reporter,
                                                  hardlink=config.hardlink)
    try:
        return _execute(built, config, reporter, backend, should_continue,
                        delete_orphans, mame_version)
    finally:
        if owned:
            backend.close()


def _execute(built, config, reporter, backend, should_continue, delete_orphans,
             mame_version):
    report = built.sync or compare_destination(built, config, reporter)

    started = time.perf_counter()
    summary = Summary(files=built.file_count, missing_roms=built.missing_roms,
                      missing_chds=built.missing_chds,
                      skipped=report.counts.get(sync.KEEP, 0))
    total_bytes = report.to_transfer
    state = {"bytes": 0, "name": ""}

    def moved_bytes(count):
        # Progress is counted in bytes, not files: one 11 GB CHD would otherwise leave
        # the bar motionless for minutes.
        state["bytes"] += count
        reporter.progress("copy", state["bytes"], total_bytes, state["name"])

    def keep_going():
        if should_continue is not None and not should_continue():
            summary.cancelled = True
            return False
        return True

    # Renames first: they are near-instant and can free a path a copy wants.
    for action in report.of(sync.MOVE):
        if not keep_going():
            break
        try:
            backend.move(action.from_relpath, action.relpath)
            summary.moved += 1
        except (OSError, MarqueeError) as error:
            summary.failed += 1
            reporter.warn(f"Could not move {action.from_relpath}: {error}")
    if summary.moved:
        reporter.info(f"Relocated {summary.moved} files without re-copying them.")

    reporter.stage("Copying")
    for action in report.of(sync.NEW) + report.of(sync.UPDATE):
        if not keep_going():
            break
        state["name"] = posixpath.basename(action.relpath)
        # Said now, not on the first byte callback: a backend that reports per file
        # left the page naming the previous file for the whole of the next one.
        reporter.progress("copy", state["bytes"], total_bytes, state["name"])
        try:
            # An UPDATE means the diff knows the file there is wrong -- possibly at
            # exactly the size of the right one -- so the backend's same-size skip
            # must not have the last word.
            backend.copy(action.source, posixpath.dirname(action.relpath), moved_bytes,
                         replace=action.kind == sync.UPDATE)
        except (OSError, MarqueeError) as error:
            summary.failed += 1
            reporter.warn(f"Could not copy {action.relpath}: {error}")
            continue
        if action.kind == sync.NEW:
            summary.copied += 1
        else:
            summary.updated += 1

    if delete_orphans and not summary.cancelled:
        for action in report.of(sync.ORPHAN):
            if not keep_going():
                break
            try:
                backend.delete(action.relpath)
            except (OSError, MarqueeError) as error:
                summary.failed += 1
                reporter.warn(f"Could not remove {action.relpath}: {error}")
                continue
            summary.deleted += 1
        if summary.deleted:
            reporter.info(f"Removed {summary.deleted} files the plan no longer wants.")

    if summary.failed:
        reporter.warn(f"{summary.failed} file(s) could not be written or removed; "
                      f"the next run will try them again.")

    if summary.cancelled:
        reporter.warn("Stopped. Whatever arrived is kept; the next run continues.")

    if config.write_gamelist and not summary.cancelled:
        reporter.stage("Writing gamelists..")
        try:
            result = gamelist.write(
                built.library_items, backend, copy_images=config.copy_artwork,
                reporter=reporter,
                on_progress=lambda done, total:
                    reporter.progress("gamelist", done, total))
            summary.gamelists = result["games"]
            summary.artwork = result["images"]
        except Exception as error:  # noqa: BLE001 - a cosmetic step must not lose a
            # transfer that otherwise worked.
            reporter.warn(f"Could not write the gamelists: {error}")

    # What the destination holds now, not what the source folder held: with a partial
    # torrent folder the two differ by every game that was already in the library.
    summary.machines = len(built.library_items)
    summary.copied_bytes = state["bytes"]
    summary.destination_bytes = report.at_destination
    summary.seconds = time.perf_counter() - started

    written = manifest.write(config.copy_path, config, mame_version, summary)
    if written:
        reporter.info(f"Recorded what this destination now holds in {manifest.NAME}.")
    return summary
