# Contributing

Thanks for looking. Marquee is small enough that a change can be understood end to end,
and it tries to stay that way.

## Running it from a checkout

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]' pyflakes
marquee --web --port 8777                       # http://127.0.0.1:8777
python -m pytest -q                             # the whole suite, a few minutes
python -m pyflakes marquee tests
```

The front end is plain ES modules under `marquee/web/static`: no build step, no
framework. `docs/ARCHITECTURE.md` explains how the pieces fit.

## What makes a change easy to take

- A test that fails before it and passes after. The suite builds small romsets on disk
  and fake download clients and shares; see `tests/conftest.py`.
- Figures from a real set where behaviour depends on real data. Most of the bugs this
  project has fixed were only visible against a real library.
- Comments that say why, in the style of the code around them.

## What cannot be accepted

ROMs, CHDs, BIOS files, or links to where they can be downloaded -- in code, tests,
issues or discussions. Marquee works with sets you already have or fetch yourself.
