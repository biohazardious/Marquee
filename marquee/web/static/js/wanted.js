/* Wanted: the games the selection asks for that are not on disk yet.

   A partial set is the normal state of things -- the ROM set is 44,166 files and
   nobody downloads all of it at once -- so "what am I missing, and what would it
   cost" deserves a page rather than a wall of names in a log. */

import { icon } from './icons.js';
import { $, append, clear, count, el, human, plural, sortControl } from './util.js';
import { api, lock, post } from './api.js';

let data = null;
let expanded = false;

/* The games and the genre table in one order, sorted by the server so "largest
   first" orders all of them rather than the page that came back. */
const sorter = sortControl('wanted', [
  ['name', 'Name', 'asc'], ['size', 'Size', 'desc'], ['genre', 'Genre', 'asc'],
  ['year', 'Year', 'asc'],
], () => load(), ['size', 'desc']);

const missingUrl = () => `/api/missing?${new URLSearchParams(sorter.get()).toString()}`;

export async function load() {
  if (lock.on) return;
  try {
    data = await api(missingUrl());
  } catch (error) {
    data = { error: error.message };
  }
  render();
  loadUpgrade();
  // The release list is fetched lazily by the server; if it was not ready, come back
  // for it once rather than leaving the panel saying "fetch it in Settings".
  if (data && !data.error && !(data.releases || []).length) {
    setTimeout(async () => {
      try {
        const again = await api(missingUrl());
        if ((again.releases || []).length) { data = again; render(); }
      } catch { /* the indexer is optional */ }
    }, 2500);
  }
}

export function render() {
  const host = $('wantedBody');
  if (!host) return;
  clear(host);

  if (!data) { host.append(el('div', { class: 'muted', text: 'Loading…' })); return; }
  if (data.error) { host.append(el('div', { class: 'banner bad', text: data.error })); return; }

  if (!data.total) {
    host.append(el('div', { class: 'empty' },
      el('div', { class: 'big' }, icon('check')),
      el('div', { text: 'Nothing missing — every game the selection wants is on disk.' })));
    // A complete library is exactly the one that gets moved to a newer release, so
    // the panel that prices that cannot hide behind "nothing missing".
    host.append(upgradePanel());
    renderUpgrade();
    return;
  }

  // One number is the point of this page; the other two qualify it.
  host.append(el('div', { class: 'cards scoreboard' },
    stat('Games missing', count(data.total), null, { lead: true }),
    stat('To download', sizeLabel(data), sizeNote(data), { quiet: !data.bytes }),
    stat('Disks to fetch', count((data.disks || []).length))));

  host.append(upgradePanel());
  host.append(fetchPanel());
  host.append(releasePanel());
  host.append(genrePanel());
  host.append(listPanel());
  // Redrawn with the page, or "Show all" wiped the estimate that had just arrived.
  renderUpgrade();
}

/* "0.289" after "0.288", and "0.300" after both: a string sort would not know. */
const versionKey = (text) => String(text || '').split('.').map((part) => Number(part) || 0);
const newer = (one, other) => {
  const a = versionKey(one);
  const b = versionKey(other);
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
  }
  return false;
};

/* Moving the library to a newer release.

   The point of the whole project: only the machines this library keeps are
   considered, and only the ones whose bytes actually changed are fetched. Seven
   releases cost the same as one. */
let upgrade = null;

export async function loadUpgrade() {
  try { upgrade = await api('/api/upgrade'); } catch { upgrade = null; }
  renderUpgrade();
}

