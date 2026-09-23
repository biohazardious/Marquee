"""settings.ini as the page sees it: reading, merging, saving, and the library it names."""
import os

from .. import backends, manifest
from .. import config as configuration
from ..errors import MarqueeError
from .downloads import _filters
from .views import filtered


class SettingsMixin:
    """Part of `Application`; see marquee.web.application."""

    def current_config(self):
        """The settings as they are on disk right now.

        Cached on the file's mtime and size: one /api/state used to parse the file
        five times, and the blacklists inside it are thousands of names.
        """
        try:
            stat = os.stat(self.settings_path)
            # The inode too: every save is an atomic replace, and on a filesystem
            # with coarse timestamps two same-size saves inside a second kept the
            # first one's settings.
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        cache = self._config_cache
        if stamp is not None and cache["stamp"] == stamp and cache["config"] is not None:
            return cache["config"]
        config = configuration.load(self.settings_path)
        self._config_cache = {"stamp": stamp, "config": config}
        return config

    def config_rev(self):
        """A revision for the settings as the page sees them: the file's stamp, which
        every save moves. None when there is no file to stamp."""
        self.current_config()
        stamp = self._config_cache.get("stamp")
        return None if stamp is None else "-".join(str(part) for part in stamp)

    def destination_payload(self, copy_path=None):
        """What the destination says it already holds, if anything."""
        record = manifest.read(copy_path or self.current_config().copy_path)
        described = manifest.describe(record)
        if described:
            described["settings"] = manifest.settings_from(record)
            described["history"] = (record.get("history") or [])[-5:]
        return described

    def test_destination(self, body):
        """Reach the library and read its top level, before anything is planned.

        The folder picker can only walk this machine's disks; a share on the console
        cannot be browsed, and the Browse button used to wander into a local folder
        literally named "smb:" instead of saying so. This is the answer for a remote
        library: connect, list, say what is there.
        """
        saved = self.current_config().copy_path
        copy_path = backends.unmask(((body or {}).get("copy_path") or saved
                                     or "").strip(), saved)
        if not copy_path:
            raise MarqueeError("No library path to test.")
        remote = backends.is_remote(copy_path)
        backend = None
        try:
            backend = backends.for_destination(copy_path)
            names = backend.probe()
        except (MarqueeError, OSError, ValueError, ConnectionError) as error:
            raise MarqueeError(f"Could not reach {backends.redact(copy_path)}: "
                               f"{error}") from error
        except Exception as error:  # noqa: BLE001 - pysmb/paramiko raise their own
            raise MarqueeError(f"Could not reach {backends.redact(copy_path)}: "
                               f"{type(error).__name__}: {error}") from error
        finally:
            if backend is not None:
                backend.close()
        folders = [name for name in names if "." not in name][:6]
        managed = sum(1 for name in names if backends.is_managed(name))
        where = (f"{backend.server}, share {backend.share_name}" if remote and hasattr(backend, "share_name")
                 else copy_path)
        message = (f"Reached {where}: {len(names):,} entries at the top"
                   + (f" ({', '.join(folders)}{'…' if len(names) > 6 else ''})" if folders else "")
                   + (f", {managed:,} loose ROM files" if managed else "") + ".")
        return {"ok": True, "remote": remote, "entries": len(names), "sample": folders,
                "message": message,
                "writable": None if remote else os.access(copy_path, os.W_OK)}

    # -- putting something back --------------------------------------------- #

    def restore(self, body):
        """Take machines off the blacklist.

        The only reason a machine can be lifted off the left-out list one at a time.
        The other five are the tool's own rules -- a preliminary driver, a BIOS set --
        and overriding those is a setting, not a per-game act.

        Excluding a game used to be a one-way door: it was dropped before anything else
        ran, so it appeared in no list, no search and no tree, and the only way back was
        editing settings.ini by hand.
        """
        plan = self.job.plan
        if plan is None or plan.left_out is None:
            raise MarqueeError("Build a plan first, so there is something to look at.")

        body = body or {}
        names = body.get("machines")
        if names is None:
            chosen = filtered(plan.left_out, **_filters(body.get("filters") or {}))
            names = [item.name for item in chosen]

        excluded = {item.name for item in plan.left_out.wanted
                    if item.reason == "blacklisted"}
        putting_back = sorted(set(names) & excluded)
        if not putting_back:
            raise MarqueeError("None of those were excluded by name. The rest are left "
                               "out by the working filters, not by your settings.")

        with self._settings_lock:
            config = self.current_config()
            gone = set(putting_back)
            remaining = [name for name in config.blacklist_roms if name not in gone]
            configuration.write_settings_file(
                self.settings_path, config.with_overrides(blacklist_roms=remaining))
        return {"restored": len(putting_back), "remaining": len(remaining),
                "replan": True}

    def import_settings(self, body):
        """Read the filters a destination was built with back into the form."""
        described = self.destination_payload(body.get("copy_path") or None)
        if not described:
            raise MarqueeError("That folder has no Marquee record to import from.")
        return {"imported": described.get("settings") or {},
                "mame_version": described.get("mame_version")}

    def config_payload(self):
        config = self.current_config()
        return {
            "rom_dir": config.rom_dir or "",
            "chd_dir": config.chd_dir or "",
            # A share's password rides inside the URL; hidden like the client's.
            "copy_path": backends.redact(config.copy_path or ""),
            "allow_mature": config.allow_mature,
            "parents_only": config.parents_only,
            "write_gamelist": config.write_gamelist,
            "copy_artwork": config.copy_artwork,
            "mature_rom_folder": config.mature_rom_folder,
            "blacklist_genres": config.blacklist_genres,
            "blacklist_categories": config.blacklist_categories,
            "blacklist_roms": config.blacklist_roms,
            "settings_path": self.settings_path,
            "mame_version": config.mame_version,
            "download_client": config.download_client or "",
            "download_username": config.download_username or "",
            # Present so the form can show it is set, without handing it back out.
            "download_password": "••••••••" if config.download_password else "",
            "download_dir": config.download_dir or "",
            "remote_path_mappings": config.remote_path_mappings or "",
            # "auto", true or false as saved; and what that means right now, so the
            # form can say "Automatic -- on" rather than leave it to be guessed.
            "hardlink": "auto" if config.hardlink is None else bool(config.hardlink),
            "hardlink_effective": backends.effective_hardlink(config),
        }

    def config_from(self, body):
        """Merge what the page sent over the saved settings.

        A field the page did not send is left alone, but one it sent empty is treated as
        cleared -- otherwise clearing a path in the form and pressing Plan would quietly
        run against the old saved value.
        """
        overrides = {}
        for key in ("rom_dir", "chd_dir", "copy_path", "download_client",
                    "download_username", "download_dir", "remote_path_mappings",
                    "mature_rom_folder"):
            if key in body:
                value = body[key]
                if value is not None and not isinstance(value, str):
                    raise MarqueeError(f"{key} should be text.")
                overrides[key] = (value or "").strip()
        # A share's password is masked on the way out too; the mask coming back onto
        # the same server means the saved one.
        if overrides.get("copy_path"):
            overrides["copy_path"] = backends.unmask(overrides["copy_path"],
                                                     self.current_config().copy_path)
        # The password is masked on the way out, so the mask coming back means
        # "unchanged" rather than "set it to a row of dots".
        if "download_password" in body and not isinstance(
                body["download_password"], (str, type(None))):
            raise MarqueeError("download_password should be text.")
        if "download_password" in body and set(body["download_password"] or "") != {"\u2022"}:
            overrides["download_password"] = body["download_password"]
        for key in ("allow_mature", "parents_only", "write_gamelist", "copy_artwork"):
            if body.get(key) is not None:
                overrides[key] = body[key]
        hardlink = body.get("hardlink")
        if hardlink is not None and hardlink != "auto":
            overrides["hardlink"] = bool(hardlink)
        # A blacklist is only ever taken from something that is actually a list. The
        # page omits them entirely until it has read the saved ones, and anything else
        # arriving here is a bug somewhere, not an instruction to clear them.
        for key in ("blacklist_genres", "blacklist_categories", "blacklist_roms"):
            if isinstance(body.get(key), list):
                overrides[key] = body[key]
        # The chosen version belongs to the configuration too: it is what gets saved and
        # what the destination's record ends up naming.
        version = (body.get("mame_version") or "").strip() if "mame_version" in body else None
        if version:
            overrides["mame_version"] = version
        config = self.current_config().with_overrides(**overrides)
        if hardlink == "auto":
            # with_overrides ignores None, which is exactly what automatic is.
            config.hardlink = None
        if "mame_version" in body and not version:
            # The form's "Automatic": with_overrides ignores None, so cleared has to be
            # said explicitly or the old choice would survive every save.
            config.mame_version = None
        return config

    # -- actions ------------------------------------------------------------ #

    def save(self, body):
        with self._settings_lock:
            config = self.config_from(body)
            missing = config.missing_paths
            if missing:
                raise MarqueeError(f"Still missing: {', '.join(missing)}")
            configuration.write_settings_file(self.settings_path, config)
        return {"saved": self.settings_path}

    def _saved_since(self, moment):
        """Whether settings.ini was written after `moment` (a time.time())."""
        if moment is None:
            return False
        try:
            return os.stat(self.settings_path).st_mtime > moment
        except OSError:
            return False

    def browse(self, path):
        """Directory listing, so paths can be picked instead of typed.

        A directory that cannot be read is reported as a note on an otherwise valid
        answer rather than as a failure: the picker has to stay usable when someone
        wanders into /root on the way somewhere else.
        """
        path = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
        if not os.path.isdir(path):
            path = os.path.dirname(path) or "/"

        names, note = [], None
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            names.append(entry.name)
                    except OSError:
                        pass
            names.sort(key=str.lower)
        except PermissionError:
            note = "No permission to read this folder."
        except OSError as error:
            note = f"Could not read this folder: {error.strerror or error}"

        parent = os.path.dirname(path.rstrip(os.sep)) or "/"
        return {"path": path, "parent": parent if parent != path else None,
                # Both shapes: `directories` is the bare names, `entries` carries the
                # full path so the picker does not have to join them itself and get
                # the separator wrong.
                "directories": names,
                "entries": [{"name": name, "path": os.path.join(path, name)}
                            for name in names],
                "note": note,
                "writable": os.access(path, os.W_OK),
                "roots": _roots()}


def _roots():
    """Places worth starting from, so nobody has to type their way out of /config.

    In a container the interesting paths are the mounted volumes, and there is no
    home directory to fall back on.
    """
    found = []
    for candidate in ("/downloads", "/library", "/config", os.path.expanduser("~"), "/"):
        if candidate and os.path.isdir(candidate) and candidate not in found:
            found.append(candidate)
    return found
