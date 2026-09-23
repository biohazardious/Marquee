"""Comparing a plan against what is already at the destination.

A plain copy that skips same-sized files is fine the first time and wrong afterwards.
Upgrade a romset and machines get renamed, recategorised or dropped: the old files stay
behind in folders the plan no longer mentions, and a machine that merely moved category
gets re-copied byte for byte instead of renamed. This works out the difference so a run
can do the small thing.
"""
import os
import posixpath
from dataclasses import dataclass, field

from . import verify

# What a run can do with one file.
NEW = "new"          # not there at all
UPDATE = "update"    # there, wrong size
MOVE = "move"        # the same file, in a folder the plan no longer uses
KEEP = "keep"        # already correct
ORPHAN = "orphan"    # at the destination, not in the plan


@dataclass
class Action:
    kind: str
    relpath: str
    source: str = None
    from_relpath: str = None
    size: int = 0
    # The machine that wants this file, where one does. Orphans have none -- that is
    # what makes them orphans -- and it is what lets a run be described as what
    # happens to a game rather than as four thousand paths.
    machine: str = None
    # For UPDATE: the size of the file the library holds now, which `size` is not --
    # that is the file about to replace it. A check's verdict is about this one.
    held: int = None


@dataclass
class SyncReport:
    actions: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    bytes: dict = field(default_factory=dict)
    # Partial copies a killed run left at the destination; the next transfer removes
    # them. Found by the index, never by the plan's own reckoning.
    leftovers: list = field(default_factory=list)
    # Folders at the destination with nothing in them at all, deepest first. The next
    # transfer removes them. Found by the index, like the leftovers.
    empty_folders: list = field(default_factory=list)
    # Free space behind the destination when it was read, where the protocol can say.
    # A share cannot be measured with disk_usage from here.
    free_bytes: int = None

    def of(self, kind):
        return [action for action in self.actions if action.kind == kind]

    @property
    def to_transfer(self):
        return self.bytes.get(NEW, 0) + self.bytes.get(UPDATE, 0)

    @property
    def at_destination(self):
        """What the library weighs once the run is done: everything but the orphans."""
        return sum(self.bytes.get(kind, 0) for kind in (NEW, UPDATE, MOVE, KEEP))

    def machines(self, kind):
        """The machines with at least one file of this kind, in plan order.

        The membership test is a set, not the list being built: on a ten thousand
        machine library the obvious version is a hundred million string comparisons
        on every request.
        """
        seen, order = set(), []
        for action in self.actions:
            if action.kind == kind and action.machine and action.machine not in seen:
                seen.add(action.machine)
                order.append(action.machine)
        return order


# States the check found by reading the file rather than measuring it. A redump that
# weighs the same as the dump it replaces is invisible to a size comparison, so once
# something has looked inside, what it found outranks what the sizes say.
REPLACE_STATES = (verify.STALE, verify.DAMAGED)


def _owners(plan):
    """{destination relpath: the machine that wants it}.

    plan.files() yields each destination once -- two machines can name the same
    disk -- so the first one to claim a path owns it.
    """
    owners = {}
    for item in getattr(plan, "items", ()):
        for _source, relpath, _size in item.files():
            owners.setdefault(relpath, item)
    return owners


def _known_wrong(item, relpath):
    """Has something read this file and found it is not what the release says?

    The machine's own zip, which `state` speaks for, and any disk the check found
    zero-filled: a disk copied before the download finished is the right size and
    would otherwise be kept for ever.
    """
    if item is None:
        return False
    if relpath in getattr(item, "damaged_disks", ()):
        return True
    # The zip's own verdict: a damaged disk makes the machine `damaged` without saying
    # anything about the zip beside it.
    state = getattr(item, "rom_state", None) or item.state
    return state in REPLACE_STATES and relpath == f"{item.folder}/{item.name}.zip"


def _elsewhere(unclaimed, remaining, basename):
    """A file of that name still sitting somewhere else at the destination.

    Only a name, with no size to check it against: nothing here holds the file the
    release says should be there, so there is nothing to weigh it against. A name is
    what identifies a ROM or a disk to MAME, and the alternative -- deleting it and
    downloading the same bytes again -- is worse in every way.
    """
    candidates = unclaimed.get(basename)
    while candidates:
        candidate = candidates.pop()
        if candidate in remaining:
            return candidate
    return None


def _tally(report):
    for kind in (NEW, UPDATE, MOVE, KEEP, ORPHAN):
        matching = report.of(kind)
        report.counts[kind] = len(matching)
        report.bytes[kind] = sum(action.size for action in matching)
    return report


def blind(plan):
    """The report for a run that declined to look at the destination: everything is new.

    The backends still skip a file already there at the same size, so this copies no
    more than it must; what it cannot do is spot a move or an orphan.
    """
    report = SyncReport()
    owners = _owners(plan)
    for source, relpath, size in plan.files():
        owner = owners.get(relpath)
        report.actions.append(Action(NEW, relpath, source, size=size,
                                     machine=owner.name if owner else None))
    return _tally(report)


