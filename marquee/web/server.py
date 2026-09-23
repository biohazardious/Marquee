"""The HTTP surface: routing, the API key, static files and cached artwork.

A stdlib ThreadingHTTPServer. For one user on a LAN the cost of a framework is not
worth it: the page polls /api/state rather than holding a stream open, and everything
it can ask for lives in `Application`.
"""
import hmac
import json
import os
import secrets
import signal
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from .. import art
from .. import config as configuration
from ..errors import MarqueeError
from .application import Application
from .views import (changes_payload, left_out_payload, machine_detail, machine_keys,
                  machine_rows)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _host_name(header):
    """The name in a Host header, without its port: 'localhost', '::1', ..."""
    host = (header or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _allowed_hosts():
    """Names a keyless server answers to. A reverse proxy in front of a loopback
    server that keeps its own Host header adds it with MARQUEE_ALLOWED_HOSTS."""
    extra = os.environ.get("MARQUEE_ALLOWED_HOSTS", "")
    return set(LOOPBACK) | {name.strip().lower() for name in extra.split(",")
                            if name.strip()}


class Handler(BaseHTTPRequestHandler):
    server_version = "marquee"
    app = None

    def log_message(self, *_args):
        """Quiet: the interesting output is the job log, not an access log."""

    # -- helpers ------------------------------------------------------------ #

    def _authorised(self, query):
        if not self.app.token:
            # No key on localhost -- so the Host header is what stands between the
            # API and DNS rebinding: a page on evil.example re-pointed at 127.0.0.1 is
            # same-origin, and could read the settings and start a transfer.
            return _host_name(self.headers.get("Host")) in _allowed_hosts()
        given = self.headers.get("X-Token") or (query.get("token") or [None])[0]
        return bool(given) and hmac.compare_digest(str(given), str(self.app.token))

    def _refuse(self):
        if self.app.token:
            self._send_json({"error": "Bad or missing key."}, 401)
        else:
            # Not a key problem, and a 401 would have the page ask for one.
            self._send_json({"error": "This server only answers to localhost. Add "
                                      "the name to MARQUEE_ALLOWED_HOSTS to reach it "
                                      "through another."}, 403)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Never cached. A folder listing or a plan served from the browser's cache
        # shows a state of the disk that is minutes old, and looks like a bug in the
        # app rather than in the cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name, content_type=None):
        # Resolved and then checked against the static root: without this, a request
        # for /static/../../settings.ini would be served happily.
        target = os.path.realpath(os.path.join(STATIC, name.lstrip("/")))
        if not target.startswith(os.path.realpath(STATIC) + os.sep) \
                and target != os.path.realpath(os.path.join(STATIC, "index.html")):
            self.send_error(403)
            return
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or _content_type(target))
        self.send_header("Content-Length", str(len(body)))
        # The page is reloaded constantly during use; letting a stale module linger
        # would be far more confusing than re-reading a few kilobytes.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_art(self, tail):
        """Serve a cached picture, or point at the source when it is not cached yet.

        Redirecting rather than fetching keeps the page fast: a screen of sixty
        posters would otherwise wait on sixty round trips through this process. The
        bulk download is what fills the cache; until then the browser gets the same
        image it always did, straight from the source.
        """
        kind, _, name = tail.partition("/")
        name = unquote(name)
        if kind not in art.KINDS or not name.endswith(".png"):
            self.send_error(404)
            return

        description = name[:-len(".png")]
        if not description:
            self.send_error(404)
            return
        path = art.path_for(description, kind)
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(302)
        self.send_header("Location", art.url_for(description, kind))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _fail(self, error):
        """Answer an unexpected error instead of dropping the connection.

        Without this, anything the handler did not anticipate escaped into
        BaseHTTPRequestHandler, which logs a traceback and closes the socket with no
        reply -- and the browser reports only "NetworkError when attempting to fetch
        resource", which says nothing about what went wrong.
        """
        detail = f"{type(error).__name__}: {error}"
        traceback.print_exc()
        try:
            self._send_json({"error": detail}, 500)
        except OSError:
            pass

    @staticmethod
    def _chosen_set(plan, which):
        """Which catalogue a listing is about: the library, or what was left out of it.

        The same rows, the same filters and the same tree, pointed at a different
        plan -- rather than a second set of endpoints that would drift from the first.
        """
        if which != "left-out":
            return plan
        return None if plan is None else plan.left_out

    @staticmethod
    def _number(raw, fallback=0):
        """A query parameter that has to be a number, without a traceback when it is not.

        `?offset=abc` used to escape as a ValueError and answer 500 with a Python
        exception in it, which tells the reader nothing and looks like a crash.
        """
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    # A request body is a JSON object of settings or a list of machine names. A
    # megabyte holds a hundred thousand of those.
    MAX_BODY = 4 * 1024 * 1024

    def _body(self):
        length = self._number(self.headers.get("Content-Length") or "0", fallback=-1)
        if length < 0 or length > self.MAX_BODY:
            # A non-numeric or negative length used to be a 500 or a handler that sat
            # waiting on rfile.read(-1) until the client went away.
            raise MarqueeError("Bad Content-Length.")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type.lower() != "application/json":
            # The one request shape a page on another origin can send without a
            # preflight is a form or text/plain POST -- and with no token on
            # localhost, that page could otherwise start a transfer or a download.
            # Asked of an empty body too: `fetch(url, {method: "POST", mode:
            # "no-cors"})` sends no body at all, and used to start a transfer.
            raise MarqueeError("Expected application/json.")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as error:
            raise MarqueeError(f"Bad request body: {error}") from error
        # Every handler reads the body as an object; `[]` or `null` used to reach
        # them and come back as a 500 with a Python traceback in it.
        if not isinstance(body, dict):
            raise MarqueeError("Expected a JSON object.")
        return body

    # -- routes ------------------------------------------------------------- #

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # The shell -- markup, CSS, script -- and the health probe are served without a
        # key. The browser fetches those itself and a <script src> carries no query
        # string, so guarding them made the page impossible to load at all whenever a
        # key was in force, which in a container is always. The key guards /api/,
        # where everything that matters lives.
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "version": __version__,
                             "build": self.app.about()["build"],
                             "auth": bool(self.app.token)})
            return
        if parsed.path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            self._send_file(parsed.path[len("/static/"):])
            return
        if parsed.path.startswith("/art/"):
            self._send_art(parsed.path[len("/art/"):])
            return

        if not self._authorised(query):
            self._refuse()
            return

        try:
            if parsed.path == "/api/state":
                after = self._number((query.get("after") or ["0"])[0])
                # The plan and the settings are 130 KB of a 137 KB answer on a real
                # library and change only when something is planned or saved; the page
                # names the revisions it holds and is sent only what moved.
                payload = self.app.job.snapshot(
                    after, self.app.release_sizes(),
                    plan_rev=(query.get("plan_rev") or [None])[0])
                config_rev = self.app.config_rev()
                payload["config_rev"] = config_rev
                if config_rev is None or (query.get("config_rev") or [None])[0] != config_rev:
                    payload["config"] = self.app.config_payload()
                payload["survey"] = {"running": self.app.survey["running"],
                                     "data": self.app.survey["data"],
                                     "error": self.app.survey.get("error")}
                payload["versions"] = self.app.versions
                payload["release_watch"] = self.app.release_watch()
                payload["destination"] = self.app.destination_payload()
                payload["catalogue"] = self.app.catalogue_payload()
                payload["app"] = self.app.about()
                self._send_json(payload)
                return
            if parsed.path == "/api/machine":
                plan = self.app.job.plan
                name = (query.get("name") or [""])[0]
                found = (machine_detail(plan, name, self.app.release_sizes())
                         if plan is not None else None)
                if found is None:
                    self._send_json({"error": f"No machine named {name!r}."}, 404)
                    return
                self._send_json(found)
                return
            if parsed.path == "/api/reclaim":
                self._send_json(self.app.reclaim_payload())
                return
            if parsed.path == "/api/upgrade":
                self._send_json(self.app.upgrade_payload())
                return
            if parsed.path == "/api/art":
                self._send_json(self.app.art_payload())
                return
            if parsed.path == "/api/missing":
                self._send_json(self.app.missing_payload())
                return
            if parsed.path == "/api/releases":
                self._send_json(self.app.releases_payload())
                return
            if parsed.path == "/api/queue":
                self._send_json(self.app.queue_payload())
                return
            if parsed.path == "/api/browse":
                self._send_json(self.app.browse((query.get("path") or [""])[0]))
                return
            if parsed.path == "/api/changes":
                one = lambda key, fallback="": (query.get(key) or [fallback])[0]  # noqa: E731
                self._send_json(changes_payload(
                    self.app.job.plan, kind=one("kind"), query=one("q"),
                    offset=self._number(one("offset", "0")),
                    limit=self._number(one("limit", "500"), 500),
                    sizes=self.app.release_sizes(),
                    version=getattr(self.app.job.resolution, "xml_version", "") or "",
                    category=one("category"), group=one("group") == "1"))
                return
            if parsed.path == "/api/left-out":
                self._send_json(left_out_payload(
                    self.app.job.plan, self.app.release_sizes(),
                    reason=(query.get("reason") or [""])[0],
                    query=(query.get("q") or [""])[0],
                    condition=(query.get("condition") or [""])[0]))
                return
            if parsed.path == "/api/selection":
                plan = self.app.job.plan
                if plan is None:
                    self._send_json({"total": 0, "machines": []})
                    return
                one = lambda key: (query.get(key) or [""])[0]  # noqa: E731
                plan = self._chosen_set(plan, one("set"))
                if plan is None:
                    self._send_json({"total": 0, "machines": []})
                    return
                self._send_json(machine_keys(
                    plan, self.app.release_sizes(),
                    query=one("q"), status=one("status"), genre=one("genre"),
                    category=one("category"), mature=one("mature"), have=one("have"),
                    condition=one("condition"), reason=one("reason"),
                    state=one("state"), with_excluded=one("excluded") == "1"))
                return
            if parsed.path == "/api/machines":
                plan = self.app.job.plan
                if plan is None:
                    self._send_json({"total": 0, "offset": 0, "rows": []})
                    return
                one = lambda key, fallback="": (query.get(key) or [fallback])[0]  # noqa: E731
                plan = self._chosen_set(plan, one("set"))
                if plan is None:
                    self._send_json({"total": 0, "offset": 0, "bytes": 0, "rows": []})
                    return
                self._send_json(machine_rows(
                    plan, query=one("q"), status=one("status"), genre=one("genre"),
                    category=one("category"),
                    offset=self._number(one("offset", "0")),
                    limit=self._number(one("limit", "200"), 200),
                    sort=one("sort", "size"), descending=one("dir", "desc") == "desc",
                    mature=one("mature"), have=one("have"),
                    condition=one("condition"), reason=one("reason"),
                    state=one("state"), with_excluded=one("excluded") == "1",
                    sizes=self.app.release_sizes(), console=one("console")))
                return
        except MarqueeError as error:
            self._send_json({"error": str(error)}, 400)
            return
        except Exception as error:  # noqa: BLE001 - see _fail
            self._fail(error)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorised(parse_qs(parsed.query)):
            self._refuse()
            return

        routes = {"/api/save": self.app.save, "/api/plan": self.app.plan,
                  "/api/ignore": self.app.ignore,
                  "/api/copy": self.app.copy,
                  "/api/check": self.app.check, "/api/cancel": self.app.cancel,
                  "/api/survey": self.app.start_survey,
                  "/api/versions": self.app.start_versions,
                  "/api/import": self.app.import_settings,
                  "/api/client/test": self.app.test_client,
                  "/api/client/category/reset": self.app.reset_category,
                  "/api/destination/test": self.app.test_destination,
                  "/api/releases/refresh": self.app.refresh_releases,
                  "/api/art/download": self.app.start_art,
                  "/api/missing/fetch": self.app.fetch_missing,
                  "/api/reclaim": self.app.reclaim,
                  "/api/download": self.app.download_selection,
                  "/api/catalogue": self.app.prime_catalogue,
                  "/api/left-out/restore": self.app.restore,
                  "/api/upgrade/preview": self.app.preview_upgrade,
                  "/api/art/stop": self.app.stop_art}
        action = routes.get(parsed.path)
        if action is None:
            self.send_error(404)
            return
        try:
            self._send_json(action(self._body()))
        except MarqueeError as error:
            self._send_json({"error": str(error)}, 400)
        except Exception as error:  # noqa: BLE001 - see _fail
            self._fail(error)



