# Marquee — architecture

A MAME library manager in the shape of Sonarr/Radarr: you declare what you want,
it acquires only that, keeps it categorised, and moves it forward a version at a
time without re-downloading the world.

This document records the design and the decisions behind it. Every number in it
was measured against the real Pleasuredome sets and a real qBittorrent on the LAN,
on 2026-09-14 — not estimated.

---

## 1. The problem, in numbers

Pleasuredome publishes MAME as a handful of enormous torrents:

| Set | Files | Size |
|---|---:|---:|
| MAME 0.289 ROMs (non-merged) | 44,166 | 163.2 GB |
| MAME 0.288 CHDs (merged) | 1,042 | 1.127 TB |
| **Total** | | **1.290 TB** |

Of the 16,350 machines in 0.289, only 12,023 survive the "working, not a BIOS,
not a device, not mechanical, not a prototype" filter, and 11,995 survive a
sensible genre blacklist. That selection is:

| | Sum of files | Actual download | Saving |
|---|---:|---:|---:|
| ROMs | 45.4 GB | **51.1 GB** | 68.7% |
| CHDs | 290.9 GB | **292.5 GB** | 74.0% |
| **Total** | 336.3 GB | **343.5 GB** | **73.4%** |

"Actual download" is larger than "sum of files" because BitTorrent's unit is the
piece, not the file: a 4 MiB piece straddling a wanted and an unwanted zip must be
fetched whole. The overhead is 12.5% on ROMs (4 MiB pieces, small files) and 0.7%
on CHDs (16 MiB pieces, huge files). It is computed exactly from each file's
`piece_range`, and qBittorrent reports the same figure as the torrent's `size`
once the selection is applied.

**Upgrading is cheaper still.** Going from a 0.282 library to 0.289 — seven
releases — changes only 244 of the 11,995 wanted machines and adds 877 new ones:

| | |
|---|---:|
| Files to fetch | 1,119 |
| Sum of those files | 2.85 GB |
| **Actual download** | **4.46 GB** |
| Full set for comparison | 163.2 GB |
| Saving | **97.3%** |

That is the product.

---

## 2. Why this works

Three properties of the Pleasuredome sets make it possible, all of them stated by
Pleasuredome themselves or verified here:

1. **Non-merged zips are self-contained.** PD's non-merged set embeds BIOS and
   device ROMs inside every game zip — unlike stock MAME non-merged. Their own
   page: *"just copy galaga.zip and you are set… Pick individual games from this
   set if you are not intending to download a complete ROM set."* One file per
   machine, flat, named `<machine>.zip`. This is why the user's instruction to use
   only the non-merged full set is the right one.

2. **Every zip is TorrentZipped.** Zip metadata is normalised, so identical ROM
   content produces a byte-identical file across MAME versions. An unchanged game
   is literally the same bytes in 0.282 and 0.289 — which is what makes upgrades
   a delta rather than a re-download.

3. **libtorrent addresses data as (file, offset), not as one flat stream.** A
   piece is valid if every file it overlaps matches. Adding or removing files in a
   later version therefore does *not* invalidate everything after it. PD rely on
   this too: *"If you already have a previous set, simply join the new set by
   pointing at the content of your previous set."*

CHDs are laid out `<machine>/<disk>.chd` in a merged set, so a clone's disk lives
in the parent's folder. Resolution must be by **file name with a parent-folder
fallback**, never by directory — doing it by directory reports every clone as
missing. With the fallback, 309 of 317 needed CHDs resolve; the 8 that do not are
genuinely absent because the CHD set is still at 0.288 while ROMs are at 0.289.
That lag is normal and the UI must show it rather than hide it.

---

## 3. Shape of the system

