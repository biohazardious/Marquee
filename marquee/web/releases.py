"""What has been published: Marquee's own releases, MAME's, and moving a library between them."""
import json
import os
import time
import urllib.request

from .. import __version__, acquire, acquisition, fetch, indexers, manifest
from ..errors import MarqueeError


class ReleasesMixin:
    """Part of `Application`; see marquee.web.application."""

    # -- this program's own version ------------------------------------------ #

    UPDATE_TTL = 12 * 60 * 60
    UPDATE_RETRY = 60 * 60
    TAGS_URL = "https://api.github.com/repos/biohazardious/Marquee/tags?per_page=30"

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
            request = urllib.request.Request(
                self.TAGS_URL, headers={"User-Agent": f"Marquee/{__version__}",
                                        "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                tags = json.loads(response.read().decode("utf-8"))
            self.update["latest"] = newest_version(
                entry.get("name", "") for entry in tags if isinstance(entry, dict))
            self.update["error"] = None

        # A failed check is a note, not a fault: it is retried after UPDATE_RETRY.
        self._start(self.update, work,
                    finish=lambda: self.update.update(checked_at=time.time()))

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

        The whole thesis: 0.282 -> 0.289 changes 244 of the wanted machines and adds
        877, which is 4.5 GB rather than the 163 GB of a full set. Seven releases cost
        the same as one, because only the machines this library keeps are considered.
        """
        if self.upgrade["running"]:
            raise MarqueeError("Already working that out.")
        plan = self.job.plan
        if plan is None:
            raise MarqueeError("Build a plan first, so there is a library to move.")

        target = (body or {}).get("version")
        if not target:
            raise MarqueeError("Which release should it move to?")
        record = manifest.read(self.current_config().copy_path)
        current = (record or {}).get("mame_version") or self.current_config().mame_version
        if not current:
            raise MarqueeError("This library does not say which release it holds.")

        # What the library holds, not what the source folder happens to hold: a
        # machine already at the destination with no zip in the torrent folder is
        # exactly the kind an upgrade is about.
        wanted = [item.name for item in plan.library_items]

        def work():
            here = acquisition.signatures(fetch.fetch_xml(current))
            there = acquisition.signatures(fetch.fetch_xml(target))
            split = acquire.delta(here, there, wanted)
            self.upgrade["data"] = {
                "from": current, "to": target,
                "unchanged": len(split["unchanged"]),
                "changed": len(split["changed"]),
                "added": len(split["added"]),
                "fetch": sorted(split["changed"] + split["added"]),
                "examples": sorted(split["changed"])[:12],
            }

        if not self._start(self.upgrade, work, data=None, error=None, to=target):
            raise MarqueeError("Already working that out.")
        return {"started": True, "from": current, "to": target}


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
