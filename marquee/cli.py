"""The terminal front end: flags, prompts, printing and exit codes.

Everything that assumes a terminal lives here. The pipeline below it neither prints nor
asks, which is what lets a web UI drive the same code.
"""
import argparse
import os
import sys

from . import config as configuration
from . import pipeline, plan as planning, sync
from .errors import MarqueeError
from .reporting import TerminalReporter

DEFAULT_SETTINGS = os.path.join(pipeline.PROJECT_ROOT, "settings.ini")


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(prompt, default=None, must_exist=False):
    while True:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{prompt}{suffix}: ").strip() or (default or "")
        if not answer:
            print("  Please give a value.")
            continue
        answer = os.path.expanduser(answer)
        if must_exist and not os.path.isdir(answer):
            print(f"  '{answer}' is not a directory.")
            continue
        return answer


def ask_yes_no(prompt, default=True):
    answer = input(f"{prompt} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def run_setup(settings_path=DEFAULT_SETTINGS):
    """Ask for the three paths that cannot be guessed and write a settings.ini."""
    print("Setting up Marquee. Press Enter to accept a default in brackets.\n")

    if os.path.isfile(settings_path) and not ask_yes_no(
            f"{settings_path} already exists. Overwrite it?", default=False):
        print("Left alone.")
        return None

    rom_dir = ask("Folder holding your non-merged ROM zips", must_exist=True)
    built = configuration.Config(
        rom_dir=rom_dir,
        chd_dir=ask("Folder holding your CHD subfolders", default=rom_dir, must_exist=True),
        copy_path=ask("Destination (a folder, or smb://user:pass@host/share/path)"),
        allow_mature=ask_yes_no("Include adult titles?", default=True),
        settings_path=settings_path)
    if ask_yes_no("Skip non-arcade machines (calculators, slot machines, ...)?", default=True):
        built.blacklist_genres = list(configuration.SUGGESTED_BLACKLIST)

    configuration.write_settings_file(settings_path, built)
    print(f"\nWrote {settings_path}. Run marquee again to start copying.")
    return built


def load_config(args):
    """Defaults, then settings.ini, then the command line, then maybe the wizard."""
    # --config may name the file or the directory holding it, the way a container
    # mounts /config rather than a single file.
    args.config = configuration.settings_file(args.config)
    explicitly_asked = args.config != DEFAULT_SETTINGS
    if explicitly_asked and not os.path.isfile(args.config):
        raise MarqueeError(f"Settings file not found: {args.config}")

    built = configuration.load(args.config, rom_dir=args.rom_dir, chd_dir=args.chd_dir,
                               copy_path=args.dest, allow_mature=args.allow_mature)
    for key in built.obsolete_keys:
        print(f"Note: {key} is no longer used; genres are derived from the catlist.")

    if built.missing_paths and interactive():
        print(f"Nothing configured yet ({', '.join(built.missing_paths)}).")
        if ask_yes_no("Set it up now?", default=True):
            run_setup(args.config)
            built = configuration.load(args.config, rom_dir=args.rom_dir,
                                       chd_dir=args.chd_dir, copy_path=args.dest,
                                       allow_mature=args.allow_mature)

    if built.missing_paths:
        raise MarqueeError(
            f"Missing required setting(s): {', '.join(built.missing_paths)}. Set them in "
            f"{args.config}, pass --rom-dir/--chd-dir/--dest, or run --setup.")

    for key in ("rom_dir", "chd_dir"):
        if not os.path.isdir(getattr(built, key)):
            print(f"Warning: {key} is not a directory: {getattr(built, key)}")
    return built


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def describe_plan(built, config):
    """Print the plan. Returns False when it will not fit at the destination."""
    print(f"{len(built.items)} machines, {built.file_count} files, "
          f"{planning.human_bytes(built.total_bytes)} in the set.")
    if built.mature_filtered:
        print(f"{len(built.mature_filtered)} mature ROMs filtered out.")

    report = built.sync
    if report is not None:
        print(f"  {report.counts[sync.KEEP]} already there"
              f" · {report.counts[sync.NEW]} new"
              f" · {report.counts[sync.UPDATE]} changed"
              f" · {report.counts[sync.MOVE]} to relocate"
              f" · {report.counts[sync.ORPHAN]} no longer wanted"
              f" ({planning.human_bytes(report.bytes[sync.ORPHAN])})")

    if built.missing_roms or built.missing_chds:
        print("Some files are missing!")
        if built.missing_roms:
            print("Missing ROMs: ")
            print(', '.join(built.missing_roms))
        if built.missing_chds:
            print("Missing CHDs: ")
            print(', '.join(built.missing_chds))

    space = planning.check_free_space(built, config.copy_path)
    if space is None:
        return True
    needed, free = space
    print(f"{planning.human_bytes(needed)} to write, "
          f"{planning.human_bytes(free)} free at the destination.")
    if needed > free:
        print(f"Not enough room: {planning.human_bytes(needed - free)} short.")
        return False
    return True


def describe_summary(summary):
    line = (f"Done in {summary.seconds:.0f}s. {summary.copied} copied, "
            f"{summary.updated} updated, {summary.moved} relocated, "
            f"{summary.skipped} already there")
    if summary.deleted:
        line += f", {summary.deleted} removed"
    print(line + f". {planning.human_bytes(summary.copied_bytes)} written"
          + (f" at {planning.human_bytes(summary.rate)}/s." if summary.rate else "."))
    if summary.missing_roms or summary.missing_chds:
        print(f"{len(summary.missing_roms)} ROMs and {len(summary.missing_chds)} CHDs "
              f"were missing.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_arg_parser():
    parser = argparse.ArgumentParser(prog="marquee",
                                     description="Clean, categorize and copy a MAME romset.")
    parser.add_argument("-c", "--config", default=DEFAULT_SETTINGS,
                        help="path to settings.ini (default: next to this script)")
    parser.add_argument("--setup", action="store_true",
                        help="ask for the paths and write a settings.ini, then exit")

    paths = parser.add_argument_group("paths (override settings.ini)")
    paths.add_argument("--rom-dir", help="folder holding the ROM zips")
    paths.add_argument("--chd-dir", help="folder holding the CHD subfolders")
    paths.add_argument("--dest", help="destination folder or smb:// URL")

    data = parser.add_argument_group("MAME data")
    data.add_argument("--mame-version", metavar="X.XXX",
                      help="the MAME release your romset is; the XML and catlist are then "
                           "fetched for exactly that version")
    data.add_argument("--list-versions", action="store_true",
                      help="show which MAME versions can be fetched, then exit")
    data.add_argument("--xml", help="MAME -listxml output to read (default: auto-detect)")
    data.add_argument("--catlist", help="catlist.ini to categorize with (default: auto-detect)")
    data.add_argument("--mame-binary", default="mame",
                      help="mame executable used to generate the XML if none is found")
    data.add_argument("--fetch-support-files", action="store_true",
                      help="download a version-matched catlist.ini instead of looking on disk")
    data.add_argument("--offline", action="store_true", help="never use the network")
    data.add_argument("--ignore-version-mismatch", action="store_true",
                      help="continue when the XML and catlist are for different MAME versions")
    data.add_argument("--no-cache", action="store_true", help="do not read or write the parse cache")
    data.add_argument("--refresh-cache", action="store_true", help="re-parse the XML and rewrite the cache")

    web = parser.add_argument_group("web UI")
    web.add_argument("--web", action="store_true",
                     help="serve the browser interface instead of running on the terminal")
    web.add_argument("--host", default="127.0.0.1",
                     help="address to bind the web UI to (default: this machine only)")
    web.add_argument("--port", type=int, default=8777, help="web UI port (default: 8777)")
    web.add_argument("--open", action="store_true", dest="open_browser",
                     help="open the web UI in a browser once it starts")

    run = parser.add_argument_group("run")
    run.add_argument("-n", "--dry-run", action="store_true",
                     help="report what would be copied and stop")
    run.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    run.add_argument("-q", "--quiet", action="store_true", help="only print warnings and the summary")
    run.add_argument("--delete-orphans", action="store_true",
                     help="remove destination files the plan no longer wants "
                          "(useful after a romset upgrade)")
    run.add_argument("--no-compare", action="store_true",
                     help="skip indexing the destination; copy without detecting moves")
    run.add_argument("--report", metavar="PATH",
                     help="write the plan and the missing files to a .json or .csv file")
    mature = run.add_mutually_exclusive_group()
    mature.add_argument("--allow-mature", dest="allow_mature", action="store_true", default=None,
                        help="include adult titles")
    mature.add_argument("--no-mature", dest="allow_mature", action="store_false",
                        help="exclude adult titles")
    return parser


def options_from(args):
    return pipeline.SourceOptions(
        version=args.mame_version, xml=args.xml, catlist=args.catlist,
        mame_binary=args.mame_binary,
        fetch_support_files=args.fetch_support_files, offline=args.offline,
        ignore_version_mismatch=args.ignore_version_mismatch,
        use_cache=not args.no_cache, refresh_cache=args.refresh_cache,
        compare_destination=not args.no_compare)


def run(args):
    if args.setup:
        run_setup(args.config)
        return 0

    if args.list_versions:
        from . import fetch
        for entry in fetch.version_catalogue():
            mark = "ready      " if entry["usable"] else "unavailable"
            print(f"  {entry['version']}  {mark}  {entry['reason'] or ''}")
        return 0

    if args.web:
        from .web import serve
        serve(args.config, host=args.host, port=args.port, open_browser=args.open_browser)
        return 0

    config = load_config(args)
    if args.mame_version:
        config.mame_version = args.mame_version
    reporter = TerminalReporter(quiet=args.quiet)
    built, resolution = pipeline.build_plan(config, options_from(args), reporter)

    fits = describe_plan(built, config)

    if args.report:
        planning.write_report(built, args.report)
        print(f"Report written to {args.report}")

    if args.dry_run:
        print("Dry run, nothing was copied.")
        return 0

    if not fits and not args.yes:
        print("Refusing to start. Free some space, or pass --yes to try anyway.")
        return 1

    if not args.yes and not ask_yes_no("Start the transfer?", default=False):
        return 0

    describe_summary(pipeline.execute(built, config, reporter,
                                      delete_orphans=args.delete_orphans,
                                      mame_version=config.mame_version
                                      or resolution.xml_version))
    return 0


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except MarqueeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
