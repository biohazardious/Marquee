"""The browser's side of sorting, run under node: the trees order their branches the
same way the server orders rows."""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.join(HERE, "..", "marquee", "web", "static", "js", "tree.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

SCRIPT = """
import { orderCats, orderGenres, gameOrder } from 'file://%s';
const cats = [
  { name: 'Maze / Misc', label: 'Misc', genre: 'Maze', wanted_bytes: 10 },
  { name: 'Maze / Escape', label: 'escape', genre: 'Maze', wanted_bytes: 50 },
  { name: 'Shooter / Flying', label: 'Flying', genre: 'Shooter', bytes: 30 },
];
console.log(JSON.stringify({
  catsBySize: orderCats(cats, { sort: 'size', dir: 'desc' }).map((c) => c.label),
  catsByName: orderCats(cats, { sort: 'name', dir: 'asc' }).map((c) => c.label),
  genresBySize: orderGenres(['Shooter', 'Maze'], cats, { sort: 'size', dir: 'desc' }),
  genresByNameDown: orderGenres(['Maze', 'Shooter'], cats, { sort: 'name', dir: 'desc' }),
  ties: orderCats(cats.map((c) => ({ ...c, wanted_bytes: 0, bytes: 0 })),
                  { sort: 'size', dir: 'desc' }).map((c) => c.label),
  games: [gameOrder({ sort: 'size', dir: 'desc' }), gameOrder({ sort: 'name', dir: 'asc' })],
}));
""" % os.path.abspath(TREE)


def test_trees_order_every_level():
    out = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], capture_output=True,
                         text=True, check=True).stdout
    got = json.loads(out)
    # A category weighs what it shows: wanted_bytes when there is one, else bytes.
    assert got["catsBySize"] == ["escape", "Flying", "Misc"]
    assert got["catsByName"] == ["escape", "Flying", "Misc"], "case does not decide"
    # A genre weighs its categories together: Maze 60, Shooter 30.
    assert got["genresBySize"] == ["Maze", "Shooter"]
    assert got["genresByNameDown"] == ["Shooter", "Maze"]
    # Largest first, and equal weights still A to Z rather than turned round.
    assert got["ties"] == ["escape", "Flying", "Misc"]
    assert got["games"] == [{"sort": "size", "dir": "desc"},
                            {"sort": "description", "dir": "asc"}]
