"""Deciding which machines survive, what they are called, and where they go.

Everything here is a pure function of the parsed machine records plus a Config. It
touches no module state and prints nothing, so the same call serves the CLI, a web UI
and the tests.
"""
import configparser
import os
import re

from . import sources
from .reporting import Reporter

# Descriptions look like "Foo (prototype)", "Foo (Japan, proto)", "Foo (beta 2)". The
# original pattern was r'\(proto|prototype\)|\(beta\)', whose alternation was
# unparenthesised: it read as "(proto" OR "prototype)" OR "(beta)".
PROTO_BETA_RE = re.compile(r'\([^)]*\b(?:proto|prototype|beta)\b[^)]*\)', re.IGNORECASE)
BETA_BOOTLEG_RE = re.compile(r'beta.*bootleg|bootleg.*beta', re.IGNORECASE)

# Machines the catlist says nothing about. Named so it can be excluded like any genre.
UNLISTED = "Unlisted"


def is_skipped_machine(record):
    """BIOS, device, mechanical and non-runnable sets are never playable entries."""
    return record["bios"] or record["dev"] or record["mech"] or not record["run"]


def chd_destination(record):
    """The folder name this machine's disks belong under at the destination.

    A clone whose <disk> has a `merge` attribute shares the parent's file, so it goes
    under the parent's name and MAME's parent lookup finds it; copying a multi-gigabyte
    disk twice is the only alternative. A clone without `merge` has a disk of its own
    and gets its own folder. The old code always used cloneof, which pointed the second
    case at a different disk entirely.
    """
    name, cloneof = record["n"], record["c"]
    if cloneof and any(merge for _disk, merge in record["dk"]):
        return cloneof
    return name


def derive_screenless(machines, cross_check_ini=None, reporter=None):
    """Machines with no <display>, which is exactly what screenless.ini records.

    When the user still has a screenless.ini we compare against it and report the
    difference rather than trusting either blindly.
    """
    reporter = reporter or Reporter()
    screenless = {record["n"] for record in machines if record["scr"]}

    if cross_check_ini and os.path.isfile(cross_check_ini):
        parser = configparser.ConfigParser(allow_no_value=True)
        parser.read(cross_check_ini)
        if parser.has_section("ROOT_FOLDER"):
            listed = {name for name, _ in parser.items("ROOT_FOLDER")}
            # Entries for machines this XML does not contain say nothing about the rule.
            listed &= {record["n"] for record in machines}
            only_ini = listed - screenless
            only_derived = screenless - listed
            if only_ini or only_derived:
                reporter.warn(
                    f"derived screenless list differs from {cross_check_ini} "
                    f"({len(only_ini)} only in the ini, {len(only_derived)} only derived). "
                    f"Using the derived list, which always matches this XML.")
            else:
                reporter.info(f"Derived screenless list matches "
                              f"{os.path.basename(cross_check_ini)} exactly "
                              f"({len(screenless)} machines).")
    return screenless


# What each filter is called when the page shows why a machine did not make it. The
# order is the order they are applied in, which is the order the tally reads best in.
REASONS = (
    ("blacklisted", "On your exclude list"),
    ("screenless", "No screen at all"),
    ("not-a-game", "BIOS, device, mechanical or not runnable"),
    ("proto-beta", "Prototype or beta"),
    ("beta-bootleg", "Beta bootleg"),
    ("not-working", "Driver does not work"),
)


def filter_machines(machines, screenless_names, config, tally=None, rejects=None):
    """Apply the romset filters to raw machine records, returning the keeper dict.

    `tally` counts why each machine was dropped and `rejects` collects them. 4,323 of
    0.289's 16,350 machines never reach the library, plus whatever the user has
    excluded by name -- and the page used to show only the two totals, so the honest
    question "where did the rest go" had no answer anywhere in the app.

    Collecting them matters more than counting them: a machine excluded by name was
    unreachable once it had been, with no way back but editing settings.ini by hand.
    """
    mame_list = {}
    blacklist = set(config.blacklist_roms)
    dropped = tally if tally is not None else {}
    current = None

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1
        if rejects is not None:
            entry = _entry(current)
            entry["reason"] = reason
            rejects[current["n"]] = entry

    for record in machines:
        name = record["n"]
        current = record
        if name in blacklist:
            drop("blacklisted")
            continue
        if name in screenless_names:
            drop("screenless")
            continue
        if is_skipped_machine(record) or not record["dr"]:
            drop("not-a-game")
            continue

        description = record["d"]
        if not description or PROTO_BETA_RE.search(description):
            drop("proto-beta")
            continue
        if record["m"] and BETA_BOOTLEG_RE.search(record["m"]):
            drop("beta-bootleg")
            continue

        # MAME derives driver `status` as the worst of emulation/colour/sound/graphics,
        # so status == "good" already implies emulation == "good" and this OR is
        # effectively just "emulation is good". Kept as-is to preserve behaviour on any
        # romset whose XML does not follow that rule.
        if record["st"] != "good" and record["em"] != "good":
            drop("not-working")
            continue

        mame_list[name] = _entry(record)

    return mame_list


