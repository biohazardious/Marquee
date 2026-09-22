"""Screenshots for the library.

libretro-thumbnails names its files after MAME's own `<description>`, so the URL for a
machine's title screen is derivable from data already in hand -- no scraper, no lookup
table, no API key. This module caches them locally so the library keeps its pictures
when GitHub is slow, when the box is offline, and without asking GitHub for the same
image on every page.
"""
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

from .reporting import Reporter
from .sources import cache_dir

REPO = "https://raw.githubusercontent.com/libretro-thumbnails/MAME/master"
KINDS = ("Named_Titles", "Named_Snaps", "Named_Boxarts", "Named_Logos")
DEFAULT_KIND = "Named_Titles"

# The characters libretro replaces when turning a game's name into a filename.
UNSAFE = '&*/:`<>?\\|"'
TIMEOUT = 20
WORKERS = 8
USER_AGENT = "Marquee/0.4"


class ArtUnavailable(Exception):
    """The source could not be asked -- a rate limit, an outage, no network.

    Distinct from a picture that does not exist, which is the ordinary case for a
    quarter of the set and is not an error at all.
    """


def safe_name(description):
    return "".join("_" if char in UNSAFE else char for char in description or "")


def url_for(description, kind=DEFAULT_KIND):
    if not description:
        return None
    return f"{REPO}/{kind}/{urllib.parse.quote(safe_name(description))}.png"


def art_dir(kind=DEFAULT_KIND):
    return os.path.join(cache_dir(), "art", kind)


def path_for(description, kind=DEFAULT_KIND):
    """Where a machine's picture lives once it has been fetched.

    Keyed by description rather than by machine name: that is what libretro keys on,
    so two machines sharing a description share one file instead of two copies.
    """
    if not description:
        return None
    return os.path.join(art_dir(kind), safe_name(description) + ".png")


def have(description, kind=DEFAULT_KIND):
    path = path_for(description, kind)
    return bool(path) and os.path.isfile(path)


def fetch_one(description, kind=DEFAULT_KIND, timeout=TIMEOUT):
    """Download one picture into the cache. Returns True when a file is now there.

    A machine with no thumbnail is not an error: coverage is about three quarters of
    the set, and the gaps are obscure gambling and laserdisc machines.
    """
    target = path_for(description, kind)
    if not target:
        return False
    if os.path.isfile(target):
        return True

    request = urllib.request.Request(url_for(description, kind),
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        # A rate limit or an outage is not "no picture exists": say so, or every
        # missing thumbnail is silently re-requested from GitHub on the next pass and
        # the summary reads as if three quarters of the set had none.
        raise ArtUnavailable(f"{error.code} from {request.full_url}")
    except (urllib.error.URLError, OSError) as error:
        raise ArtUnavailable(str(error))
    if not body:
        return False

    os.makedirs(os.path.dirname(target), exist_ok=True)
    partial = target + ".part"
    try:
        with open(partial, "wb") as handle:
            handle.write(body)
        os.replace(partial, target)
    except OSError:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass
        return False
    return True


def download(descriptions, kind=DEFAULT_KIND, on_progress=None, should_continue=None,
             workers=WORKERS, reporter=None):
    """Fetch every missing picture. Returns a small summary.

    Runs a handful of threads: each file is a few kilobytes and the time is all
    round-trip, so doing them one at a time would take an hour for what takes a
    minute.
    """
    reporter = reporter or Reporter()
    wanted = [text for text in dict.fromkeys(descriptions) if text]
    todo = queue.Queue()
    for description in wanted:
        todo.put(description)

    total = len(wanted)
    state = {"done": 0, "fetched": 0, "cached": 0, "missing": 0, "failed": 0,
             "error": None}
    lock = threading.Lock()

    def work():
        while True:
            try:
                description = todo.get_nowait()
            except queue.Empty:
                return
            # A stopped run still counts what it skipped, so the progress bar reaches
            # its total rather than freezing half way and looking hung.
            stopped = bool(should_continue) and not should_continue()
            already = False if stopped else have(description, kind)
            failure = None
            try:
                ok = False if stopped else (already or fetch_one(description, kind))
            except ArtUnavailable as error:
                ok, failure = False, str(error)
            with lock:
                state["done"] += 1
                if already:
                    state["cached"] += 1
                elif ok:
                    state["fetched"] += 1
                elif failure:
                    state["failed"] += 1
                    state["error"] = failure
                else:
                    state["missing"] += 1
                done = state["done"]
            if on_progress:
                on_progress(done, total)
            todo.task_done()

    threads = [threading.Thread(target=work, daemon=True) for _ in range(min(workers, max(total, 1)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    reporter.info(f"Artwork: {state['fetched']} fetched, {state['cached']} already "
                  f"cached, {state['missing']} not published.")
    if state["failed"]:
        reporter.warn(f"{state['failed']} picture(s) could not be fetched "
                      f"({state['error']}); they will be tried again next time.")
    return {"total": total, **state}


_TOTALS = {}


def cache_totals(kind=DEFAULT_KIND):
    """(pictures, bytes) in the cache for `kind`, in one pass over the folder.

    The artwork page asks every 1.2 s while a download runs, and it used to be three
    listings and 24,000 stats of a 12,000-picture folder each time: 200 ms a poll.
    The answer is kept until the folder's own timestamp moves, which is every time a
    picture lands and never otherwise.
    """
    directory = art_dir(kind)
    try:
        stamp = os.stat(directory).st_mtime_ns
    except OSError:
        return 0, 0
    cached = _TOTALS.get(directory)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    count = total = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".png"):
                    try:
                        total += entry.stat().st_size
                        count += 1
                    except OSError:
                        pass
    except OSError:
        return 0, 0
    _TOTALS[directory] = (stamp, count, total)
    return count, total


def cached_count(kind=DEFAULT_KIND):
    return cache_totals(kind)[0]


def cache_bytes(kind=DEFAULT_KIND):
    return cache_totals(kind)[1]