API_KEY_NAME = "api_key"


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def _content_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                             "application/octet-stream")


API_KEY_ENV = "MARQUEE_API_KEY"


def api_key(settings_path):
    """A token that survives a restart, kept beside the settings.

    A fresh token on every start means every bookmark, every reverse proxy and every
    script breaks whenever the container is restarted -- which, in a container, is
    often. Written 0600 because it is a password.

    `MARQUEE_API_KEY` in the environment wins over the file and is never written to
    it: on a NAS the container's log is the awkward place to fish a key out of, and
    the environment is where its settings already live.
    """
    chosen = (os.environ.get(API_KEY_ENV) or "").strip()
    if chosen:
        return chosen
    directory = os.path.dirname(os.path.abspath(
        configuration.settings_file(settings_path) or "."))
    path = os.path.join(directory, API_KEY_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(16)
    try:
        os.makedirs(directory, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except OSError:
        # An unwritable config directory is not a reason to refuse to start; the
        # token simply will not survive the restart.
        pass
    return token


# How long a stop waits for a running transfer to reach the end of its current file.
# Docker sends SIGKILL ten seconds after SIGTERM unless told otherwise.
STOP_GRACE = 8


def shutdown(app, grace=STOP_GRACE):
    """Ask a running transfer or check to stop, and give it `grace` seconds to.

    It stops between files, writes the destination's record as a stopped run, and
    whatever was mid-copy stays a .part that the next transfer clears away.
    Returns whether the job had come to rest.
    """
    app.job.cancel()
    deadline = time.monotonic() + grace
    while app.job.busy and time.monotonic() < deadline:
        time.sleep(0.1)
    return not app.job.busy


def _terminate(_signum, _frame):
    raise KeyboardInterrupt


def serve(settings_path, host="127.0.0.1", port=8777, open_browser=False):
    """Run until interrupted. Returns nothing; prints where to point a browser."""
    token = None if host in LOOPBACK else api_key(settings_path)

    app = Application(settings_path, token)
    # Plan straight away when there is something to plan. Without this every restart
    # leaves the library, the selection tree and the wanted list empty until somebody
    # notices and presses Build plan.
    started = app.autoplan()
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)

    shown = "localhost" if host in LOOPBACK else host
    url = f"http://{shown}:{port}/" + (f"?token={token}" if token else "")
    print(f"Marquee web UI on {url}")
    if started["started"]:
        print("Building a plan from the saved settings.")
    else:
        print(f"Not planning yet: {started['reason']}.")
    if token:
        print("Listening beyond this machine, so a token is required. Anyone who reaches "
              "this port with it can read paths and start copies -- keep it off untrusted "
              "networks.")
    print("Ctrl-C to stop.")

    if open_browser:
        import webbrowser
        webbrowser.open(url)

    # `docker stop` sends SIGTERM; tini, PID 1 in the image, passes it on. With no
    # handler Python died of it on the spot (exit 143): a transfer stopped mid-file,
    # with no record of the run written. Now it is a stop between files (exit 0).
    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        pass                      # not the main thread; nothing to install into

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if app.job.busy and not shutdown(app):
            print("The transfer had not reached the end of its file; the next one "
                  "carries on from there.")
        httpd.server_close()
