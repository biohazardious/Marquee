"""Getting the torrent folder's space back once the library has the files.

The deliberate counterpart to `narrow()`. Narrowing only ever stops files arriving;
this is the one act in Marquee that takes finished data away, so it is fenced in:

- only torrents in Marquee's own category -- never somebody's own download of the
  set, which may be half-way and is theirs to manage;
- only when every file the torrent has data for is already in the library, at the
  size the release says, according to a plan compared against the library;
- the whole torrent, removed with its files by the download client. Taking single
  files out from under a torrent leaves the pieces they share with their neighbours
  unreadable, and the client then reports the torrent as broken;
- and priced first: what it frees, what stops seeding, and whether the library's
  copies are hardlinks -- in which case it frees nothing and says so.

Nothing is removed without a second request naming the torrents.
"""
import os

from .. import acquisition, backends, sync
from ..errors import MarqueeError
from ..plan import human_bytes


class ReclaimMixin:
    """Part of `Application`; see marquee.web.application."""

    def reclaim_payload(self, _body=None):
        """What removing each of Marquee's finished torrents would free, and why not."""
        config = self.current_config()
        plan = self.job.plan
        if plan is None or plan.sync is None:
            raise MarqueeError("Build a plan first: it is what says which files the "
                               "library already has.")
        # Files the library holds, as they were found in the source folder: a KEEP
        # with a source is "this torrent file is already there, at this size".
        in_library = {}
        for action in plan.sync.actions:
            if action.kind == sync.KEEP and action.source:
                in_library[os.path.normpath(action.source)] = action.relpath
        library_local = config.copy_path and not backends.is_remote(config.copy_path)

        torrents = []
        for entry, named in self._torrent_files(config):
            if entry.get("category") != acquisition.CATEGORY:
                continue
            # What the torrent really holds as files: the ones selected, and any left
            # complete. A skipped file shows a little progress -- the pieces it shares
            # with a wanted neighbour -- but that data lives in the client's part
            # file, not in the file, and it was never going to the library.
            # Even one the client calls complete: with every piece shared, it reads 100%
            # and is still not on disk. A deselected file counts only if it is there.
            on_disk = [(path, record) for path, record in named
                       if float(record.get("progress", 0) or 0) > 0
                       and (record.get("priority", 1) > 0 or os.path.isfile(path))]
            missing = [path for path, _record in on_disk if path not in in_library]
            unfinished = [path for path, record in on_disk
                          if float(record.get("progress", 0) or 0) < 1]
            freed, linked = 0, 0
            for path, record in on_disk:
                relpath = in_library.get(path)
                if library_local and relpath and _same_file(
                        path, os.path.join(config.copy_path, relpath)):
                    linked += 1
                else:
                    freed += int(record.get("size", 0) * float(record.get("progress", 0) or 0))
            if unfinished:
                blocked = (f"{len(unfinished):,} of its files are still arriving; "
                           f"let it finish, plan, and transfer first.")
            elif missing:
                blocked = (f"{len(missing):,} of its {len(on_disk):,} files are not in the "
                           f"library yet. Transfer them first \u2014 removing the torrent "
                           f"would delete the only copy.")
            elif not on_disk:
                blocked = "It holds no data."
            else:
                blocked = None
            torrents.append({
                "hash": entry["hash"], "name": entry.get("name") or entry["hash"],
                "files": len(on_disk), "linked": linked,
                "freed": freed, "freed_human": human_bytes(freed),
                "blocked": blocked, "reclaimable": blocked is None,
            })
        ready = [t for t in torrents if t["reclaimable"]]
        total = sum(t["freed"] for t in ready)
        return {"torrents": torrents, "freed": total, "freed_human": human_bytes(total),
                "note": ("Removing a torrent stops it seeding. The library keeps its "
                         "copies; the download client deletes the torrent's own files.")}

    def reclaim(self, body):
        """Remove the named torrents with their files -- only those the price allowed."""
        wanted = set((body or {}).get("hashes") or [])
        if not wanted:
            raise MarqueeError("Name the torrents to remove; nothing is removed by default.")
        # Worked out again now, not trusted from the page: a file could have been
        # deleted from the library since it was priced.
        price = self.reclaim_payload()
        allowed = {t["hash"]: t for t in price["torrents"] if t["reclaimable"]}
        refused = sorted(wanted - set(allowed))
        if refused:
            raise MarqueeError(f"{len(refused)} of those cannot be removed now; ask for "
                               f"the price again to see why.")
        client = self.client()
        done = []
        for infohash in sorted(wanted):
            client.delete(infohash, delete_files=True)
            done.append(allowed[infohash])
        self._queue_cache = {"at": 0.0, "data": None}
        freed = sum(t["freed"] for t in done)
        return {"removed": len(done), "freed": freed, "freed_human": human_bytes(freed),
                "replan": True}


def _same_file(one, other):
    """Whether two paths are one file on disk -- a hardlink frees nothing."""
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False