```
                    ┌──────────────────────────────────────────┐
                    │               Marquee                    │
                    │  (single container, :8585)               │
                    └──────────────────────────────────────────┘
                                      │
   ┌──────────────┬───────────────────┼───────────────────┬──────────────┐
   │              │                   │                   │              │
┌──▼───────┐ ┌────▼──────┐  ┌─────────▼────────┐ ┌────────▼──────┐ ┌─────▼──────┐
│ Catalogue│ │ Indexer   │  │ Download client  │ │ Import        │ │ Library    │
│          │ │           │  │                  │ │               │ │            │
│ MAME XML │ │Pleasure-  │  │ qBittorrent      │ │ hardlink/copy │ │ local/SMB/ │
│ + catlist│ │dome page  │  │ Web API v2       │ │ + categorise  │ │ FTP/SFTP   │
│ per      │ │→ infohash │  │ add stopped,     │ │ + rename      │ │            │
│ version  │ │+ datfiles │  │ set file prio,   │ │               │ │ Batocera   │
└──────────┘ └───────────┘  │ start, watch     │ └───────────────┘ └────────────┘
                            └──────────────────┘
```

The mapping onto Sonarr's mental model, which is the one the user asked for:

| Sonarr | Marquee |
|---|---|
| Series you monitor | **Profile** — genre/category/machine selection rules |
| Indexer | **Pleasuredome** release index |
| Download client | **qBittorrent** (per-file priority is the whole trick) |
| Import / rename | **Import** into the categorised library |
| Quality upgrade | **Version upgrade** by content-signature delta |
| Activity / History | same |

---

## 4. Decisions

### D1 — Two tiers: torrent folder and library. Never one folder.

The torrent folder holds the PD layout (`MAME 0.289 ROMs (non-merged)/galaga.zip`)
and keeps seeding. The library holds the categorised tree
(`Maze/galaga.zip`). They are different shapes, so they cannot be the same
directory.

When both are on one filesystem the library can be built with **hardlinks**: the
categorised copy costs no extra bytes and seeding continues undisturbed. 343 GB
total rather than 686 GB. When the library is on another host it is copied over
SMB/FTP/SFTP and the torrent folder stays behind as the seeding cache.

Hardlinking is **off by default and opt-in**, because a hardlinked file is the same
file: anything that writes to a library ROM writes through to the torrent folder and
breaks the set being seeded. The tool itself never does that -- it unlinks a
destination before replacing it, so a changed ROM can never write through -- but an
outside program still could.

The library is *derived state*. The torrent folder is the source of truth. That
is what makes every filter decision reversible.

### D2 — Selection happens before the download, not after.

Add the torrent **stopped**, set every file to priority 0, set the wanted files to
priority 1, then start. Verified against the live client: a 225,000-byte, 6-file
torrent reported `size: 98,304` after selecting 2 files — qBittorrent computes the
piece-inflated cost itself, so the UI can show the true number without doing the
piece arithmetic.

### D3 — Upgrades come from content signatures, not from PD's update packs.

PD's update torrents only bridge consecutive releases (0.288 → 0.289). Going
0.282 → 0.289 would mean chaining seven of them, and each one carries every
machine, including the 32,000 the user does not want.

Instead: for each machine compute a signature over its complete non-merged
content — its own ROMs, plus its BIOS's ROMs when `romof` points at a BIOS, plus
every `device_ref`'d device's ROMs, recursively, keyed by SHA-1. Diff the
signature at the library's version against the target version. Machines whose
signature is unchanged are already correct on disk and are skipped. This handles
any version jump, forwards or backwards, and only ever considers machines the
profile actually wants.

The delta is then fetched from the **target version's full-set torrent**, pointed
at the existing torrent folder, with only the changed and new files at priority 1.

### D4 — Non-merged ROMs, merged CHDs. Nothing else.

Split and merged ROM sets require parent/BIOS/device resolution at copy time and
break per-game extraction. CHDs are only published merged. Software List sets are
out of scope for now (a separate library with its own layout).

### D5 — SQLite in `/config`, not JSON files.

Per-version machine catalogues (16k rows), torrent file indexes (44k rows),
library state, queue and history, with relational queries across them. JSON blobs
stop being workable at this size. `sqlite3` is stdlib, so this adds no dependency.

The destination keeps its `.marquee.json` manifest as well — that file is what
lets a library be picked up by a fresh install, and it stays.

