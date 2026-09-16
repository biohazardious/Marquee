/* Everything that did not make it into the library, in the same shape as the library.

   Six reasons, and they are not alike. Five of them are the tool's own filters -- a
   preliminary driver, a BIOS set, a prototype -- and are shown so that "where did the
   other 4,323 machines go" has an answer somewhere. The sixth is the games you
   excluded by name, and those can be put back: excluding one used to be a one-way
   door, because it was dropped before anything else ran and appeared in no list, no
   search and no tree. */

import { icon } from './icons.js';
import { $, append, badges, clear, count, debounce, el, human } from './util.js';
import { api, machines as fetchMachines, post, refresh } from './api.js';

const INDENT = [10, 26, 42, 58];
const ROWS = 500;

/* Short enough to sit in a badge. The System page spells them out in full. */
const LABELS = {
  blacklisted: 'excluded by name',
  screenless: 'no screen',
  'not-a-game': 'not a game',
  'proto-beta': 'proto / beta',
  'beta-bootleg': 'beta bootleg',
  'not-working': 'does not run',
};

const V = {
  reasons: [], genres: [], cats: [], rows: {}, loading: new Set(),
  open: new Set(), openCats: new Set(),
  reason: '', query: '', condition: '', total: 0, everything: 0, bytesHuman: '',
  imperfectExclusions: 0,
  adultFolder: 'ZZ-Adult', loaded: false,
};

export function adopt(config) {
  if (config && config.mature_rom_folder) V.adultFolder = config.mature_rom_folder;
}

let generation = 0;

export async function load() {
  const host = $('leftOutBody');
  if (!host) return;
  const mine = (generation += 1);
  if (!V.loaded) host.append(el('div', { class: 'muted', style: 'padding:14px', text: 'Loading…' }));
  try {
    const payload = await api('/api/left-out?'
      + new URLSearchParams({
        reason: V.reason, q: V.query, condition: V.condition,
      }).toString());
    if (mine !== generation) return;
    V.reasons = payload.reasons || [];
    V.genres = payload.genres || [];
    V.cats = payload.categories || [];
    V.total = payload.total || 0;
    // The reason tally is always over everything, filters or not -- which makes it
    // the only honest answer to "is this empty because nothing was left out, or
    // because of what I just typed".
    V.everything = V.reasons.reduce((sum, entry) => sum + entry.count, 0);
    V.bytesHuman = payload.bytes_human || '';
    V.imperfectExclusions = payload.imperfect_exclusions || 0;
    V.loaded = true;
  } catch (error) {
    if (mine !== generation) return;
    clear(host);
    host.append(el('div', { class: 'banner bad', text: error.message }));
    return;
  }
  render();
}

const wantedOf = (cat) => cat.wanted ?? cat.machines ?? 0;
const weightOf = (cat) => cat.wanted_bytes ?? cat.bytes ?? 0;

function catsOf(genre, mature) {
  return V.cats.filter((cat) => cat.genre === genre
    && Boolean(cat.mature) === Boolean(mature));
}

function totals(cats) {
  return cats.reduce((sum, cat) => ({
    wanted: sum.wanted + wantedOf(cat),
    bytes: sum.bytes + weightOf(cat),
  }), { wanted: 0, bytes: 0 });
}

function branchRow({ depth, label, open, onOpen, cats }) {
  const sum = totals(cats);
  return el('div', {
    class: `row ${depth === 0 ? 'genre' : 'cat'}`,
    style: `padding-left:${INDENT[depth]}px`,
    onclick: onOpen,
  },
    el('span', { class: 'twist', text: open ? '▾' : '▸' }),
    el('span', { class: 'name', text: label }),
    el('span', { class: 'count', text: count(sum.wanted) }),
    el('span', { class: 'size', text: human(sum.bytes) }));
}

function putBack(names, label) {
  const button = el('button', { class: 'btn sm', text: label });
  button.onclick = async (event) => {
    event.stopPropagation();
    button.disabled = true;
    button.textContent = 'Putting back…';
    try {
      const done = await post('/api/left-out/restore',
        names ? { machines: names } : {
          filters: { reason: 'blacklisted', condition: V.condition, query: V.query },
        });
      button.textContent = `${count(done.restored)} back — build a plan`;
      // The file changed; the selection page follows it on the next snapshot.
      refresh().catch(() => {});
    } catch (error) {
      button.disabled = false;
      button.textContent = error.message;
    }
  };
  return button;
}

