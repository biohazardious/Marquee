# What was built, and what was measured and rejected

Written as a working note, not a wish list: each entry says what it was worth, what it
cost, and what it depended on. Ordered by value per unit of work at the time.

Entries 1-11 are closed. They are kept as the record of *why* each one was built the
way it was -- and, for the one that was rejected, why it was not. The open entries
(12 onwards) came from outside suggestions on 2026-09-24; each was checked against the
code and, where it could be, the real set before it went in.

Measured against the live set (MAME 0.289, 11,990 machines, 332 GB).

---

## 1. gamelist.xml — **done**

EmulationStation shows a folder of `mslug.zip` as "mslug". With a `gamelist.xml` it
shows *Metal Slug - Super Vehicle-001*, 1996, Nazca, 2 players, Platform, with the
title screen beside it.

Every field it wants is already in hand — description, year, manufacturer, genre,
players, and the artwork cache the Posters button fills. Nothing to fetch, nothing to
scrape, no new failure mode. It is the difference between "files on a console" and a
library somebody wants to browse.

**Cost:** small. One writer, one setting, one pass at the end of a transfer.

*Built.* One list at the root of the library rather than one per genre folder: that
is how EmulationStation reads a system, and it is also the only arrangement that
cleans up after itself -- a gamelist dropped into every folder keeps those folders
alive after the last game moves out, because nothing may prune a folder that is not
empty.

## 2. One game, one ROM — **done**

`dlair` ships as four revisions at 11.5 GB each; `thayers`/`thayersa` are 16.1 GB
each. The set is full of families where every member is the same game.

Keeping the parent and dropping its clones is the standard romset preference and it
is worth tens of gigabytes here. The rule has one subtlety: a clone whose parent did
not survive the filters must be kept, or the game disappears entirely.

**Cost:** small, and it interacts with everything downstream — do it before the
gamelist so the gamelist describes what was actually copied.

*Built.* Measured on the real set: 11,990 machines and 332 GB become 4,452 and
251.8 GB. **80.2 GB, 24.1%.** Off by default; it is a taste, not a correction.

## 3. Fetch what is missing — **done**

The Wanted page prices the gap; the acquisition stack underneath it is written and
tested. What is missing is the button.

The one hazard is real: the user's client holds 1.3 TB of finished MAME torrents, and
`select_only` would drop every unlisted file to priority 0 and stop them seeding.
`include()` exists for exactly this and only ever raises priorities.

**Cost:** medium. A job phase, a confirmation that states the cost, and progress.

*Built.* Two steps on purpose: "what would it cost" names the release and the bytes
before anything is committed. `include()` throughout, so nothing is ever deselected.

## 4. Version upgrades in the UI — **done**

The thesis of the project: 0.282 → 0.289 is 4.46 GB rather than 163 GB, because only
244 machines changed and 877 are new. `acquire.delta()` and the content signatures are
done and tested; what is missing is choosing a target release in the UI, showing the
delta, and running it.

**Cost:** medium. Needs two versions' catalogues in hand at once.

*Built.* On the Wanted page: pick a release, see how many machines are already the
right bytes and how many moved, then hand the delta to the same fetch path.

## 4b. Better artwork coverage — **measured, not worth building**

libretro-thumbnails has a title screen for about 60% of the set. A fallback chain
through the other kinds looked obvious, so it was measured on 60 machines that have no
title screen: **snaps cover 2% of them, box art 0%, logos 12%** -- and a logo is not a
screenshot. The gap is not a gap in our lookup, it is a gap in the source. Left alone.

## 5. Watch for new releases — **done**

Pleasuredome publishes roughly monthly. Now that (4) can act on it, a daily check and
a "0.290 is out" banner is worth having: the point of the project is that the answer
to a new release is a few gigabytes, not an afternoon.

**Cost:** small. One timer, one comparison against the recorded version, one banner.

*Built.* The listing is refreshed when it is more than six hours old, compared against
what the library's own record says it holds, and a line appears on every page with a
link straight to the cost of moving. Versions compare as numbers -- "0.9" sorts after
"0.289" as a string and is not a newer MAME.

## 6. Is the library still the release it claims to be? — **done**

The one thing a filename cannot answer. MAME replaces bad dumps and renames chips
between releases, so `strider2.zip` from 0.252 and from 0.289 are the same name and
different bytes; a library that decides "we have it" from the name carries those
differences for ever, which is the difference between a copy of a romset and one that
is kept up to date.

**Cost:** small, because a zip's central directory lists every entry's name and CRC-32
with no unpacking — measured at 8.7 ms a game, so a 10,000-game library is read in
under two minutes with no network.