function renderUpgrade() {
  const host = $('upgradeBody');
  if (!host) return;
  clear(host);
  if (!upgrade) return;

  const here = upgrade.library_version || upgrade.configured_version;
  // Newer only. Older releases were offered too, and "move to 0.250" is not a
  // question anyone with a 0.289 library is asking.
  const others = (upgrade.available || [])
    .filter((version) => version !== here && (!here || newer(version, here)));

  host.append(el('div', { class: 'muted', style: 'margin-bottom:12px' },
    here ? `This library holds MAME ${here}.` : 'This library does not say which release it holds.'));

  if (!others.length) {
    host.append(el('div', { class: 'muted', text: here
      ? 'Nothing newer is published.'
      : 'Fetch the release list in Settings to see what is available.' }));
    return;
  }

  const pick = el('select', { style: 'width:auto' }, others.map((version) =>
    el('option', { value: version, text: `MAME ${version}` })));

  host.append(el('div', { class: 'rowflex' }, pick,
    el('button', {
      class: 'btn', text: 'What would it cost?',
      onclick: async (event) => {
        event.target.disabled = true;
        try {
          await post('/api/upgrade/preview', { version: pick.value });
          upgrade = { ...upgrade, running: true, error: null };
          renderUpgrade();
          poll();
        } catch (error) {
          event.target.disabled = false;
          upgrade = { ...upgrade, error: error.message };
          renderUpgrade();
        }
      },
    })));

  if (upgrade.running) {
    host.append(el('div', { class: 'muted', style: 'margin-top:12px',
      text: `Comparing ${here} with ${upgrade.to}… this reads both releases’ catalogues.` }));
  }
  if (upgrade.error) {
    host.append(el('div', { class: 'banner bad', style: 'margin-top:12px',
      text: upgrade.error }));
  }
  if (upgrade.data) host.append(result(upgrade.data));
}

function result(data) {
  const box = el('div', { style: 'margin-top:14px' });
  box.append(el('div', { class: 'cards' },
    stat('Already correct', count(data.unchanged)),
    stat('Changed', count(data.changed)),
    stat('Now working', count(data.now_working || 0)),
    stat('New in this release', count(data.new || 0)),
    data.missing ? stat('Not in the library yet', count(data.missing)) : null,
    data.leaving ? stat('Leaving', count(data.leaving)) : null));

  // Which ones, by name: "35 now working" is a number, "Daytona USA" is a reason.
  const named = (label, rows) => (rows && rows.length
    ? el('div', { class: 'hint', style: 'margin-bottom:8px' },
      el('b', { text: `${label}: ` }),
      rows.map((row) => row.description || row.name).join(', '),
      rows.length >= 12 ? '…' : '')
    : null);
  append(box,
    named(`Did not run in ${data.from}, runs in ${data.to}`, data.now_working_examples),
    named(`New in ${data.to}`, data.new_examples),
    data.leaving
      ? el('div', { class: 'hint', style: 'margin-bottom:10px' },
        el('b', { text: `Leaving the selection: ` }),
        (data.leaving_examples || []).map((row) => row.description || row.name).join(', '),
        (data.leaving_examples || []).length >= 12 ? '…' : '',
        ` — MAME ${data.to} no longer runs them or no longer has them. They stay in the `
        + 'library until you tick “Also delete” on the Transfer page.')
      : null);

  if (!data.fetch.length) {
    box.append(el('div', { class: 'banner good',
      text: `Nothing to fetch — every game is the same in ${data.to}.` }));
    return box;
  }

  const outcome = el('div');
  append(box,
    el('div', { class: 'banner info' },
      `${plural(data.fetch.length, 'game', 'games')} to fetch to move from ${data.from} to ${data.to}. `
      + `The other ${count(data.unchanged)} are already the right bytes on disk. `
      + `Then set MAME ${data.to} in Settings and build the plan: disks, and the transfer, follow from it.`),
    data.examples.length
      ? el('div', { class: 'hint', style: 'margin-bottom:10px',
          text: `Changed, for instance: ${data.examples.join(', ')}` })
      : null,
    el('button', {
      class: 'btn primary', text: `Fetch ${plural(data.fetch.length, 'game', 'games')}`,
      onclick: async (event) => {
        event.target.disabled = true;
        clear(outcome);
        try {
          const done = await post('/api/missing/fetch', { machines: data.fetch, version: data.to });
          outcome.append(el('div', { class: 'banner good' },
            `Asked for ${plural(done.selected, 'file', 'files')} (${done.bytes_human}). `
            + `${count(done.raised)} newly selected. Watch Activity.`));
        } catch (error) {
          outcome.append(el('div', { class: 'banner bad', text: error.message }));
        }
      },
    }),
    outcome);
  return box;
}

let upgradeTimer = null;

function poll() {
  if (upgradeTimer) return;
  upgradeTimer = setInterval(async () => {
    await loadUpgrade();
    if (!upgrade || !upgrade.running) {
      clearInterval(upgradeTimer);
      upgradeTimer = null;
    }
  }, 1500);
}