function gameRow(m, depth) {
  const mine = m.reason === 'blacklisted';
  const row = el('div', {
    class: 'row game',
    style: `padding-left:${INDENT[Math.min(depth, 3)]}px`,
  },
    el('span', { class: 'twist' }),
    el('span', { class: 'name' },
      el('span', { text: m.description }),
      el('small', { text: m.name })),
    // Without the sync state: every one of these is absent by definition, and a
    // "missing" badge on all 7,556 says nothing.
    el('span', { class: 'rowflex' }, badges({ ...m, status: '' }),
      el('span', {
        class: `badge ${mine ? 'flaw' : 'keep'}`,
        text: LABELS[m.reason] || (m.reason || '').replace(/-/g, ' '),
      })),
    el('span', { class: 'size', text: m.bytes_human || '—' }));
  if (mine) append(row, putBack([m.name], 'Put back'));
  return row;
}

async function loadCategory(name) {
  if (V.rows[name] || V.loading.has(name)) return;
  V.loading.add(name);
  render();
  try {
    const found = await fetchMachines({
      set: 'left-out', category: name, reason: V.reason, q: V.query,
      condition: V.condition, limit: ROWS, sort: 'size', dir: 'desc',
    });
    V.rows[name] = found.rows;
  } finally {
    V.loading.delete(name);
    render();
  }
}

function categoryRows(cat, depth) {
  const open = V.openCats.has(cat.name);
  const swap = () => {
    if (open) V.openCats.delete(cat.name);
    else { V.openCats.add(cat.name); loadCategory(cat.name); }
    render();
  };
  const rows = [branchRow({
    depth, label: cat.label || cat.name, open, onOpen: swap, cats: [cat],
  })];
  if (!open) return rows;
  if (!V.rows[cat.name] && !V.loading.has(cat.name)) {
    // Open, but its rows were dropped when the filter changed. Fetch them again --
    // leaving it empty made every open category read "nothing here" the moment the
    // dropdown moved, which looked exactly like a filter that does not work.
    loadCategory(cat.name);
  }
  if (V.loading.has(cat.name)) {
    rows.push(el('div', { class: 'row game dim', text: 'loading…' }));
    return rows;
  }
  const games = V.rows[cat.name] || [];
  games.forEach((m) => rows.push(gameRow(m, depth + 1)));
  if (!games.length) {
    rows.push(el('div', {
      class: 'row game dim',
      style: `padding-left:${INDENT[Math.min(depth + 1, 3)]}px`,
      text: 'Nothing here for that reason.',
    }));
  }
  return rows;
}

function groupRows(genre, mature, depth) {
  const cats = catsOf(genre, mature);
  if (!cats.length) return [];
  const key = `${mature ? '18+' : ''}${genre}`;
  const open = V.open.has(key);
  const swap = () => { open ? V.open.delete(key) : V.open.add(key); render(); };
  const rows = [branchRow({ depth, label: genre, open, onOpen: swap, cats })];
  if (open) cats.forEach((cat) => rows.push(...categoryRows(cat, depth + 1)));
  return rows;
}

/* The same shape as the library: the genres, then one branch named after the adult
   folder holding the same genres and categories underneath it. */
function treeNodes() {
  const nodes = [];
  for (const genre of V.genres) {
    const rows = groupRows(genre.name, false, 0);
    if (rows.length) nodes.push(rows);
  }
  const adult = V.cats.filter((cat) => cat.mature);
  if (!adult.length) return nodes;

  const open = V.open.has(' adult');
  const swap = () => {
    open ? V.open.delete(' adult') : V.open.add(' adult');
    render();
  };
  const rows = [branchRow({
    depth: 0, label: V.adultFolder, open, onOpen: swap, cats: adult,
  })];
  if (open) {
    V.genres.map((genre) => genre.name)
      .filter((name) => adult.some((cat) => cat.genre === name))
      .forEach((name) => rows.push(...groupRows(name, true, 1)));
  }
  nodes.push(rows);
  return nodes;
}

/* The filter bar, built once and never rebuilt.

   It used to be created fresh on every render, and render runs after every fetch --
   so typing a letter replaced the box being typed into, focus and caret and all. One
   letter in, the search was over. The controls are kept as nodes and only their
   contents are refreshed. */
const BAR = {};

function controls() {
  if (BAR.root) return BAR.root;

  BAR.search = el('input', {
    type: 'text', placeholder: 'Find a game…', id: 'leftOutSearch',
  });
  BAR.search.oninput = debounce(() => {
    V.query = BAR.search.value.trim();
    V.rows = {};
    load();
  });

  BAR.condition = el('select', {
    onchange: () => { V.condition = BAR.condition.value; V.rows = {}; load(); },
  }, [
    ['', 'Any condition'],
    ['good', 'Fully emulated'],
    ['flawed', 'Imperfect'],
  ].map(([value, label]) => el('option', { value, text: label })));

  BAR.reason = el('select', {
    onchange: () => { V.reason = BAR.reason.value; V.rows = {}; load(); },
  });

  BAR.count = el('b');
  BAR.bytes = el('span', { class: 'muted' });
  BAR.action = el('span');

  BAR.root = el('div', { class: 'rowflex libsum' },
    BAR.count, BAR.bytes,
    el('span', { style: 'flex:1' }),
    el('span', { class: 'search' }, icon('search'), BAR.search),
    BAR.condition, BAR.reason, BAR.action);
  return BAR.root;
}