### D6 — Remote path mapping is mandatory, not optional.

qBittorrent reports `save_path: /downloads` as seen from inside *its* container.
Marquee sees the same data at some other path. Sonarr solves this with Remote
Path Mappings and so does this: a list of (download-client path → local path)
rewrites. Without it, import silently finds nothing.

### D7 — No frontend build step.

The UI is hand-written ES modules and CSS served static. `pip install` then run;
no node, no bundler, no lockfile drift. The Sonarr look — dark shell, left
sidebar, dense tables, a live Activity page — is CSS and markup, not a framework.

### D8 — Reclaiming space is explicit.

Dropping a genre unlinks it from the library immediately. The bytes are still in
the torrent folder, still seeding. Actually freeing them means deleting files the
torrent wants, which stops you seeding them and shows the torrent as incomplete —
so it is a separate, clearly-labelled action, never a side effect of a filter
change.

---

### D9 — The API key guards the data, not the page.

A container necessarily binds `0.0.0.0`, so a key is always in force. Guarding the
shell as well made the UI impossible to open at all: the browser fetches the
stylesheet and the ES modules itself, and a `<script src>` carries no query string.
So `/`, `/static/*` and `/api/health` are public -- they are markup with no data in
them -- and `/api/` answers **401** without a key. The page keeps the key in
`localStorage` and sends it as `X-Token`.

### D10 — Artwork is derived, not scraped.

libretro-thumbnails names its files after MAME's own `<description>`, so the URL for a
machine's title screen is a pure function of data already in hand. `/art/<kind>/<name>.png`
serves the local cache and **redirects to the source on a miss**, which means an
uncached page is exactly as fast as it was before and gets faster as the cache fills.

Coverage is about 60% and that is the ceiling: of the machines with no title screen,
2% have an in-game snap, 0% box art and 12% a logo. The gap is in the source, not in
the lookup, so there is no fallback chain.

### D11 — One gamelist, at the root of the library.

EmulationStation reads a system from one `gamelist.xml` whose paths include the
subfolders. Writing one into every genre folder would also work -- and would keep
every genre folder alive forever, because a folder holding a gamelist is not empty and
nothing may prune it. One file at the root is both the convention and the only
arrangement that cleans up after itself.

### D12 — Never deselect a file that is already here.

The download client may already hold a release torrent, finished and seeding.
`select_only` drops every file it was not asked about to priority 0, which stops it
seeding them; on a 1.3 TB set that is not cheaply recoverable.

The first answer was `include()`, which only ever raises a priority. That is safe and
it is also wrong, because it is only half the problem. qBittorrent hands a *newly
added* torrent every one of its 44,166 files at normal priority — so `include()` finds
nothing to raise, and starting it fetches the whole set. Both of those happened, in
that order, on a real client: a dry run added the set to read its file table, the
commit that followed decided it was somebody else's torrent, and 275 MB of files
nobody asked for arrived before it was stopped.

Choosing between the two calls on *who added the torrent* was the mistake. The thing
that actually matters is whether a file exists yet:

`narrow(indices)` skips every file that is neither wanted nor already complete, and
touches nothing else. On a set with nothing downloaded that is exactly `select_only`.
On a finished set it is exactly `include`. Nothing being seeded can be stopped by it.
Reclaiming space stays a separate, clearly-labelled action.

One more line was needed, and it took a review to see it. `narrow` is safe for a
torrent that is finished, and safe for one that has nothing -- but a torrent the user
is fetching *in full*, half-way through, is neither: every file they have not got yet
is incomplete, and `narrow` drops all of them to 0. So the rule is about ownership,
and ownership is the qBittorrent category. A torrent filed under this tool's own
category -- added by it now, or on an earlier run -- is ours to `narrow`. Anything
else in the client is the user's, and is only ever `include()`d; the answer says so
(`shared: true`), because "asked for 1,786 files" means something different when
nothing was deselected to make room for them.