function upgradePanel() {
  return el('div', { class: 'panel' },
    el('h2', {}, 'Move to a newer release',
      el('span', { class: 'sub', text: 'only what actually changed' })),
    el('div', { class: 'body', id: 'upgradeBody' }));
}

/* The download will land where qBittorrent puts it. When that place is not visible
   from here, the files arrive and the library step finds nothing -- so it is said
   at the moment of asking, with both paths. */
function landingNote(answer) {
  if (answer.path_visible !== false) return null;
  return el('div', { class: 'banner warn', style: 'margin-top:10px' },
    `qBittorrent writes to ${answer.client_path || 'its download folder'}`,
    answer.path && answer.path !== answer.client_path ? `, which this app looks for at ${answer.path}` : '',
    ' — and cannot see it. Mount that folder into this container, or add a remote path '
    + 'mapping (qBittorrent’s path -> this app’s path) in Settings → Download client. '
    + 'The download itself is not affected.');
}

/* Asking the download client for what is missing. Two steps on purpose: the first
   says what it would cost and which release it comes from, the second commits. */
/* One quote for everything the selection wants and has not got: zips from the ROM
   set, disks from the CHD set. It goes through /api/download with no filter -- the
   same road as the Library page's Download button -- because the older
   /api/missing/fetch only knew about zips, and a library short of 112 disks showed
   "nothing to fetch" for as long as every zip was accounted for. */
function fetchPanel() {
  const result = el('div', { id: 'fetchResult' });

  const check = el('button', {
    class: 'btn', text: 'What would it cost?',
    onclick: async () => {
      clear(result);
      result.append(el('div', { class: 'muted', text: 'Asking the download client…' }));
      try {
        const answer = await post('/api/download', { filters: {}, dry_run: true });
        clear(result);
        if (answer.nothing) {
          result.append(el('div', { class: 'banner good', text: answer.message }));
          return;
        }
        const parts = answer.parts || [];
        append(result,
          el('div', { class: 'banner info' },
            `${answer.bytes_human} over the wire: `
            + `${count(answer.machines)} ROM${answer.machines === 1 ? '' : 's'}`
            + (answer.disks ? ` and ${count(answer.disks)} disk${answer.disks === 1 ? '' : 's'}` : '')
            + ` — the published sets total ${answer.set_bytes_human}.`),
          el('table', { class: 'grid', style: 'margin:12px 0' },
            el('tbody', {}, parts.map((part) => el('tr', { style: 'cursor:default' },
              el('td', {}, el('b', { text: part.kind === 'chds' ? 'Disks' : 'ROMs' }),
                el('small', { class: 'dim', text: part.release || '' })),
              el('td', { class: 'num nowrap', text: part.error ? '—' : `${plural(part.files, 'file', 'files')}` }),
              el('td', { class: 'num nowrap', text: part.error ? '' : part.bytes_human }))))),
          // The count belongs with the price; the names and the reason belong with the
          // download, where Activity follows it and says why they will not come.
          ...parts.filter((part) => part.missing_count).map((part) => el('div', { class: 'hint', text:
            `${count(part.missing_count)} not in ${part.release}. Once it starts, Activity `
            + 'lists them and says why.' })),
          ...parts.filter((part) => part.error).map((part) => el('div', { class: 'banner warn', text: part.error })),
          ...parts.filter((part) => !part.error).map((part) => landingNote(part)),
          el('button', {
            class: 'btn primary', style: 'margin-top:10px', text: 'Download them',
            onclick: async (event) => {
              event.target.disabled = true;
              try {
                const done = await post('/api/download', { filters: {} });
                clear(result);
                append(result, el('div', { class: 'banner good' },
                  `Queued — ${done.bytes_human} across ${(done.parts || []).length} torrent(s). `
                  + 'Watch Activity; when it has finished, build a plan and transfer.'),
                  ...(done.parts || []).filter((part) => !part.error).map((part) => landingNote(part)));
              } catch (error) {
                clear(result);
                result.append(el('div', { class: 'banner bad', text: error.message }));
              }
            },
          }));
      } catch (error) {
        clear(result);
        result.append(el('div', { class: 'banner bad', text: error.message }));
      }
    },
  });

  return el('div', { class: 'panel' },
    el('h2', {}, 'Fetch them',
      el('span', { class: 'sub', text: 'through your download client' })),
    el('div', { class: 'body' },
      el('p', { class: 'muted', style: 'margin-top:0' },
        'Only the missing files are selected in the release torrents — zips from the '
        + 'ROM set, disks from the CHD set. Files you already have are never '
        + 'deselected, so anything you are seeding keeps seeding.'),
      check,
      result));
}