function refreshControls() {
  BAR.count.textContent = `${count(V.total)} machines`;
  BAR.bytes.textContent = V.bytesHuman || '';

  clear(BAR.reason);
  BAR.reason.append(el('option', {
    value: '', text: `Every reason (${count(V.everything)})`,
  }));
  for (const entry of V.reasons) {
    BAR.reason.append(el('option', {
      value: entry.reason, text: `${entry.label} (${count(entry.count)})`,
    }));
  }
  BAR.reason.value = V.reason;
  BAR.condition.value = V.condition;
  // Never while it is being typed into: the value on screen is the newer one.
  if (document.activeElement !== BAR.search && BAR.search.value !== V.query) {
    BAR.search.value = V.query;
  }

  clear(BAR.action);
  const mine = V.reasons.some((entry) => entry.reason === 'blacklisted');
  if (mine && V.reason === 'blacklisted' && V.total) {
    BAR.action.append(putBack(null, `Put all ${count(V.total)} back`));
  }
}

function clearFilters() {
  V.query = '';
  V.reason = '';
  V.condition = '';
  // Directly, not through the refresh: that leaves the box alone while it has the
  // focus, which is right for a fetch landing mid-word and wrong for the one button
  // whose entire purpose is to empty it.
  if (BAR.search) BAR.search.value = '';
  V.rows = {};
  load();
}

export function render() {
  const host = $('leftOutBody');
  if (!host) return;

  // Nothing to filter: no plan yet, or a plan that left nothing out at all.
  if (!V.loaded || !V.everything) {
    BAR.root = null;
    clear(host);
    host.append(el('div', { class: 'empty' },
      el('div', { class: 'big', text: '\u{1f5c3}' }),
      el('div', {
        text: V.loaded
          ? 'Nothing was left out.'
          : 'Build a plan first and whatever it leaves out will be listed here.',
      })));
    return;
  }

  // The bar stays mounted; only what is under it is redrawn. Taking it out of the
  // document and putting it back would lose the focus just as surely as rebuilding it.
  if (BAR.root?.parentNode !== host) {
    clear(host);
    host.append(controls(), el('div', { id: 'leftOutRest' }));
  }
  refreshControls();
  const rest = $('leftOutRest');
  clear(rest);

  if (!V.total) {
    rest.append(el('div', { class: 'empty' },
      el('div', { class: 'big', text: '\u2315' }),
      el('div', { text: 'Nothing left out matches that.' }),
      el('button', {
        class: 'btn sm', text: 'Clear the filters', onclick: clearFilters,
        style: 'margin-top:12px',
      })));
    return;
  }

  // Where a big exclude list came from. "Leave all out" on a filtered view writes
  // every name it matched and keeps no record of the rule, so this is the only way
  // back to recognising them -- and to undoing exactly that group.
  const shared = V.reason === 'blacklisted' && !V.condition && V.imperfectExclusions
    ? el('div', { class: 'banner info' },
      `${count(V.imperfectExclusions)} of these are imperfectly emulated `
      + '— which is what one “Leave all out” on the Library’s Imperfect filter does. ',
      el('button', {
        class: 'btn sm', text: 'Show just those',
        onclick: () => { V.condition = 'flawed'; V.rows = {}; load(); },
      }))
    : null;

  const panel = el('div', { class: 'panel' },
    el('h2', {}, 'Left out',
      el('span', { class: 'sub', text: 'what is not in the library, and why' })),
    el('div', { class: 'body tight' },
      el('div', { class: 'tree', id: 'leftOutTree' })));
  append(rest, shared, panel,
    el('div', {
      class: 'hint',
      style: 'padding:0 2px',
      text: 'Five of these are the working filters: a driver that does not run, a BIOS '
        + 'set, a prototype. The sixth is your own exclude list \u2014 which is what '
        + 'the Selection page writes when you untick a game, one at a time or in bulk '
        + '\u2014 and only those can be put back here. They return to the library on '
        + 'the next plan.',
    }));

  const tree = $('leftOutTree');
  for (const node of treeNodes()) tree.append(...node);
}
