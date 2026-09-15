# MameFiles

Nothing is kept here, and nothing needs to be. This is where Marquee looks for MAME
support files before it goes and fetches them, and where you can drop your own.

## catlist.ini

The one external file the project actually needs: it is what gives every machine its
genre and category, and so the whole folder layout of the library.

You do not have to put it here. Marquee fetches the copy that matches your romset's
MAME version from [AntoPISA/MAME_SupportFiles](https://github.com/AntoPISA/MAME_SupportFiles)
and caches it, and checks the file's own header against the version it asked for.

Put one here when you are working offline, or when your romset is older than **MAME
0.262**, which is as far back as that repository's history reaches. Older releases are
in the MAME Extras packs at [progetto-SNAPS](https://www.progettosnaps.net/support/).

**It has to be the version your romset is.** A catlist.ini for another release files
machines under categories they have since moved out of, and Marquee stops rather than
build a library from it. A copy for the wrong version is skipped in favour of fetching
the right one, and Marquee says which file it ignored and why.

## What is *not* needed

- `genre.ini` — it is the leading part of every catlist.ini section (`Arcade: Maze /
  Misc.` → `Maze`), derived rather than read. Checked against MAME Extras 0.252 and
  0.289: both match exactly.
- `screenless.ini` — it lists the machines the XML gives no `<display>` element, which
  is read from the XML itself. Deriving it means it can never be out of step with your
  romset. Point `screenless_ini` at a copy if you want the two compared and the
  difference reported.
- `category.ini` — never read at all.

A `mameXXXX.xml` dropped here is found too, and is ignored by git: they are around
90 MB. Marquee otherwise fetches the machine list for your version, or generates one
with `mame -listxml` if MAME is on your PATH.
