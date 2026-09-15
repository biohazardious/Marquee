"""Download clients.

A client's whole job is: take a release and a set of wanted files, and get exactly
those bytes onto disk. Nothing here knows what a MAME machine is.
"""
from ..errors import ConfigError


class DownloadClient:
    """The interface the acquisition pipeline relies on."""

    def test(self):
        """Prove the client is reachable and authenticated. Returns a description."""
        raise NotImplementedError

    def add(self, source, save_path, category=None, stopped=True):
        """Add a torrent (magnet URI or .torrent bytes). Returns its infohash."""
        raise NotImplementedError

    def files(self, infohash):
        """[{index, path, size, priority, piece_range, progress}] for one torrent."""
        raise NotImplementedError

    def select_only(self, infohash, indices):
        """Download only these file indices, and nothing else."""
        raise NotImplementedError

    def start(self, infohash):
        raise NotImplementedError

    def status(self, infohashes=None):
        """[{hash, name, state, progress, downloaded, size, speed, eta, save_path}]."""
        raise NotImplementedError


def for_url(url, **kwargs):
    if not url:
        raise ConfigError("No download client configured.")
    from .qbittorrent import QBittorrent
    return QBittorrent(url, **kwargs)
