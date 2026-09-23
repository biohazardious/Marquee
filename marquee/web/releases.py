"""What has been published: Marquee's own releases, MAME's, and moving a library between them."""
import dataclasses
import json
import os
import re
import time
import urllib.request

from .. import __version__, acquisition, fetch, indexers, manifest, pipeline
from ..reporting import Reporter
from ..errors import MarqueeError


class ReleasesMixin:
    """Part of `Application`; see marquee.web.application."""

    def newest_published(self):
        """The newest MAME release with a full ROM set in the listing, or None.

        Read from the listing in hand, or fetched once if there is none yet -- this
        runs on a plan's worker thread, and only when nothing else names a release.
        """
        data = self.releases.get("data")
        if data is None:
            found = indexers.default().releases()
            data = [_release_payload(r) for r in found]
            self.releases.update(data=data, error=None, fetched_at=time.time())
        versions = [row["version"] for row in data
                    if row.get("kind") == "roms" and row.get("full_set") and row.get("version")]
        return max(versions, key=_version_key) if versions else None

    # -- this program's own version ------------------------------------------ #

    UPDATE_TTL = 12 * 60 * 60
    UPDATE_RETRY = 60 * 60
    TAGS_URL = "https://api.github.com/repos/biohazardious/Marquee/tags?per_page=30"
    # The same tags as a feed. Not the API, so not its 60-an-hour allowance -- which
    # everything else on the same address shares, and which a TrueNAS box had used
    # up, leaving "HTTP Error 403: rate limit exceeded" on the System page.
    TAGS_FEED = "https://github.com/biohazardious/Marquee/tags.atom"

    def about(self):
        """Version, build and whether something newer is out -- for the sidebar."""
        self._watch_updates()
        latest = self.update["latest"]
        return {
            "version": __version__,
            "build": build_label(),
            "update": {
                "latest": latest,
                "newer": bool(latest and _version_key(latest) > _version_key(__version__)),
                "url": f"https://github.com/biohazardious/Marquee/releases/tag/v{latest}"
                       if latest else None,
                "error": self.update["error"],
            },
        }

    def _watch_updates(self):
        if os.environ.get("MARQUEE_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
            return
        age = time.time() - (self.update["checked_at"] or 0)
        due = self.update["checked_at"] is None or age > (
            self.UPDATE_RETRY if self.update["error"] else self.UPDATE_TTL)
        if self.update["running"] or not due:
            return

        def work():
            try:
                names = self._tags_from_api()
            except (OSError, ValueError):
                names = self._tags_from_feed()
            self.update["latest"] = newest_version(names)
            self.update["error"] = None

        # A failed check is a note, not a fault: it is retried after UPDATE_RETRY.
        self._start(self.update, work,
                    finish=lambda: self.update.update(checked_at=time.time()))

    def _tags_from_api(self):
        request = urllib.request.Request(
            self.TAGS_URL, headers={"User-Agent": f"Marquee/{__version__}",
                                    "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            tags = json.loads(response.read().decode("utf-8"))
        return [entry.get("name", "") for entry in tags if isinstance(entry, dict)]

    def _tags_from_feed(self):
        request = urllib.request.Request(
            self.TAGS_FEED, headers={"User-Agent": f"Marquee/{__version__}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
        # Each entry links to .../releases/tag/<name>.
        return re.findall(r"/releases/tag/([^\"'<>\s]+)", body)

    def refresh_releases(self, _body=None):
        def work():
            found = indexers.default().releases()
            self.releases["data"] = [_release_payload(r) for r in found]
            self.releases["error"] = None

        # Anything it raises is recorded, not just a MarqueeError: a truncated read
        # from the index that escaped left "running" false with no data, which
        # release_watch read as "never asked" and asked again on every poll.
        self._start(self.releases, work,
                    finish=lambda: self.releases.update(fetched_at=time.time()))
        return {"started": True}

    # How long a release listing is trusted before it is fetched again. Pleasuredome
    # publishes about monthly, so this is about noticing within a day, not polling.
    RELEASE_TTL = 6 * 60 * 60
    # After a failure: soon enough to recover from a blip, not so soon as to hammer.
    RELEASE_RETRY = 10 * 60

    def release_watch(self):
        """Whether anything newer than this library has been published.

        Cheap enough to answer on every poll: it reads the listing already in memory,
        and only kicks off a refresh when that listing has gone stale.
        """
        age = time.time() - (self.releases["fetched_at"] or 0)
        due = self.releases["data"] is None and self.releases["fetched_at"] is None
        due = due or age > (self.RELEASE_RETRY if self.releases["error"] else self.RELEASE_TTL)
        if not self.releases["running"] and due:
            self.refresh_releases()

        versions = sorted({entry["version"]
                           for entry in (self.releases.get("data") or [])
                           if entry["kind"] == "roms" and entry["full_set"]},
                          reverse=True)
        newest = versions[0] if versions else None
        record = manifest.read(self.current_config().copy_path)
        here = (record or {}).get("mame_version") or self.current_config().mame_version
        return {"newest": newest, "library": here,
                "newer": bool(newest and here and _version_key(newest) > _version_key(here))}

    def releases_payload(self):
        # Fetched once, lazily. It is a static page that changes about once a month,
        # so asking for it on the first read beats making the user press a button to
        # find out what MAME releases exist.
        if self.releases["data"] is None and not self.releases["running"] \
                and not self.releases["error"]:
            self.refresh_releases()
        return dict(self.releases)

    # -- version upgrades ---------------------------------------------------- #

    def upgrade_payload(self):
        record = manifest.read(self.current_config().copy_path)
        return {**self.upgrade,
                "library_version": (record or {}).get("mame_version"),
                "configured_version": self.current_config().mame_version,
                "available": sorted(
                    {entry["version"] for entry in (self.releases.get("data") or [])
                     if entry["kind"] == "roms" and entry["full_set"]},
                    reverse=True)}

    def preview_upgrade(self, body):
        """What moving the library to another release would actually cost.

        The whole thesis: 0.282 -> 0.289 changes 244 of the wanted machines, which is
        a few gigabytes rather than the 163 of a full set -- only the machines this
        library keeps are considered.

        "Keeps" is asked of the target release, not of the library. The filters are
        re-run on its XML: a game MAME fixed (44 went from "does not work" to working
        between 0.282 and 0.289 -- Daytona USA, Cyber Sled, Dead or Alive) joins the
        selection, one it broke leaves it, and a driver new in that release is there
        at all. Comparing only what the library already held saw none of that.
        """
        if self.upgrade["running"]:
            raise MarqueeError("Already working that out.")
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is a library to move.")

        target = (body or {}).get("version")
        if not target:
            raise MarqueeError("Which release should it move to?")
        config = self.current_config()
        record = manifest.read(config.copy_path)
        current = (record or {}).get("mame_version") or config.mame_version
        if not current:
            raise MarqueeError("This library does not say which release it holds.")

        # What the library holds, not what the source folder happens to hold: a
        # machine already at the destination with no zip in the torrent folder is
        # exactly the kind an upgrade is about.
        held = {item.name for item in plan.library_items}

        def work():
            # The same settings, planned at the target release. Not compared with
            # the library -- only which machines it would want is asked of it. A
            # catlist for a release out this week often is not yet: the older one
            # still sorts every machine it knows, which is all a preview needs.
            target_plan, _resolution = pipeline.build_plan(
                dataclasses.replace(config, mame_version=target),
                pipeline.SourceOptions(version=target, compare_destination=False,
                                       ignore_version_mismatch=True),
                Reporter())
            then = acquisition.records(fetch.fetch_xml(current))
            there = acquisition.records(fetch.fetch_xml(target))
            wanted = {item.name: item for item in target_plan.wanted}
            split = upgrade_split(held, wanted, then, there)
            describe = lambda names: [  # noqa: E731
                {"name": name, "description": wanted[name].description
                 if name in wanted else (there.get(name) or then.get(name) or {}).get("d", name)}
                for name in names[:12]]
            self.upgrade["data"] = {
                "from": current, "to": target,
                "unchanged": len(split["unchanged"]),
                "changed": len(split["changed"]),
                "added": len(split["now_working"]) + len(split["new"]) + len(split["missing"]),
                "now_working": len(split["now_working"]),
                "new": len(split["new"]),
                "missing": len(split["missing"]),
                "leaving": len(split["leaving"]),
                "fetch": sorted(split["changed"] + split["now_working"] + split["new"]
                                + split["missing"]),
                "examples": split["changed"][:12],
                "now_working_examples": describe(split["now_working"]),
                "new_examples": describe(split["new"]),
                "leaving_examples": describe(split["leaving"]),
            }

        if not self._start(self.upgrade, work, data=None, error=None, to=target):
            raise MarqueeError("Already working that out.")
        return {"started": True, "from": current, "to": target}


def works(record):
    """Whether MAME says a machine runs, the way the not-working filter decides it."""
    return (record or {}).get("st") == "good" or (record or {}).get("em") == "good"


def upgrade_split(held, wanted, then, there):
    """Sort a move between releases into what it means for this library.

    `held` is what the library has, `wanted` what the target selection wants, and
    `then` / `there` the two releases' machine records (content signature `sig`,
    driver status `st`, emulation `em`).
    """
    out = {key: [] for key in ("unchanged", "changed", "now_working", "new",
                               "missing", "leaving")}
    for name in sorted(wanted):
        if name in held:
            same = (then.get(name) or {}).get("sig") == (there.get(name) or {}).get("sig")
            out["unchanged" if same else "changed"].append(name)
        elif name not in then:
            out["new"].append(name)
        elif not works(then[name]):
            out["now_working"].append(name)
        else:
            # In the old release and running there, just not in the library: never
            # fetched, or a filter that no longer drops it.
            out["missing"].append(name)
    out["leaving"] = sorted(name for name in held if name not in wanted)
    return out


def newest_version(tag_names):
    """The highest release among tag names like v0.5.0; None when there is none."""
    best = None
    for name in tag_names:
        text = str(name or "").strip()
        if not text.startswith("v"):
            continue
        candidate = text[1:]
        if not all(part.isdigit() for part in candidate.split(".")):
            continue
        if best is None or _version_key(candidate) > _version_key(best):
            best = candidate
    return best


def build_label():
    """The build the image was made from: "master@7c17217", "v0.5.0@7c17217", or
    "source" for a checkout run directly."""
    raw = (os.environ.get("MARQUEE_BUILD") or "").strip()
    if not raw or raw == "local":
        return "local" if raw else "source"
    ref, _, sha = raw.partition("@")
    return f"{ref}@{sha[:7]}" if sha else ref


def _version_key(version):
    """"0.289" sorts after "0.9" -- compare the numbers, not the string."""
    try:
        return tuple(int(part) for part in str(version).split("."))
    except (TypeError, ValueError):
        return (0,)


def _release_payload(release):
    return {"kind": release.kind, "version": release.version,
            "variant": release.variant, "name": release.name,
            "infohash": release.infohash, "from_version": release.from_version,
            "full_set": release.is_full_set, "magnet": release.magnet_uri(),
            "datfile": release.datfile_url}
