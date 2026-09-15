"""Fetching MAME support files, version-matched to the romset.

AntoPISA publishes the MAME Extras ini files to a git repository whose commits are
tagged with the MAME version they carry, so a file matching any particular romset can
be pulled from the commit that introduced it. Only catlist.ini is actually required by
this tool; the rest are offered because they are useful to cross-check against.

Coverage is whatever the repository's history holds -- 0.262 upwards at the time of
writing. Older romsets are not served here; progetto-SNAPS keeps a deeper archive that
has to be fetched by hand.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import atomic, sources
from .errors import SourceNotFoundError
from .reporting import Reporter

REPO = "AntoPISA/MAME_SupportFiles"
# The listxml is published in a second repository, updated less often than the first --
# which is why some versions can be categorised but not parsed.
DATS_REPO = "AntoPISA/MAME_Dats"
XML_PATH = "ARCADE_xml/arcade.xml"
ARCHIVE_URL = "https://www.progettosnaps.net/support/"
USER_AGENT = "Marquee (+https://github.com/AntoPISA/MAME_SupportFiles)"

# Where each support file lives inside the repository.
SUPPORT_PATHS = {
    "catlist.ini": "catver.ini/catlist.ini",
    "genre.ini": "catver.ini/genre.ini",
    "screenless.ini": "category.ini/screenless.ini",
}

COMMIT_LIST_TTL = 24 * 60 * 60
VERSION_IN_MESSAGE_RE = re.compile(r'(\d+\.\d+)')


# Kept as an alias so callers can catch either name.
SupportFileError = SourceNotFoundError


def _request(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise SourceNotFoundError(
                f"GitHub refused the request for {url} (HTTP 403). The unauthenticated "
                f"API allows 60 requests an hour; try again later or pass --offline and "
                f"place the file yourself.") from error
        raise SourceNotFoundError(f"HTTP {error.code} fetching {url}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SourceNotFoundError(f"Could not reach {url}: {error}") from error


def _commits_cache_path(tag):
    return os.path.join(sources.cache_dir(), f"commits-{tag}.json")


def _commit_history(timeout=30, refresh=False, repo=REPO, path=None, tag="support"):
    """[(version, sha)] newest first, cached because the API is rate limited."""
    cache_file = _commits_cache_path(tag)
    if not refresh and os.path.isfile(cache_file):
        try:
            if time.time() - os.path.getmtime(cache_file) < COMMIT_LIST_TTL:
                with open(cache_file, encoding="utf-8") as handle:
                    return [tuple(entry) for entry in json.load(handle)]
        except (OSError, ValueError):
            pass

    quoted = urllib.parse.quote(path or SUPPORT_PATHS["catlist.ini"])
    url = f"https://api.github.com/repos/{repo}/commits?path={quoted}&per_page=100"
    payload = json.loads(_request(url, timeout).decode("utf-8"))

    history = []
    for commit in payload:
        match = VERSION_IN_MESSAGE_RE.search(commit["commit"]["message"])
        if match:
            history.append((match.group(1), commit["sha"]))

    try:
        atomic.write_json(cache_file, history)
    except OSError:
        pass
    return history


def available_versions(timeout=30, refresh=False):
    """MAME versions whose catlist can be fetched, oldest first."""
    versions = {version for version, _sha in _commit_history(timeout, refresh)}
    return sorted(versions, key=float)


def xml_versions(timeout=30, refresh=False):
    """MAME versions whose listxml can be fetched, oldest first."""
    history = _commit_history(timeout, refresh, repo=DATS_REPO, path=XML_PATH, tag="dats")
    return sorted({version for version, _sha in history}, key=float)


def version_catalogue(timeout=30, refresh=False):
    """Every version either repository knows about, and what is there for each.

    The two repositories do not move in step, so a version can be categorisable but not
    parsable. Saying which is missing is more use than hiding the version.
    """
    catlists = set(available_versions(timeout, refresh))
    xmls = set(xml_versions(timeout, refresh))
    entries = []
    for version in sorted(catlists | xmls, key=float, reverse=True):
        has_xml, has_catlist = version in xmls, version in catlists
        entries.append({
            "version": version, "xml": has_xml, "catlist": has_catlist,
            "usable": has_xml and has_catlist,
            "reason": None if has_xml and has_catlist else
                      ("no listxml published for this version -- supply one with --xml, "
                       "or generate it with mame -listxml" if not has_xml
                       else "no catlist published for this version"),
        })
    return entries


def cached_xml(version):
    return os.path.join(sources.cache_dir(), "xml", f"mame{version}.xml")


def fetch_xml(version, timeout=600, refresh=False, reporter=None):
    """Download the listxml for `version` and return its path.

    Roughly eighty megabytes, so it is cached and the caller is told it is happening.
    """
    reporter = reporter or Reporter()
    destination = cached_xml(version)
    if os.path.isfile(destination) and os.path.getsize(destination) > 0 and not refresh:
        return destination

    history = _commit_history(repo=DATS_REPO, path=XML_PATH, tag="dats")
    sha = next((commit for candidate, commit in history if candidate == version), None)
    if sha is None:
        known = sorted({v for v, _ in history}, key=float)
        raise SourceNotFoundError(
            f"No MAME {version} listxml is published. Available: "
            f"{', '.join(known)}. Supply one with --xml, or generate it with "
            f"mame -listxml.")

    url = f"https://raw.githubusercontent.com/{DATS_REPO}/{sha}/{urllib.parse.quote(XML_PATH)}"
    reporter.info(f"Downloading the MAME {version} XML (about 80 MB)..")
    payload = _request(url, timeout)

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    partial = destination + ".part"
    try:
        with open(partial, "wb") as handle:
            handle.write(payload)
        build = sources.read_xml_build(partial)
        if build and build != version:
            raise SourceNotFoundError(
                f"Downloaded XML reports MAME {build}, not the {version} that was asked "
                f"for. The archive layout may have changed.")
        os.replace(partial, destination)
    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    return destination


def _ref_for_version(version, timeout=30):
    """The commit that carries `version`, or 'main' when it is the newest."""
    history = _commit_history(timeout)
    if not history:
        raise SourceNotFoundError("Could not read the support file history from GitHub.")

    for candidate, sha in history:
        if candidate == version:
            return sha

    known = sorted({v for v, _ in history}, key=float)
    raise SourceNotFoundError(
        f"MAME {version} support files are not in the archive, which covers "
        f"{known[0]} to {known[-1]}. Download the MAME Extras pack for {version} from "
        f"{ARCHIVE_URL} and put catlist.ini in ./MameFiles/, or run with --offline.")


def cached_support_file(name, version):
    return os.path.join(sources.cache_dir(), "support", version, name)


def fetch_support_file(name, version=None, timeout=60, refresh=False, reporter=None):
    """Download one support file and return its local path.

    When `version` is given the matching commit is used and the downloaded file's own
    header is checked against it, so a renamed or reorganised path cannot quietly
    deliver the wrong romset's categories.
    """
    if name not in SUPPORT_PATHS:
        raise SourceNotFoundError(f"Unknown support file: {name}")

    destination = cached_support_file(name, version or "latest")
    if os.path.isfile(destination) and not refresh:
        return destination

    ref = _ref_for_version(version, timeout) if version else "main"
    path = urllib.parse.quote(SUPPORT_PATHS[name])
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"

    (reporter or Reporter()).info(f"Fetching {name} for MAME {version or 'latest'}..")
    payload = _request(url, timeout)

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    partial = destination + ".part"
    try:
        with open(partial, "wb") as handle:
            handle.write(payload)
        _verify_version(partial, name, version)
        os.replace(partial, destination)
    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    return destination


def _verify_version(path, name, wanted):
    _file_version, mame_version = sources.read_ini_version(path)
    if wanted and mame_version and mame_version != wanted:
        raise SourceNotFoundError(
            f"Downloaded {name} reports MAME {mame_version}, not the {wanted} that was "
            f"requested. The archive layout may have changed; fetch the file by hand "
            f"from {ARCHIVE_URL}.")