function sizeLabel(data) {
  if (data.bytes) return human(data.bytes);
  if (data.priced) return 'nothing to fetch';
  return 'unknown';
}

function sizeNote(data) {
  if (data.bytes) return null;
  if (data.priced) return 'the release does not carry these machines';
  if (data.client) return 'the release torrent is not in your client — Fetch them will add it';
  return 'connect a download client to price it';
}

function stat(key, value, note, { lead = false, quiet = false } = {}) {
  return el('div', { class: `card ${lead ? 'lead' : ''}` },
    el('div', { class: 'k', text: key }),
    el('div', { class: `v ${quiet ? 'quiet' : 'accent'}`, text: value }),
    note ? el('div', { class: 'n', text: note }) : null);
}

function releasePanel() {
  const full = (data.releases || []).filter((release) => release.full_set);
  const body = el('div', { class: 'body' });
  if (!full.length) {
    body.append(el('div', { class: 'muted' },
      'Fetch the release list in Settings to see which sets these files come from.'));
  } else {
    body.append(el('table', { class: 'grid' },
      el('tbody', {}, full.map((release) => el('tr', { style: 'cursor:default' },
        el('td', {}, el('b', { text: release.name })),
        el('td', { class: 'mono muted', text: release.version }),
        el('td', { class: 'mono dim', style: 'font-size:11px',
          text: release.infohash.slice(0, 16) }))))));
  }
  return el('div', { class: 'panel' },
    el('h2', {}, 'Where these come from',
      el('span', { class: 'sub', text: 'Pleasuredome full sets' })), body);
}

function genrePanel() {
  return el('div', { class: 'panel' },
    el('h2', {}, 'By genre'),
    el('div', { class: 'body tight' },
      el('table', { class: 'grid' },
        el('thead', {}, el('tr', {},
          el('th', { text: 'Genre' }),
          el('th', { class: 'num', text: 'Games' }),
          el('th', { class: 'num', text: 'Size' }))),
        el('tbody', {}, data.genres.map((genre) => el('tr', { style: 'cursor:default' },
          el('td', { text: genre.name }),
          el('td', { class: 'num', text: count(genre.machines) }),
          el('td', { class: 'num nowrap', text: genre.bytes_human || '-' })))))));
}

/* Which half of the game is wanted. A zip already in the library whose disk is not
   is a common case -- the zip came with a ROM set, the disk never did -- and a row
   that just says the name reads as if the whole game were missing. */
function partsOf(row) {
  const disks = row.disks || [];
  if (!disks.length) return '';
  const what = disks.length === 1 ? 'disk ' + disks[0] : `${disks.length} disks`;
  return row.zip === false ? ` · ${what} only` : ` · + ${what}`;
}

function listPanel() {
  const rows = expanded ? data.rows : data.rows.slice(0, 25);
  return el('div', { class: 'panel' },
    el('h2', {}, 'Games',
      el('span', { class: 'sub',
        text: data.shown < data.total
          ? `showing ${count(data.shown)} of ${count(data.total)}`
          : count(data.total) }),
      el('span', { class: 'spacer' }),
      sorter.node,
      el('button', {
        class: 'btn sm', text: expanded ? 'Show fewer' : 'Show all',
        onclick: () => { expanded = !expanded; render(); },
      })),
    el('div', { class: 'body tight' },
      el('table', { class: 'grid' },
        el('tbody', {}, rows.map((row) => el('tr', { style: 'cursor:default' },
          el('td', { class: 'title-cell' },
            el('b', { text: row.description }),
            el('small', { text: row.name + partsOf(row) })),
          el('td', { class: 'muted', text: row.category || row.genre }),
          el('td', { class: 'num nowrap', text: row.bytes_human || '-' })))))));
}
