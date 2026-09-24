"""Whether a remote library answers at all -- the cheap probe behind planning again
once the console is switched back on."""
import socket

import pytest

from marquee import backends


@pytest.mark.parametrize("url, expected", [
    ("smb://192.168.1.20/Batocera3/roms/mame/", ("192.168.1.20", (445, 139))),
    ("smb://user:p@ss:w@rd@nas.local/share", ("nas.local", (445, 139))),
    ("smb://user@nas:1445/share/mame", ("nas", (1445,))),
    ("ftp://user:pw@host/roms", ("host", (21,))),
    ("ftps://host:990/roms", ("host", (990,))),
    ("sftp://root@192.168.1.20/userdata/roms", ("192.168.1.20", (22,))),
    ("ssh://root@box:2222/roms", ("box", (2222,))),
    ("/run/media/library", None),
])
def test_address(url, expected):
    assert backends.address(url) == expected


def test_a_listening_port_answers():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert backends.answers(f"smb://127.0.0.1:{port}/share", timeout=2)
    finally:
        listener.close()


def test_a_closed_port_does_not():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert not backends.answers(f"smb://127.0.0.1:{port}/share", timeout=2)


def test_a_local_folder_is_not_probed():
    assert not backends.answers("/run/media/library")
