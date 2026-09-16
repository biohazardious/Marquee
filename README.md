# Marquee — a MAME library manager

[![Licence: GPL v3](https://img.shields.io/badge/licence-GPL--3.0-blue.svg)](LICENSE)

A MAME library manager in the shape of Sonarr/Radarr: you declare what you want, it
acquires only that, keeps it categorised, and moves it forward a version at a time
without re-downloading the world.

Pleasuredome publishes MAME as two enormous torrents — 163 GB of ROMs and 1.13 TB of
CHDs. Most of it is machines that do not work, are prototypes, are mechanical, or are
in genres you would never play. Marquee works out which files you actually want,
tells the download client to fetch only those, and builds a categorised library out of
the result.

| | Published | Fetched |
|---|---:|---:|
| MAME 0.289 ROMs (non-merged) | 163.2 GB | 51.1 GB |
| MAME 0.288 CHDs (merged) | 1.127 TB | 292.5 GB |
| **Total** | **1.290 TB** | **343.5 GB** |

Upgrading is cheaper still. Moving a 0.282 library to 0.289 — seven releases — changes
244 machines and adds 877, which is **4.46 GB** rather than 163 GB.

Those are measured figures, not estimates. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for how they are arrived at and why the approach works.

It is built for EmulationStation systems like Batocera, RecalBox and RetroPie, but works
anywhere you need a categorised MAME set.

> Marquee ships no ROMs, no disk images and no MAME data. It reads the machine list
> MAME itself publishes, the category files the MAME community maintains, and whatever
> your own download client has fetched. See [Credits](#credits-and-upstream-sources).

## Contents

[Running it](#running-it) · [The pages](#the-pages) · [Starting from nothing](#starting-from-nothing) ·
[Keeping a library current](#keeping-a-library-current) · [What it does](#what-it-does) ·
[How it works](#how-it-works) · [Command line](#command-line) · [Development](#development) ·
[Credits](#credits-and-upstream-sources) · [Licence](#licence)

## Running it

```bash
cp .env.example .env        # point CONFIG, DOWNLOADS and LIBRARY at your folders
docker compose up -d
docker compose logs         # the first line has the URL, with the API key in it
```

Open the URL it prints. The key is stored in the browser after the first visit, so
plain `http://localhost:8585/` works from then on; it also lives in `config/api_key`
if you need it again. A server bound to localhost only asks for no key at all.

Or without Docker:

```bash
pip install -e .
marquee --web                                # settings.ini beside the package
marquee --web --config ~/.config/marquee/    # or a config directory
```

`--config` takes either the settings file or the directory holding it, which is what
the container mounts at `/config`. Outside Docker the UI is on port 8777.

Requires Python 3.9 or newer. The only hard dependency is `pysmb`; `paramiko` is
needed for SFTP and is an optional extra.

## The pages

A dark web UI with nine pages, in the order the work happens:

- **Overview** — where the library stands and what to do next. The four stages
  (source folder → selection → fetch → library) as cards with the number that matters
  on each, one recommendation at the top worked out from the same figures ("2.9 GB
  ready to go into the library", "your settings moved on — rebuild the plan"), the
  health of the setup, the free space the next transfer needs, and the last runs.
  It is the page the app opens on. The topbar pill says the same thing from every
  other page, and the status bar under it says what is happening *right now* --
  "Transferring 1.1 GB of 2.9 GB · 118 MB/s · 15s left · mslug.zip", "Checking the
  library 4,210 of 10,223", "Stopped: no space left on device" -- with the download
  client beside it: unreachable and why, connected, or "Downloading 1 release · 11.9
  MB/s · 38 GB left · 55m left".
- **Library** — every machine the selection covers, downloaded or not, as a poster
  grid or a dense table. On a fresh install that is the whole catalogue: nothing is on
  disk, and the point of the page is choosing what should be.
  Title screens come from libretro-thumbnails, matched by MAME's own description, so
  there is no scraper and no lookup table; **Artwork** downloads them all so the
  page loads from disk and works offline, and the grid redraws itself when it
  finishes. Filter by genre, by sync state, or by whether a title is adult. Click a
  game for year, manufacturer, players, controls, screen type and resolution, where it
  will be filed, its files and its other revisions. Each game has its own link
  (`#library/mslug`). Filter by condition, too: everything listed passes MAME's
  working filter, but 3,229 of the 11,993 run with something imperfect about them and
  each one says what. **Download** asks the client for exactly what the filters are
  showing — it prices it first, and nothing is fetched until you say so.
- **Selection** — one tree shaped like the library on disk: genre → category → game,
  and then a branch named after your adult folder holding the same genres and
  categories underneath it. catlist marks adult categories in the category name, so
  the two halves are genuinely different categories filed in different folders, and
  each side ticks without disturbing the other. Every row is a tick box with its
  game count and size. Untick a genre, a category, or a single game; tick it again to
  put it back, then **Save selection**. The tick box decides; clicking the game's name
  opens it. The counts are what the selection asks for, not what happens to be
  downloaded, so the tree means something on a fresh install. It takes the library's
  filters too — genre, condition, adult, downloaded or not — and when one is set the
  tree becomes a flat list of what matched, with **Leave all out** / **Put all back**
  acting on the whole match rather than the page of it. Dropping every imperfectly
  emulated game is 11,993 → 8,764 games and 494.9 → 322.6 GB. Only the largest 500
  games of a category are drawn, but every tick box covers all of them.
  Unticking something that is already on the console is a deletion waiting to
  happen, so the heading says so at once -- "· 312 in the library · 4.1 GB to
  delete" -- rather than only under **Delete** on the Transfer page after a rebuild.
  Nothing goes until that page's "Also delete" box is ticked.
- **Wanted** — the games the selection asks for that are not on disk yet, grouped by
  genre and priced from the release torrent, with a button that asks the download
  client for exactly those. Also where you move the library to a newer MAME release:
  it compares the two catalogues and fetches only the machines whose bytes actually
  changed. A partly-downloaded set is the normal state of things, so this gets a page
  rather than a wall of names in a log.
- **Left out** — everything that is not in the library, in the same tree as the
  library. Six reasons: five are the working filters (a driver that does not run, a
  BIOS set, a prototype) and are there so the question has an answer; the sixth is the
  games you excluded by name, and those have a **Put back** on them. Excluding a game
  used to be a one-way door — it was dropped before anything else ran, so it appeared
  in no list, no search and no tree.
- **Transfer** — what the next run will do, before it does it: what is copied, what is
  replaced, what is merely renamed, what would be deleted and what is left alone.
  Every row opens into the games and files it means. See
  [What a transfer will do](#what-a-transfer-will-do).
- **Activity** — the running job, the download queue, and the log.
- **Settings** — media management, download client (with a Test button), indexer.
- **System** — which XML and catlist are in use, what the library on disk records,
  **Check the library**, and the breakdown of every machine the filters dropped. On
  0.289 that is 4,323 of 16,350 before your own exclusions — 2,861 with no screen at
  all, 949 whose driver does not work, 306 prototypes and betas, 206 BIOS and device
  sets.

## Starting from nothing

Nothing downloaded, an empty library folder, a qBittorrent that has never seen a MAME
torrent. That is the case the whole thing is shaped around.

1. **Settings** → point **Library** at where the games should end up, fill in the
   **Download client**, press Test. Leave the ROM and CHD folders at the folder your
   client downloads into; Marquee finds the set inside it.
2. **Build plan.** No files are needed for this — it reads MAME's own XML and
   catlist.ini for the release you chose and works out the catalogue. About 12,000
   games, filed by genre and category.
3. **Library** → filter. By genre, by search, by adult, by year — whatever narrows it
   to what you actually want.
4. **⬇ Download.** It reads the release's file table — which means adding the torrent
   to your client *stopped*, so its own metadata arrives and none of its content — and
   tells you what that selection costs. 16 Metal Slug games: 936 MB of a 152 GB set.
   Confirm, and the client fetches those files and skips the other 44,150.
5. When it finishes, **Build plan** again, look at **Transfer**, and run it. The games
   land under `Genre/Category/machine.zip`, with a `gamelist.xml` and artwork for the
   console.

Where downloads go is qBittorrent's business, not Marquee's — it is configured there,
per category. What Marquee needs is to be able to *read* them: if the client reports
`/data/torrents/...` and this app sees the same files somewhere else, set a remote
path mapping in Settings. The download drawer says where the client is putting things
and warns when that path is not visible from here.

## Keeping a library current

Three questions, three answers. This is the part a plain copy tool cannot do.

### Is what is there still the right file?

A name match says nothing. MAME rebuilds ROM sets between releases — a bad dump
replaced, a chip renamed, a mask ROM redumped — so `strider2.zip` from 0.252 and
`strider2.zip` from 0.289 are the same name and different bytes. A library that only
checks for existence carries those differences for ever, and that is the difference
between a copy of a romset and one that is kept up to date.

**Check the library**, on the System page or beside the Transfer summary,
reads every zip's central directory — entry
names and CRC-32s, no unpacking — and compares them with the release's own ROM list,
one ROM at a time. About 9 ms a game: a 10,000-game library in under two minutes, with
no network.

Each game comes back as **current**, **out of date** (a ROM is there with the wrong
contents, or one it owns outright is missing), **missing an inherited ROM** (what a
merged or split set looks like from here, and not evidence of the wrong version),
**damaged**, or **absent**. Anything out of date joins the download list, with the
reason on the row:

```
NBA Showtime NBA on NBC      2.7.u27: 4242bf14 not 44a086a1
NFL Blitz 2000 Gold Edition  494_blitz_2000.u96: missing
Total Vice (ver EBA)         93c46.7k: 25aa0bd1 not 9c34554a
```

A real 0.252-era library of 9,904 games measured against 0.289: **9,350 current, 386
out of date, 168 missing an inherited ROM** — 5.8 GB to bring up to date, against 250
GB to fetch the set again. What the check finds outranks what the file sizes say, so a
redump that happens to weigh the same as the dump it replaces is still replaced.

### What a transfer will do

The **Transfer** page is the diff between the library and the selection, in five rows,
each of which opens into the games and files it means:

| | |
|---|---|
| **Copy over** | not at the destination at all |
| **Replace** | there, but not what this release says it is |
| **Move** | the same file, in a folder this release no longer uses — **renamed, never re-copied** |
| **Delete** | at the destination, wanted by nothing — left alone unless you ask |
| **Leave alone** | already correct, and not touched |

Each kind can be read as one list or **by genre** -- the same genre → category →
game tree the Selection page uses, so a run reads as what it does to the library
("Platform: 65 games, 38 GB, most of it Run Jump") and not as 1,144 lines.

Nothing is deleted unless the box is ticked, and every candidate says *why* it is one:
*you left it out*, *not in this release*, *a disk this release does not list for it*.
"Delete 917 files" is not something anyone can agree to; that list is.

The **Move** row is what makes an upgrade cheap. A machine's *path* changes between
releases as well as its contents — catlist renamed `Casino` to `Gambling` in 0.289, and
a clone with a disk of its own moves out of its parent's folder — and those are renames,
not downloads. On a real 0.252-era library moving to 0.289 that was **954 files and
23.7 GB relocated** rather than deleted and fetched again, and 939 games that stopped
counting as missing.

### Updating a library you already have

Point **Library** at an existing romset and build a plan. What is already there and
still wanted is counted as already there — not copied again, and not reported as
something to download. A real 0.252 library came out as 7,256 of 8,764 games already
present, 1,508 to fetch and the rest accounted for.

The source folder does not have to hold anything for this: the library is a statement
about what you have, not only a place to put things.

## What it does

- Parse `mameXXXX.xml`, read the ROM data, and filter out non-working drivers,
  prototypes, betas, screenless devices, mechanical cabinets and BIOS sets
- Find the MAME XML and catlist.ini by itself, and refuse to run when the two are for
  different MAME versions
- Derive genres and file every machine under `Genre/Category/`
- Separate ROM and CHD source folders, and a separate destination folder for adult titles
- List the missing ROM and CHD files, so only what is needed is downloaded
- Download the matching catlist.ini by itself when it is not already on disk
- Exclude by ROM name, by genre or by category
- Dry run, a written report of what is missing, and a free-space check before copying
- Copy to a local folder, or to a console over SMB, FTP, FTPS or SFTP
- Sync rather than copy: work out what is new, changed, merely moved, or no longer wanted
- Relocate a recategorised machine instead of re-copying it
- Optionally remove destination files the plan no longer wants, for romset upgrades

### On the console

- Write a `gamelist.xml` at the root of the library, so EmulationStation shows
  *Metal Slug - Super Vehicle-001, 1996, Nazca, 2 players* instead of `mslug` — every
  field of it is already known, nothing is scraped
- Place the downloaded artwork beside the games
- **One game, one ROM**: keep each family's parent and leave its other revisions out.
  On MAME 0.289 that is 7,538 fewer machines and **80 GB less** — Dragon's Lair alone
  ships four revisions at 11.5 GB each. A game whose parent did not survive the
  filters keeps one version, so nothing disappears.

### Acquisition

- Read the Pleasuredome index and list every published set, its version and its magnet
- Drive qBittorrent over its Web API: add a release, select exactly the files your
  filters want, and leave the other 32,000 at priority 0
- Cost a download before starting it, to the byte — including the pieces BitTorrent
  makes you fetch for files you did not ask for
- Work out a version upgrade from content signatures, so seven releases cost the same
  as one and only the machines you keep are considered
- Hardlink the categorised library out of the torrent folder when they share a
  filesystem, so the library costs no extra space and seeding carries on
- Rewrite the download client's view of a path into yours, for when it runs in another
  container or on another host

### A file that is there is not a file that is finished

qBittorrent allocates every selected file at its full size before a byte of it
arrives, so a torrent folder half-way through a download holds thousands of zips that
are the right size and nothing but zeros. Marquee reads the last 128 bytes of every
zip (the end-of-archive record has to be there) and the first 8 of every CHD (its
header) before calling a file downloaded -- a fraction of a second for 14,000 files.
Anything that fails is "downloading", not "on disk", and is never copied.

## What it does not do

- Fix broken ROMs or rebuild sets — use a ROM manager such as clrmamepro or RomVault
- Scrape box art, videos or metadata — use Skraper or EmulationStation's own scraper
- Host, seed or provide any content of its own
- Handle Software List sets. They are indexed so you can see them, but the library is
  arcade only

## How it works

### Syncing

A run does not copy blindly. It indexes the destination first and works out, per file,
which of the five things above to do with it. A file that is not where it belongs but
is somewhere else at the destination under the same name is a rename, so a
recategorised machine costs a directory operation rather than its own size — on a real
0.282 set, two renamed folders holding 79 files and 12 GB resynced in **0.3 seconds
with nothing written**, against about forty seconds of disk traffic for a plain copy.

`--no-compare` skips the destination index and copies the old way. Over SMB the index
costs one directory listing per folder rather than one per file.

### CHDs

Disks are located by name, not by folder. MAME searches a clone's parent set as well as
the clone itself, so CHD collections routinely keep a clone's own disk inside the
parent's folder — `zokuoten/zokuotena.chd` rather than `zokuotena/zokuotena.chd`.
Looking for a folder named after the machine therefore reports those clones as missing
when the file is right there; on a 0.282 arcade set that was 101 of 101
reported-missing CHDs.

At the destination each machine gets the disks it actually needs:

- a clone whose `<disk>` has a `merge` attribute shares the parent's file, so it is
  placed under the parent's name and MAME's parent lookup finds it — copying a
  multi-gigabyte disk twice is the only alternative;
- a clone without `merge` has a disk of its own and gets its own folder.

Only the named disks are copied, so a source folder holding several clones' disks no
longer drags all of them along.

### MAME support files

Only **catlist.ini** is needed. The other two files this project used to ask for carry
no information it cannot work out on its own:

- `genre.ini` is the leading component of every catlist.ini section (`Arcade: Maze /
  Misc.` → `Maze`). Checked against MAME Extras 0.252 (48 genres, 45,379 machines) and
  0.289 (59 genres, 50,368 machines) — both match exactly, so it is derived instead of
  read. Note the separator is `" / "` and not a bare slash: a genre can contain one of
  its own, as in `Game Console/Computer` or `Videocassette Player/Recorder`.
- `screenless.ini` lists the machines the XML gives no `<display>` element, so it is
  derived from the XML itself. This also means it can never be out of step with your
  romset. Point `screenless_ini` at an old copy and Marquee will compare the two and
  report any difference.
- `category.ini` was never read at all.

`catlist.ini` and `mameXXXX.xml` are looked for next to the package, in `./MameFiles/`,
beside each other, and in the usual MAME locations (`~/.mame`, `/usr/share/mame`,
Batocera, RetroPie, Recalbox). If no XML turns up and `mame` is on PATH, `mame
-listxml` is used to produce one. Both carry a version stamp — `;; CATLIST.ini 0.252 /
... / MAME 0.252 ;;` and the XML's `build` attribute — which are compared before
anything else happens.

A catlist.ini for **another** release is skipped rather than used: it is found first,
files machines under categories they have since moved out of, and the run then stops
on a mismatch over a file nobody remembered putting there. Marquee says which one it
ignored and fetches the matching one instead. Only `--offline`, with nothing else to
use, falls back to it — and says so.

### Picking a MAME version

Tell it which MAME release your romset is and both the machine list and the categories
are fetched for exactly that release — nothing to download by hand, nothing to keep in
step.

```
marquee --list-versions
marquee --mame-version 0.289 --web
```

Support files come from [AntoPISA/MAME_SupportFiles][supportfiles], whose commits are
tagged with the MAME version they carry, and the machine list from
[AntoPISA/MAME_Dats][dats]. The downloaded file's own header is checked against the
version that was asked for, so a reorganised archive cannot quietly hand over another
romset's categories.

The two repositories do not move together, so a version can be categorisable but not
parsable: at the time of writing **17 of 28 published versions** have both, and 0.288
is one of those that does not. `--list-versions` says which and why rather than hiding
them, and any version still works if you supply the XML yourself or generate it with
`mame -listxml`. That history reaches back to **MAME 0.262**; for anything older, take
the MAME Extras pack for your version from [progetto-SNAPS][extras] and drop
catlist.ini in `./MameFiles/`. `--offline` turns the network off entirely.

The XML parse is cached under `~/.cache/marquee` (inside the container, `/config/cache`),
keyed on the file's size and timestamp, so only the first run pays for it.

### What the destination remembers

After every transfer a small `.marquee.json` is written into the destination root:

```json
{ "mame_version": "0.282",
  "settings": { "blacklist_genres": [], "blacklist_categories": [] },
  "stats": { "machines": 9914, "bytes": 280513 },
  "history": [ { "date": "...", "from": "0.279", "to": "0.282", "copied": 412, "moved": 79 } ] }
```

It is what lets the app open and say *this console holds MAME 0.282, 9,914 machines,
synced on Tuesday* instead of asking, and it tells a fresh install apart from an
upgrade. The filters travel with the set, so pointing at a destination can load back
the selection it was built with — handy after a reinstall, or to copy one console's
setup to another.

Only `.zip` and `.chd` files are ever considered this tool's business. A destination is
somebody's console folder: scraped artwork, `gamelist.xml`, ES metadata and the record
itself are never counted as leftovers and never removed.

## Command line

Everything the web UI does can be done without it.

```
marquee [--config settings.ini] [--setup] [--web [--host H] [--port N] [--open]]
        [--rom-dir DIR] [--chd-dir DIR] [--dest DIR|smb://...]
        [--mame-version X.XXX] [--list-versions]
        [--xml mame0252.xml] [--catlist catlist.ini] [--mame-binary mame]
        [--fetch-support-files] [--offline] [--ignore-version-mismatch]
        [--no-cache] [--refresh-cache]
        [-n|--dry-run] [-y|--yes] [-q|--quiet] [--report plan.json]
        [--delete-orphans] [--no-compare] [--allow-mature|--no-mature]
```

`--setup` asks for the three paths that cannot be guessed and writes a settings.ini for
you; running with nothing configured offers the same thing. The paths can also be given
entirely on the command line, in which case no settings.ini is needed at all:

```
marquee --rom-dir ~/mame/roms --chd-dir ~/mame/chds --dest /media/batocera/roms/mame -n
```

`--dry-run` reports the plan — how many machines, how many files, how much space, what
is missing, and what would actually be transferred against what is already at the
destination — and stops. `--report` writes the same thing to a `.json` or `.csv` file,
so the missing-ROM list can be fed to whatever you download with. Before copying
starts, the space the run needs is compared against what is free at the destination.

By default `settings.ini` is read from the folder the package lives in, and relative
paths inside it are resolved against that same folder, so it can be run from anywhere.
`MameCleaner.py` in the checkout is the same entry point under its old name and takes
the same flags.

**Use one MAME version for everything**: a 0.251 romset wants `mame0251.xml`, the 0.251
catlist.ini and the 0.251 CHDs. Marquee checks this and stops if they disagree.
**Non-merged** ROM sets are strongly recommended — each zip is then self-contained,
which is what makes per-game picking work.

## Settings

`settings.ini` holds the paths, the selection and the download client. Copy
[settings.ini.example](settings.ini.example) and edit it, run `marquee --setup`, or
just use the Settings page — it writes the same file. In Docker it lives in the
`CONFIG` volume, beside the API key and the caches.

## Troubleshooting

### The container shows a folder the host has emptied

A Docker bind mount resolves to an inode when the container starts, not to a path. So
renaming the library folder on the host — or deleting and recreating it — leaves the
container looking at the old directory, with its old contents, for as long as it runs.
The host shows an empty folder, the app shows 28 genres, and neither is lying.

```bash
docker compose up -d --force-recreate     # re-resolves every mount
```

It is worth knowing which way round it went: if the old folder was renamed rather than
deleted, the files are still there under the new name, and the mount is what was
keeping them reachable.

### The UI is reachable but the download client is not

The client is reached from wherever Marquee runs. In Docker that is the container, so
`localhost` means the container itself — use the host's address, or put both on the
same Docker network. **Test** in Settings reports what went wrong, and on success says
which qBittorrent answered and where it is downloading to; if that folder is not one
this app can read, set a remote path mapping beside it.

## Development

```
marquee/
    config.py      Config dataclass, settings.ini reading and writing
    sources.py     finding MAME data, version headers, streaming parse, parse cache
    fetch.py       downloading version-matched support files
    catalog.py     filtering, screenless/genre derivation, categories, folder names
    plan.py        deciding what to copy, free space, reports
    sync.py        diffing a plan against what is already at the destination
    verify.py      reading what a zip actually holds and checking it against the release
    art.py         title screens from libretro-thumbnails, cached locally
    gamelist.py    the file EmulationStation reads
    manifest.py    .marquee.json: what the destination remembers
    pipeline.py    build_plan() and execute(), the whole job with no terminal attached
    acquire.py     mapping wanted machines onto file indices inside a torrent
    acquisition.py what a download would cost, and driving a client to make it
    indexers/      the Pleasuredome index and release datfiles
    download/      the qBittorrent Web API client
    backends/      local, SMB, FTP and SFTP destinations
    reporting.py   Reporter protocol: terminal, collecting, silent
    errors.py      MarqueeError and its subclasses
    cli.py         flags, prompts, printing, exit codes
    web/           server.py, job.py and a single-page front end
```

Nothing below `cli.py` prints, prompts or calls `sys.exit`; it reports through a
`Reporter` and raises `MarqueeError`. Planning and copying are separate calls, so a
caller can show the plan and decide before anything is written:

```python
from marquee import Config, pipeline

config = Config(rom_dir="~/mame/roms", chd_dir="~/mame/chds",
                copy_path="/media/batocera/roms/mame")
plan, resolution = pipeline.build_plan(config)        # copies nothing
print(len(plan.items), plan.missing_roms)
summary = pipeline.execute(plan, config)              # only if you decide to
```

Pass a `Reporter` subclass to either call to receive progress and messages instead of
having them printed.

### Tests

```
pip install -e ".[dev]"
pytest
```

The suite runs against a small synthetic romset in `tests/fixtures/`, so it needs no
MAME data of its own. Where it makes a claim about the real MAME Extras files — that
genre.ini is redundant, for instance — it checks that against `MameFiles/` when you
have put the real thing there, and skips it otherwise. The front end has no build step
and no framework; its modules are checked by the suite too.

Tested on Linux and Batocera; it should work on RecalBox, RetroPie and other distros.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the measurements
behind it, and [docs/ROADMAP.md](docs/ROADMAP.md) for what was built, what was measured
and rejected, and what is deliberately not being done.

## Credits and upstream sources

Marquee is a thin layer over other people's work. It bundles none of it and fetches
what it needs on demand.

- **[MAME][mame]** and MAMEdev — the emulator and, through `-listxml`, the machine
  data everything here is derived from: ROM and disk lists, CRCs, driver status,
  displays, inputs and parent/clone relationships. MAME is licensed GPL-2.0-or-later.
- **[AntoPISA / progetto-SNAPS][snaps]** — `catlist.ini`, the category file that gives
  every machine its genre and category, and so the entire folder layout. Fetched
  version-matched from [MAME_SupportFiles][supportfiles]; the machine list comes from
  [MAME_Dats][dats]. Neither is bundled here.
- **[Pleasuredome][pd]** — the MAME torrents and their datfiles. Marquee reads the
  public index to learn what has been published and hands the magnet to your own
  client; it hosts nothing and seeds nothing.
- **[libretro-thumbnails][thumbs]** — title screens, snapshots, box art and logos,
  named after MAME's own `<description>`, which is why no scraper is needed.
- **[qBittorrent][qbt]** — the download client driven over its Web API v2.
- **EmulationStation** and the [Batocera][batocera], [RetroPie][retropie] and
  [Recalbox][recalbox] projects — the `gamelist.xml` format and the folder conventions
  the library is written for.
- **[Sonarr][sonarr] and [Radarr][radarr]** — the shape of the thing: a library you
  declare, a client that fetches it, and a page that tells you what is missing.
- [pysmb][pysmb] and [paramiko][paramiko] for the SMB and SFTP destinations.

[mame]: https://www.mamedev.org/
[snaps]: https://www.progettosnaps.net/
[extras]: https://www.progettosnaps.net/support/
[supportfiles]: https://github.com/AntoPISA/MAME_SupportFiles
[dats]: https://github.com/AntoPISA/MAME_Dats
[pd]: https://pleasuredome.github.io/pleasuredome/mame/index.html
[thumbs]: https://github.com/libretro-thumbnails/libretro-thumbnails
[qbt]: https://www.qbittorrent.org/
[batocera]: https://batocera.org/
[retropie]: https://retropie.org.uk/
[recalbox]: https://www.recalbox.com/
[sonarr]: https://sonarr.tv/
[radarr]: https://radarr.video/
[pysmb]: https://github.com/miketeo/pysmb
[paramiko]: https://www.paramiko.org/

## Licence

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).

```
Marquee — a MAME library manager
Copyright (C) 2026 Baybars HAZAR (Biohazardious) and contributors

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.
```

GPLv3 is the same licence Sonarr, Radarr and the rest of that family use, which felt
like the right company to keep.

The licence covers Marquee's own code. MAME, the support files, the thumbnails and
anything you download with it belong to their respective authors and carry their own
terms. You are responsible for having the right to the ROMs you point this at.