def compare(plan, existing):
    """Work out what to do, given {destination relpath: size} of what is already there.

    A file that is somewhere else at the destination under the same name and size is
    treated as a move: on a category rename that turns hours of copying into a handful
    of directory operations.
    """
    report = SyncReport()

    remaining = dict(existing)
    # Same basename and size, wherever it currently sits, is a move candidate.
    by_signature = {}
    for relpath, size in existing.items():
        by_signature.setdefault((posixpath.basename(relpath), size), []).append(relpath)

    owners = _owners(plan)
    # Exact matches first, all of them, and only then the moves. Two clones with a
    # disk of the same name and size under different folders used to have the first
    # one claim the second's file as "moved from" -- relocating it, then copying the
    # second's again -- because the second had not been looked at yet.
    unresolved = []
    for source, relpath, size in plan.files():
        owner = owners.get(relpath)
        machine = owner.name if owner else None
        current = remaining.pop(relpath, None)
        if current == size and not _known_wrong(owner, relpath):
            report.actions.append(Action(KEEP, relpath, source, size=size,
                                         machine=machine))
            continue
        if current is not None:
            report.actions.append(Action(UPDATE, relpath, source, size=size,
                                         machine=machine, held=current))
            continue
        unresolved.append((source, relpath, size, machine))

    for source, relpath, size, machine in unresolved:
        candidates = by_signature.get((posixpath.basename(relpath), size))
        moved_from = None
        while candidates:
            candidate = candidates.pop()
            if candidate in remaining:
                moved_from = candidate
                del remaining[candidate]
                break
        if moved_from:
            report.actions.append(
                Action(MOVE, relpath, source, from_relpath=moved_from, size=size,
                       machine=machine))
        else:
            report.actions.append(Action(NEW, relpath, source, size=size,
                                         machine=machine))

    # Whatever is left, by file name. A library built to an older layout keeps a
    # clone's disk in its parent's folder, and that is the same file under the same
    # name in the wrong place -- the one case where the size of what belongs there
    # cannot be known, because nothing has the file to weigh.
    unclaimed = {}
    for relpath in remaining:
        unclaimed.setdefault(posixpath.basename(relpath), []).append(relpath)

    # Machines the selection wants whose files are not in the source folder but are
    # already at the destination. Without this an existing library is reported as
    # thousands of orphans -- and, with "remove what the library no longer wants"
    # ticked, deleted and then downloaded again.
    #
    # A path already settled above counts as found here too. A clone shares its
    # parent's disk on purpose: the parent's KEEP had taken the path out of
    # `remaining`, so the clone looked for the same file by name, found an old copy
    # in another folder, and planned to rename it onto the file that was already
    # there -- which the console's share refused, and rightly.
    #
    # `items` are looked at as well as `absent_items`: a machine whose disk is in the
    # source folder but whose zip is not (a torrent narrowed to what was missing,
    # with the zip long since placed) is "here", so its zip never went through
    # plan.files() -- and the correct zip in the library was an orphan, planned for
    # deletion, while the same machine was listed as something to download.
    claimed = {action.relpath for action in report.actions}
    present = {action.relpath for action in report.actions
               if action.kind in (KEEP, MOVE)}
    for item in list(getattr(plan, "items", ())) + list(getattr(plan, "absent_items", ())):
        paths = list(item.wanted_paths())
        found = 0
        # Files the check read and found wrong, with nothing in the source folder to
        # replace them. They stay where they are -- a stale zip still plays -- but
        # they are not "here": counting them as settled meant a download never asked
        # for the right bytes, and "Check the library" found problems nothing fixed.
        refetch = set()
        for relpath in paths:
            if getattr(item, "romless", False) and relpath == f"{item.folder}/{item.name}.zip":
                # Nothing to have: the machine has no ROMs, and no set carries a zip.
                found += 1
                claimed.add(relpath)
                continue
            if relpath in claimed:
                found += relpath in present
                continue
            size = remaining.pop(relpath, None)
            if size is not None:
                # Already exactly where it belongs: nothing to do, which is KEEP.
                claimed.add(relpath)
                if _known_wrong(item, relpath):
                    refetch.add(relpath)
                else:
                    found += 1
                    present.add(relpath)
                report.actions.append(Action(KEEP, relpath, None, size=size,
                                             machine=item.name))
                continue
            elsewhere = _elsewhere(unclaimed, remaining,
                                   posixpath.basename(relpath))
            if elsewhere is None:
                continue
            claimed.add(relpath)
            if _known_wrong(item, relpath) or _known_wrong(item, elsewhere):
                refetch.add(relpath)
            else:
                found += 1
                present.add(relpath)
            report.actions.append(
                Action(MOVE, relpath, None, from_relpath=elsewhere,
                       size=remaining.pop(elsewhere), machine=item.name))
        item.in_library = bool(paths) and found == len(paths)
        # Settled: every file is either already in the library or on its way from the
        # source folder. What is left over is what a download would have to supply.
        item.compared = True
        item.unsettled = [relpath for relpath in paths
                          if relpath not in claimed or relpath in refetch]
        item.settled = bool(paths) and not item.unsettled

    for relpath, size in remaining.items():
        report.actions.append(Action(ORPHAN, relpath, size=size))

    return _tally(report)


def survey(paths):
    """(files, bytes) under each path, for showing what the source actually holds."""
    results = {}
    for label, path in paths.items():
        count = total = 0
        if path and os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        total += os.stat(os.path.join(root, name)).st_size
                        count += 1
                    except OSError:
                        pass
        results[label] = {"files": count, "bytes": total, "path": path}
    return results