*Built.* Each game comes back current, out of date, missing an inherited ROM, damaged
or absent, with the offending ROM and both CRCs on the row. A real 0.252-era library of
9,904 games against 0.289: **9,350 current, 386 out of date, 168 missing an inherited
ROM** — 5.8 GB to bring up to date against 250 GB to fetch the set again. What the
check finds outranks what the file sizes say, so a redump that weighs the same as the
dump it replaced is still replaced.

## 7. What a run will do, before it does it — **done**

Every figure was already computed — the sync report is what the copy runs from — and
none of it was on screen. A run that copies, replaces, renames and, if asked, deletes
should be readable beforehand rather than afterwards in a log.

**Cost:** small; a page over a report that already existed.

*Built.* Five rows — copy over, replace, move, delete, leave alone — each opening into
the games and files it means, with a reason on every deletion candidate. Building it
turned up the thing the diff had been getting wrong: a machine's **path** changes
between releases as well as its contents (catlist renamed `Casino` to `Gambling` in
0.289; a clone with a disk of its own leaves its parent's folder), and matching on the
exact path alone turned every one of those renames into a delete and a re-download. On
the live library that was **954 files and 23.7 GB** relocated instead of fetched again,
and 939 games that stopped counting as missing.

## 9. A QA pass over the whole thing — **done, 2026-09-16**

Four read-only reviews (engine, backends, web layer, front end) and then every
finding checked by hand. Sixty-odd findings, twenty-one of them real enough to log
(`.wolf/buglog.json` #59-79). The ones that mattered:

- A stale zip of the *same size* was reported as updated and left in place: every
  backend's same-size skip ran before the diff's UPDATE could. `replace=True` now.
- Only `smb://` counted as remote: an `ftp://` library wrote its manifest into a local
  folder named after the URL, password included.
- `gamelist.xml` was rebuilt from the source folder alone, so a partial torrent folder
  struck every already-present game off the console's list on every run.
- `narrow()` was applied to any torrent in the client, including the user's own
  half-finished download of the whole set (see D12).
- SMB's `storeFile` was passed `show_progress=True` by position; the test fake
  accepted any arity, so a green suite hid it.
- Two overlapping Start transfer requests both ran, into one destination.
- `set(needed)` rebuilt per machine: twenty seconds per Transfer page load.
- Settings showed pre-save values after a save; "Put back" was undone by the next
  Build plan; deleting orphans took one click and persisted across runs.

Every persistent write now goes through `marquee/atomic.py`. Test count 885 → 911.

## 10. Games the console's MAME cannot run — **done, 2026-09-23**

Batocera ships its own MAME, a few releases behind the set: 0.285 under a 0.289
library. Older full sets are not published, so they cannot be matched. Measured on the
real library: 714 of 10,022 games are machines 0.285 does not have, 67 are ones whose
0.289 zip lacks a ROM 0.285 still wants, and the other 9,241 run as they are (MAME
finds a ROM by CRC, so a relabelled chip is no obstacle). With *The console's MAME* set,
the first kind is filed under `ZZ-Version-Mismatch/`, the second under `ZZ-Missing-ROM/`,
genre and category kept inside; both names are settings. Nothing is deleted, and the
day the console catches up the setting changes and they move back by rename.

## 11. An ignore list — **done, 2026-09-23**

The library root on a Batocera box holds files Marquee did not put there and must never
touch: the BIOS zips a BIOS pack drops in (`neogeo.zip`, `pgm.zip`, `cpzn1.zip`...) for
the older libretro cores that want split sets. They showed up among "files nothing
wants", one tick away from deletion. `ignore_paths` (Settings, or "Ignore" / "Leave them
alone" on the Transfer page's delete list) takes them out of the diff altogether: never
deleted, never moved from, never counted. Patterns start at the library's top and `*`
never crosses a folder, so `*.zip` is exactly the top-level zips. A path the selection
itself wants stays Marquee's whatever the list says. The Transfer page applies it to the
plan in hand; nothing has to be listed again.

## 12. Space nobody wants any more — **open**

Reclaim frees a Marquee torrent once every file it holds is in the library. It has no
answer for a file that was fetched and then *stopped being wanted* -- a genre
unticked, a game excluded, one game one ROM turned on. Such a file is on disk, is not
in the library, and never will be, so Reclaim reports the torrent as blocked with
"not in the library yet. Transfer them first", which a Transfer can never satisfy.
The torrent stays blocked for ever and its space is invisible.

What to build: a third verdict beside "in the library" and "still arriving" --
*nobody wants it* -- with the files and bytes listed, so the page shows the space the
selection has left behind. Removing it is still the whole torrent (single files share
pieces with their neighbours; see `web/reclaim.py`), still priced first, and still a
second request naming the torrents; the warning changes from "the library keeps its
copies" to "these files exist nowhere else". Show each torrent's ratio and seeding
time on the same row -- the client already reports them -- so the decision to stop
seeding is made with the numbers in view.

To measure first: whether a torrent from an older release, left behind by an upgrade,
lands here or somewhere else.

**Cost:** small to medium. One more set in `reclaim_payload`, one more state on the page.

## 13. A preferred region for one game one ROM — **open**

One game one ROM keeps each family's parent (and, when the parent does not survive the
filters, the first clone by name). The parent is whichever set MAME's developers made
the parent, and that is often not the one an English-speaking player wants. On 0.289,
of 5,097 playable families 667 have a Japanese parent, and in **120 families the
parent is neither World nor US while a US clone exists**.

What to build: an ordered region list (e.g. World, US, Europe, Japan) consulted only
when one game one ROM is on. Within a family, the first surviving member whose
description names the best-ranked region wins; with no match, today's rule stands.
The region comes from the parenthesised part of `<description>` ("(US, set 1)",
"(World, rev A)"). Bootlegs, hacks and prototypes never win over a proper set on
region alone.

The things to get right: the chosen clone must pass every filter the parent would have
(working status, genre, blacklists); non-merged zips make any member self-contained,
but a clone's disk lives in the parent's folder (see the CHD layout note), so a family
with CHDs needs its disk found by name; and the gamelist, the Wanted price and the
upgrade preview must all describe the member actually kept. Off by default, like one
game one ROM itself.

**Cost:** medium. Most of it is tests against real families.

## 14. How long until it is on the console — **open, small**

The Activity page already shows each torrent's speed and ETA from the client. What
nothing says is how long a fetch *will* take before it is started: "what it would
cost" gives bytes only. Add a time to that confirmation, from the rate Marquee
observed on its last download, and say which rate it used. Before anything has
downloaded there is no honest rate, and the line is left out rather than guessed.

**Cost:** small. Record the last observed rate; one sentence on the confirmation.

## 15. A second download client — **open, only when someone asks for it**

`marquee/download/__init__.py` has a `DownloadClient` base, but `QBittorrent` does not
inherit from it and the pipeline now depends on far more than it lists: `include()`,
`narrow()`, `delete()`, `piece_hashes()`, categories, `recent_errors()`. Before any
second client, the base class has to say what is actually called, and a fake client
in the tests has to satisfy it -- that is worth doing on its own, because today
nothing stops the interface drifting further.

Transmission's RPC and Deluge's both select files and set priorities, so either could
be written against that interface. The rule a new client must keep is the one
qBittorrent's does: it only ever *raises* file priorities on a torrent it did not
fetch, so nothing somebody else is seeding is ever deselected.

**Cost:** small for the interface, medium per client. Unraid users are the likely
askers; until one does, only the interface is worth the time.

## Already there

- **A health endpoint.** `/api/health` answers without a key (`web/server.py`), and
  both the Dockerfile's `HEALTHCHECK` and the TrueNAS catalog template use it. A hung
  mount under a probe shows as a timeout, which is the unhealthy signal it should be.
  Probing the library itself there would put SMB traffic on a 30-second timer.

## 8. A database — only when something needs it

The design note argues for SQLite. Nothing yet does: the plan is rebuilt from the XML
in 0.3 s and the parse cache makes that cheap. Adding a schema now would be structure
without a problem to solve. Revisit when the queue and history outlive a process.

---

## Deliberately not doing

- **Software List sets.** A separate library with its own layout and its own 1 TB.
  Indexed so they are visible; out of scope for the arcade library.
- **BIOS and device sets.** Pleasuredome's non-merged zips already embed them, which
  is what makes per-game copying work at all.
- **Stopping files at a seeding quota.** Ratio and seeding time belong to a torrent,
  not to a file, and a Marquee torrent is a whole release with only some of its files
  selected, so a file cannot "meet its quota". qBittorrent already enforces share
  limits per torrent and per category, and can pause or remove at the limit. The part
  worth having -- ratio and seeding time beside the button that stops seeding -- is in
  (12).
- **Rebuilding or repairing ROM sets.** Marquee reads a zip to answer "is this still
  the release we think it is" (6), and the download client hash-checks what it fetches.
  Merging, splitting and fixing a bad set is a rebuilder's job and it does it better
  than we would.