def _entry(record):
    """One machine as the rest of the app wants it, with no filtering decision in it."""
    disks = record["dk"]
    machine = {
        'description': record["d"],
        'chd_req': bool(disks),
        'status_good': record["st"] == "good",
        'emulation_good': record["em"] == "good",
        # How well it runs, not just that it does. Everything here has passed the
        # working filter; these are the caveats MAME attaches to it.
        'driver_status': record.get("st") or "",
        'emulation': record.get("em") or "",
        'savestate': record.get("sv") or "",
        'features': [list(entry) for entry in (record.get("ft") or [])],
        # Carried through so a list of 12,000 machines can be read by a human:
        # a row saying only "galaga" tells nobody anything.
        'year': record.get("y", ""),
        'manufacturer': record.get("m", ""),
        'display': record.get("dp"),
        'players': record.get("pl", 0),
        'controls': record.get("ct") or [],
        'buttons': record.get("bt", 0),
        'cloneof': record.get("c"),
        'signature': record.get("sig", "")}
    if disks:
        machine['chd_folder'] = chd_destination(record)
        # A merged disk is the parent's file, under the parent's name for it: eight
        # machines in 0.289 name theirs differently (konam80a's 826aaa01 is
        # konam80s/826eaa01.chd), and looking for their own name read them as
        # missing for ever. MAME finds it in the parent's folder by its hash.
        machine['chd_disks'] = [merge or disk for disk, merge in disks]
        machine['parent'] = record["c"]
    return machine


def categorize(mame_list, catlist, config, reporter=None):
    """Give every keeper its catlist category and genre, and mark the excluded ones.

    Blacklisted machines are flagged rather than dropped so the weight of each genre can
    still be measured: deciding what to leave out of a 1.2 TB set is impossible without
    seeing what each genre actually costs.
    """
    reporter = reporter or Reporter()
    categorized = set()
    blacklist = set(config.blacklist_genres)
    excluded_categories = set(config.blacklist_categories)

    # genre.ini repeats the leading component of every catlist section, so the blacklist
    # works off catlist alone.
    # UNLISTED is a genre this tool invents for machines catlist says nothing about,
    # so it is a perfectly good thing to blacklist even though no catlist section
    # names it. Warning about it sends people looking for a typo that is not there.
    known_genres = set(sources.derive_genres(catlist)) | {UNLISTED}
    unknown = [genre for genre in config.blacklist_genres if genre not in known_genres]
    if unknown:
        # One line rather than one per genre: a trimmed-down catlist can miss a dozen.
        reporter.warn(f"{len(unknown)} blacklisted genre(s) are not in this catlist and "
                      f"were skipped: {', '.join(unknown)}")

    for section in catlist.sections():
        if section in sources.CATLIST_SKIP_SECTIONS:
            continue
        # lstrip() strips a character set, not a prefix, so it would eat any leading
        # a/r/c/d/e/: of the real category name.
        category = sources.clean_category(section)
        genre = sources.genre_of(section)
        for machine_name, _ in catlist.items(section):
            machine = mame_list.get(machine_name)
            if machine is None:
                continue
            machine["category"] = category
            machine["genre"] = genre
            machine["excluded"] = genre in blacklist or category in excluded_categories
            categorized.add(machine_name)

    for unlisted in set(mame_list) - categorized:
        mame_list[unlisted]["category"] = UNLISTED
        mame_list[unlisted]["genre"] = UNLISTED
        mame_list[unlisted]["excluded"] = (UNLISTED in blacklist
                                           or UNLISTED in excluded_categories)
    return mame_list


def collapse_clones(mame_list, reporter=None):
    """One game, one ROM: keep the parent of each family and drop its other versions.

    `dlair` ships as four revisions at 11.5 GB each and `thayers`/`thayersa` at 16.1 GB
    each -- on this set the saving runs to tens of gigabytes, and the copies are the
    same game.

    The subtlety is that a parent does not always survive the filters: it may be a
    prototype, or have no ROMs of its own. Dropping its clones too would lose the game
    entirely, so a family with no surviving parent keeps one clone -- the first by
    name, which is arbitrary but predictable.
    """
    reporter = reporter or Reporter()
    families = {}
    for name, machine in mame_list.items():
        families.setdefault(machine.get("cloneof") or name, []).append(name)

    keep = set()
    for parent, members in families.items():
        if parent in mame_list:
            keep.add(parent)
        else:
            keep.add(sorted(members)[0])

    dropped = [name for name in mame_list if name not in keep]
    for name in dropped:
        mame_list[name]["excluded"] = True
        mame_list[name]["clone_of_kept"] = True

    if dropped:
        reporter.info(f"One game one ROM: {len(dropped)} alternate version(s) set "
                      f"aside, {len(keep)} kept.")
    return mame_list


_UNSAFE = re.compile(r'[<>:"\\|?*]')
_UNSAFE_SPACES = re.compile(r"\s{2,}")


def folder_name(category, mature_folder="ZZ-Adult"):
    """(destination folder, is_mature) for a catlist category."""
    is_mature = sources.MATURE_MARKER in category
    if is_mature:
        category = category.replace(sources.MATURE_MARKER, "")

    # Dots are dropped rather than only trimmed (see commit "Folder naming fix"), and
    # the mature branch follows the same rule instead of keeping them. Splitting on the
    # spaced separator keeps a genre like "Videocassette Player/Recorder" whole; the
    # slash inside it cannot survive as a path separator, so it becomes a dash.
    #
    # Characters Windows and SMB refuse in a name go too: catlist writes the TTL
    # machines under "TTL * Ball & Paddle", and '*' is a wildcard to SMB -- a share on
    # the console would not take the folder at all.
    parts = [_UNSAFE_SPACES.sub(" ", _UNSAFE.sub("", part)).replace(".", "")
             .replace("/", "-").strip()
             for part in category.split(sources.CATEGORY_SEPARATOR)]
    parts = [part for part in parts if part]
    if is_mature:
        parts.insert(0, mature_folder)

    return "/".join(parts), is_mature


def folder_namer(config):
    """The folder_name callable the planner expects, bound to a Config."""
    def namer(category):
        return folder_name(category, config.mature_rom_folder)
    return namer