A corollary: a release added only to read its file table is left holding its single
smallest file. It is stopped, so it does nothing on its own — but a torrent sitting in
somebody's client with 1.3 TB selected is a loaded gun, and qBittorrent will not accept
one with nothing selected at all.

### D13 — The catalogue is the whole release, not what is on disk.

A library page built from the files present shows an empty screen on a fresh install,
which is precisely when someone needs to see what is available. So the plan carries
`absent_items` beside `items` — every machine the selection asks for, whether or not a
byte of it is here — and the page lists both, marked. `items` is untouched, so copying
and syncing still work from what can actually be copied today.

Sizes for what is absent come from the release torrent's own metadata: added stopped
with `stopCondition=MetadataReceived`, the file table read, no content fetched. 44,166
names and sizes, cached in `/config/cache/marquee/catalogue.json` so a restart does
not pay for it again. That turns "1,523 Fighter games" into "1,523 Fighter games,
22.3 GB", which is a figure someone can decide on.

### D14 — Where downloads go is the client's business.

Marquee sets a category and reads `content_path` back. It does not hand qBittorrent a
save path, because the path this process sees is not necessarily the path the client
writes to — handing over ours scatters the set somewhere nobody meant. This is how
Sonarr and Radarr work, and for the same reason.

What does have to cross back is where the files landed, remapped into our terms: the
library is built from them, and on a fresh install nobody knows the folder's name in
advance. The download drawer shows it, offers to point the ROM folder at it, and warns
when the path is not visible from here — which is a remote path mapping, not a
mystery. Planning also descends one level on its own: a folder with no zips in it but
a subfolder full of them is a torrent folder, and that is what was meant.

---

## 5. Storage layout

```
/config                     marquee.db, logs, cache/{xml,catlist,dat}
/downloads                  torrent folder (must match qBittorrent's view,
                            or be reachable through a remote path mapping)
/library                    categorised output, when local
```

`/library` may instead be `smb://host/share/roms/mame`, `ftp://…` or `sftp://…`,
in which case nothing is mounted and the backend does the transfer.

---

## 6. What is built

All of it except the database, which nothing has needed yet -- see
[ROADMAP.md](ROADMAP.md) for why, and for what is worth building next.

| | |
|---|---|
| Indexer | `indexers/pleasuredome.py` — 17 sets, magnets, datfiles |
| Download client | `download/qbittorrent.py` — per-file selection, `include()` |
| Selection → files | `acquire.py` — torrent indices and exact piece cost |
| Acquisition | `acquisition.py` — plan, start, remote path mapping |
| Signatures | `sources.py` — the closure of a non-merged zip, per machine |
| Library | `catalog.py`, `plan.py`, `sync.py`, `manifest.py` |
| One game one ROM | `catalog.collapse_clones` — 80 GB on the real set |
| Destinations | `backends/` — local (hardlink), SMB, FTP, SFTP |
| Console files | `gamelist.py`, `art.py` |
| Web | `web/application.py` (what it can do, assembled from `settings.py`, `downloads.py`, `releases.py` and `tasks.py`), `web/job.py` (the one background job), `web/views.py` (the plan as the pages read it), `web/server.py` (how HTTP reaches it) |

---

## 7. What carries over

The existing package is not thrown away. `sources`, `catalog`, `plan`, `sync`,
`manifest`, `backends` and the selection tree are the parts that already know what
a MAME romset is, and they keep their tests. What is new is everything to the left of
the library: acquiring the bytes instead of assuming a 1.2 TB local set.

One thing the design had to learn from real data: a partly-downloaded source is the
*normal* state, not an error. Nobody pulls 44,166 files in one go. So "what does the
selection want that is not here yet" is a first-class question -- the Wanted page --
priced from the release torrent's own file table rather than guessed at.

## 8. A note on the name

It was briefly called Arcadarr, after the Sonarr/Radarr family. The shape of the app
is still theirs -- declare what you want, let something else fetch it, keep the
library tidy -- but the name is not. A marquee is the lit sign above an arcade
cabinet: the thing that tells you what is inside.
