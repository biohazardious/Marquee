"""The Pleasuredome MAME index.

The page is a flat run of paragraphs rather than a table: a heading names the set,
then a "Set:" line carries the magnet and a "Datfile:" line the DAT. The set name
inside the magnet's link text is the only reliable source of the kind, the MAME
version and the merge variant, so that is what gets parsed -- headings on the page
lag the sets they describe (the CHD heading still says "unchanged" while the link
underneath names a newer release).

Verified against the live page on 2026-09-14.
"""
import html
import os
import re
import urllib.parse
import urllib.request

from .release import (Release, ROMS, CHDS, BIOS, SL_ROMS, SL_CHDS, EXTRAS, UPDATE,
                      ROLLBACK, MERGED, NON_MERGED, SPLIT)
from ..errors import MarqueeError

INDEX_URL = "https://pleasuredome.github.io/pleasuredome/mame/index.html"
USER_AGENT = "Marquee/0.4 (+https://github.com/pleasuredome)"

_ANCHOR = re.compile(
    r'<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', re.S | re.I)
_BTIH = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})", re.I)
_VERSION = re.compile(r"\b0\.(\d{3})\b")
_UPDATE_RANGE = re.compile(r"\(v(0\.\d+)\s*to\s*v(0\.\d+)\)", re.I)
_VARIANT = re.compile(r"\((merged|non-merged|split|bios-devices)\)", re.I)

# A dir2dat datfile is a second description of the same set, generated from the files
# on disk rather than from MAME. Only the canonical one is kept.
_DIR2DAT = re.compile(r"\(dir2dat\)", re.I)


def _kind_of(name):
    lowered = name.lower()
    if lowered.startswith("mame - update") or " update " in lowered:
        return UPDATE
    # A rollback set is a full-set-shaped torrent holding only superseded files, so it
    # has to be recognised before the "roms"/"chds" tests below claim it.
    if "rollback" in lowered:
        return ROLLBACK
    if "software list chds" in lowered:
        return SL_CHDS
    if "software list roms" in lowered:
        return SL_ROMS
    if "bios-devices" in lowered:
        return BIOS
    if "chds" in lowered:
        return CHDS
    if "extras" in lowered or "multimedia" in lowered:
        return EXTRAS
    if "roms" in lowered:
        return ROMS
    return None


def _update_kind(name):
    """What an update set updates. `Update ROMs` and `Update CHDs` differ only here."""
    lowered = name.lower()
    if "software list chds" in lowered:
        return SL_CHDS
    if "software list roms" in lowered:
        return SL_ROMS
    if "chds" in lowered:
        return CHDS
    if "extras" in lowered:
        return EXTRAS
    return ROMS


def _variant_of(name):
    found = _VARIANT.search(name)
    if not found:
        return None
    value = found.group(1).lower()
    return {"merged": MERGED, "non-merged": NON_MERGED, "split": SPLIT}.get(value)


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse(document):
    """Every release on the page, plus the datfile URLs keyed by set name."""
    releases = {}
    datfiles = {}

    for match in _ANCHOR.finditer(document):
        href = html.unescape(match.group("href"))
        label = _text(match.group("text"))
        if not label:
            continue

        if href.startswith("magnet:"):
            found = _BTIH.search(href)
            if not found:
                continue
            release = _release_from(label, found.group(1), href)
            if release:
                releases[release.name] = release
        elif ".zip" in href.lower() and "/mame/" in href.lower():
            if _DIR2DAT.search(label) or _DIR2DAT.search(href):
                continue
            datfiles.setdefault(label, href)

    # The datfile link text is the set name, so the two halves join on it. When they
    # disagree -- the CHD row links the 0.289 datfile beside the 0.288 magnet -- the
    # magnet wins, because that is what actually gets downloaded.
    joined = []
    for release in releases.values():
        url = datfiles.get(release.name)
        joined.append(release if url is None else
                      Release(**{**release.__dict__, "datfile_url": url}))
    return sorted(joined, key=lambda r: (r.kind, r.version or "", r.variant or ""))


def _release_from(label, infohash, magnet):
    if len(infohash) == 32:
        import base64
        infohash = base64.b32decode(infohash.upper()).hex()
    infohash = infohash.lower()

    kind = _kind_of(label)
    if kind is None:
        return None

    from_version = None
    if kind == UPDATE:
        span = _UPDATE_RANGE.search(label)
        if not span:
            return None
        from_version, version = span.group(1), span.group(2)
        kind = _update_kind(label)
        return Release(kind=kind, version=version, from_version=from_version,
                       infohash=infohash, name=label, variant=_variant_of(label),
                       magnet=magnet)

    found = _VERSION.search(label)
    if not found:
        return None
    return Release(kind=kind, version=f"0.{found.group(1)}", infohash=infohash,
                   name=label, variant=_variant_of(label), magnet=magnet)


class Pleasuredome:
    name = "Pleasuredome"

    def __init__(self, url=None, timeout=30):
        # MARQUEE_INDEX_URL points it at a mirror of the page -- or, for a test run,
        # at a stand-in listing sets that are nobody's ROMs.
        self.url = url or os.environ.get("MARQUEE_INDEX_URL", "").strip() or INDEX_URL
        self.timeout = timeout

    def fetch(self):
        request = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            raise MarqueeError(f"Could not reach Pleasuredome: {error}")

    def releases(self, document=None):
        return parse(document if document is not None else self.fetch())

    def current(self, document=None):
        """The newest full ROM and CHD sets -- the two a library is actually built from.

        They are published independently and routinely sit a version apart, so this
        returns each at its own latest rather than forcing them to agree.
        """
        found = self.releases(document)
        roms = [r for r in found
                if r.kind == ROMS and r.variant == NON_MERGED and r.is_full_set]
        chds = [r for r in found if r.kind == CHDS and r.is_full_set]
        return {
            "roms": max(roms, key=_version_key, default=None),
            "chds": max(chds, key=_version_key, default=None),
        }


def _version_key(release):
    found = _VERSION.search(release.version or "")
    return int(found.group(1)) if found else -1


def datfile_name(url):
    return urllib.parse.unquote(url.rsplit("/", 1)[-1])
