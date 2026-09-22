/* The library: every machine the current plan covers, as posters or as a table,
   with a detail panel behind each one. */

import { icon } from './icons.js';
import { $, append, badges, banner, clear, count, debounce, el, hero, human, pressable, shot, store, stored, subtitle } from './util.js';
import { api, lock, machine as fetchMachine, machines as fetchMachines, post, state } from './api.js';

const V = {
  view: stored('marquee.view', 'posters'),
  art: stored('marquee.art', 'Named_Titles'),
  query: '', genre: '', status: '', category: '', mature: '', have: '', condition: '',
  state: '',
  sort: 'description', dir: 'asc',
  offset: 0, limit: 60, total: 0, bytes: 0, rows: [], loading: false,
};

const PAGE = { posters: 60, table: 150 };

function setQuery(text) {
  V.query = text;
  V.offset = 0;
  load();
}

let generation = 0;

export async function load() {
  const host = $('libraryBody');
  if (!host || lock.on) return;
  // Requests can land out of order: a slow broad search used to overwrite the
  // narrower filter chosen after it. Only the latest one is allowed to draw.
  const mine = (generation += 1);
  V.loading = true;
  V.limit = PAGE[V.view];
  host.classList.add('loading');
  try {
    const found = await fetchMachines({
      q: V.query, genre: V.genre, status: V.status, mature: V.mature, have: V.have,
      condition: V.condition, state: V.state,
      sort: V.sort, dir: V.dir, offset: V.offset, limit: V.limit,
    });
    if (mine !== generation) return;
    V.rows = found.rows;
    V.total = found.total;
    V.bytes = found.bytes || 0;
  } catch (error) {
    if (mine !== generation) return;
    clear(host);
    host.append(el('div', { class: 'banner bad', text: error.message }));
    return;
  } finally {
    if (mine === generation) {
      V.loading = false;
      host.classList.remove('loading');
    }
  }
  render();
}

export function clearFilters() {
  V.query = ''; V.genre = ''; V.status = ''; V.mature = '';
  V.have = ''; V.condition = ''; V.state = '';
  V.offset = 0;
  const search = $('libSearch');
  if (search) search.value = '';
  for (const id of ['libGenre', 'libStatus', 'libAdult', 'libHave',
    'libCondition', 'libState']) {
    if ($(id)) $(id).value = '';
  }
  load();
}

function poster(m) {
  // The badges go inside the picture rather than in a wrapper around it. A .shot
  // nested in another .shot was centred by the outer grid instead of filling it, and
  // with the image absolutely positioned there was nothing left in flow to give the
  // inner one a width -- so it collapsed to nothing the moment the image loaded.
  const art = shot(m, V.art, cacheBust);
  art.append(el('div', { class: 'corner' }, badges(m).slice(0, 2)));
  return pressable(el('div', { class: `poster ${m.here ? '' : 'absent'}`, onclick: () => open(m.name) },
    art,
    el('div', { class: 'meta' },
      el('b', { title: m.description, text: m.description }),
      el('small', { text: `${subtitle(m) || m.name}${m.bytes ? ` · ${m.bytes_human}` : ''}` }))), () => open(m.name));
}

const COLUMNS = [
  { key: 'description', label: 'Game', sortable: true },
  { key: 'year', label: 'Year', sortable: true, cls: 'num' },
  { key: 'manufacturer', label: 'Manufacturer', sortable: true },
  { key: 'category', label: 'Category', sortable: true },
  { key: 'players', label: 'Players', cls: 'num' },
  { key: 'flags', label: '' },
  { key: 'size', label: 'Size', sortable: true, cls: 'num' },
];

function table() {
  const head = el('tr', {}, COLUMNS.map((column) => {
    const cell = el('th', {
      class: `${column.cls || ''} ${column.sortable ? 'sortable' : ''}`,
      onclick: column.sortable ? () => sortBy(column.key) : null,
      'aria-sort': V.sort === column.key
        ? (V.dir === 'desc' ? 'descending' : 'ascending') : null,
      text: column.label + (V.sort === column.key ? (V.dir === 'desc' ? ' ↓' : ' ↑') : ''),
    });
    // Sortable from the keyboard as well as by mouse.
    return column.sortable ? pressable(cell, () => sortBy(column.key)) : cell;
  }));

  const body = V.rows.map((m) => pressable(el('tr', { class: m.here ? '' : 'absent', onclick: () => open(m.name) },
    el('td', { class: 'title-cell' },
      el('b', { text: m.description }),
      el('small', { text: m.name })),
    el('td', { class: 'num dim', text: m.year || '-' }),
    el('td', { class: 'muted', text: m.manufacturer || '-' }),
    el('td', { class: 'muted', text: m.category }),
    el('td', { class: 'num dim', text: m.players || '-' }),
    el('td', { class: 'rowflex' }, badges(m)),
    el('td', { class: 'num nowrap', text: m.bytes ? m.bytes_human : '—' })), () => open(m.name)));

  return el('table', { class: 'grid' },
    el('thead', {}, head),
    el('tbody', {}, body));
}

function sortBy(key) {
  const column = key === 'size' ? 'size' : key;
  if (V.sort === column) V.dir = V.dir === 'desc' ? 'asc' : 'desc';
  else { V.sort = column; V.dir = column === 'description' || column === 'manufacturer' ? 'asc' : 'desc'; }
  V.offset = 0;
  load();
}

function pager() {
  const from = V.total ? V.offset + 1 : 0;
  const to = Math.min(V.offset + V.limit, V.total);
  return el('div', { class: 'rowflex', style: 'padding:14px 18px;border-top:1px solid var(--line)' },
    el('span', { class: 'muted', text: `${count(from)}–${count(to)} of ${count(V.total)}` }),
    el('span', { style: 'flex:1' }),
    el('button', {
      class: 'btn sm', disabled: V.offset === 0,
      onclick: () => { V.offset = Math.max(0, V.offset - V.limit); load(); },
      text: '‹ Previous',
    }),
    el('button', {
      class: 'btn sm', disabled: to >= V.total,
      onclick: () => { V.offset += V.limit; load(); },
      text: 'Next ›',
    }));
}

export function render() {
  const host = $('libraryBody');
  if (!host) return;
  clear(host);

  if (!V.rows.length) {
    const plan = state.data.plan || {};
    // Three different reasons for an empty page, and each needs its own sentence:
    // nothing has been checked, the genre is on the exclude list, or the filters
    // simply meet nowhere -- in which case they are named, each droppable alone.
    const checked = Object.keys(plan.states || {}).length;
    const unchecked = V.state && !checked;
    const outGenre = V.genre && (plan.genres || []).find(
      (genre) => genre.name === V.genre && genre.excluded);
    const active = chips();
    const empty = el('div', { class: 'empty' },
      el('div', { class: 'big', text: unchecked ? '🔍' : '🕹' }),
      el('div', { text: unchecked
        ? 'Nothing has looked inside the files yet, so no game has a check result.'
        : outGenre
          ? `${V.genre} is left out of the library, so none of its games are here.`
          : active.length > 1
            ? 'No game matches all of these at once.'
            : active.length
              ? 'Nothing matches that.'
              : 'No plan yet — set your folders and build one.' }));
    if (unchecked) {
      const button = el('button', { class: 'btn primary sm', text: 'Check the library',
        style: 'margin-top:12px' });
      button.onclick = async () => {
        button.disabled = true;
        button.textContent = 'Checking…';
        try { await post('/api/check', {}); } catch (error) { button.textContent = error.message; }
      };
      empty.append(button);
    } else if (outGenre) {
      empty.append(el('div', { class: 'rowflex', style: 'justify-content:center;margin-top:12px' },
        el('a', { class: 'btn sm primary', href: '#selection', text: 'Put it back on the Selection page' }),
        active[0]));
    } else if (active.length) {
      empty.append(el('div', { class: 'rowflex', style: 'justify-content:center;margin-top:12px' },
        active,
        active.length > 1
          ? el('button', { class: 'btn sm ghost', text: 'Clear all', onclick: clearFilters })
          : null));
    }
    host.append(empty);
    return;
  }

  host.append(summary());
  host.append(V.view === 'posters'
    ? el('div', { class: 'posters', style: 'padding:18px' }, V.rows.map(poster))
    : el('div', { style: 'overflow-x:auto' }, table()));
  host.append(pager());
}

function filtering() {
  return Boolean(V.query || V.genre || V.status || V.mature || V.have
    || V.condition || V.state);
}

/* What the current filter adds up to. This is the line the download decision is made
   on, so it says what the selection weighs and how much of it is already here. */
function summary() {
  const here = V.rows.filter((row) => row.here).length;
  const note = V.bytes
    ? `${human(V.bytes)}`
    : 'sizes unknown until the release is read';
  return el('div', { class: 'rowflex libsum' },
    el('b', { text: `${count(V.total)} ${V.total === 1 ? 'game' : 'games'}` }),
    el('span', { class: 'muted', text: note }),
    V.have !== 'yes' && here < V.rows.length
      ? el('span', { class: 'badge missing', text: 'includes games not downloaded' })
      : null,
    el('span', { style: 'flex:1' }),
    filtering()
      ? el('button', { class: 'btn sm ghost', text: 'Clear filters', onclick: clearFilters })
      : null);
}

/* Artwork -------------------------------------------------------------------
   The pictures come from the source until they are on disk, so downloading them is
   about speed and working offline rather than about seeing anything new. When a run
   finishes the grid is redrawn, because the images it is holding are now local. */
let artTimer = null;

export async function artStatus() {
  try {
    const status = await api('/api/art');
    // Navigating away and back must not lose sight of a run that is still going.
    if (status && status.running) watchArt();
    return status;
  } catch {
    return null;
  }
}

async function startArt(kind) {
  await post('/api/art/download', { kind });
  watchArt();
}

export function watchArt() {
  if (artTimer) return;
  artTimer = setInterval(async () => {
    const status = await artStatus();
    renderArtBar(status);
    // No answer counts as finished: on an error the timer used to run for ever.
    if (!status || !status.running) {
      clearInterval(artTimer);
      artTimer = null;
      // The browser is still holding redirects to the source; a redraw picks up the
      // local copies.
      bustCache();
      render();
    }
  }, 1200);
}

let cacheBust = '';

function bustCache() { cacheBust = `?v=${Date.now()}`; }

export function renderArtBar(status) {
  const host = $('artBar');
  if (!host) return;
  clear(host);
  if (!status) return;

  if (status.running) {
    const percent = status.total ? Math.round((status.done / status.total) * 100) : 0;
    host.append(el('div', { class: 'panel', style: 'margin-bottom:14px' },
      el('div', { class: 'body' },
        el('div', { class: 'rowflex', style: 'margin-bottom:8px' },
          el('b', { text: 'Downloading artwork' }),
          el('span', { class: 'muted', text: `${count(status.done)} of ${count(status.total)}` }),
          el('span', { style: 'flex:1' }),
          el('button', { class: 'btn sm danger', text: 'Stop',
            onclick: () => post('/api/art/stop', {}).catch((error) => banner(error.message, 'bad')) })),
        el('div', { class: 'bar' }, el('i', { style: `width:${percent}%` })))));
    return;
  }

  if (status.summary) {
    const done = status.summary;
    host.append(el('div', { class: 'banner good' },
      `Artwork done — ${count(done.fetched)} downloaded, ${count(done.cached)} already `
      + `here, ${count(done.missing)} not published. ${status.bytes_human} on disk.`));
  }
  if (status.error) host.append(el('div', { class: 'banner bad', text: status.error }));
}

export function toolbar() {
  const search = el('input', {
    type: 'text', placeholder: 'Search games…', value: V.query,
    oninput: debounce((event) => setQuery(event.target.value)),
  });

  const genres = el('select', {
    onchange: (event) => { V.genre = event.target.value; V.offset = 0; load(); },
  }, el('option', { value: '', text: 'All genres' }));

  const statuses = el('select', {
    onchange: (event) => { V.status = event.target.value; V.offset = 0; load(); },
  },
    el('option', { value: '', text: 'Any action' }),
    // The same words the Transfer page uses. Two pages describing one run in two
    // vocabularies -- `keep` here, "Leave alone" there -- is a puzzle nobody asked for.
    // "Not downloaded" is the Have filter's job; offering it twice under two names
    // made the two look like they disagreed.
    [['new', 'Copy over'], ['update', 'Replace'], ['move', 'Move'],
      ['keep', 'Leave alone']].map(([kind, label]) =>
      el('option', { value: kind, text: label, selected: V.status === kind })));

  const have = el('select', {
    onchange: (event) => { V.have = event.target.value; V.offset = 0; load(); },
  }, [
    ['', 'All games'],
    ['no', 'Not downloaded'],
    ['yes', 'On disk'],
  ].map(([value, label]) => el('option', { value, text: label, selected: V.have === value })));

  const condition = el('select', {
    onchange: (event) => { V.condition = event.target.value; V.offset = 0; load(); },
  }, [
    ['', 'Any condition'],
    ['good', 'Fully emulated'],
    ['flawed', 'Imperfect'],
  ].map(([value, label]) => el('option', { value, text: label,
                                           selected: V.condition === value })));

  /* What checking the library against the release found. Empty until something has
     looked, which is why the choice says so rather than reading "current". */
  // Not `state`: this module imports the poll store under that name, and a <select>
  // shadowing it inside one function is a trap laid for the next edit.
  const checkResult = el('select', {
    onchange: (event) => { V.state = event.target.value; V.offset = 0; load(); },
  }, [
    ['', 'Any check result'],
    ['stale', 'Out of date'],
    ['incomplete', 'Missing an inherited ROM'],
    ['current', 'Verified current'],
    ['damaged', 'Damaged'],
    ['absent', 'File missing'],
  ].map(([value, label]) => el('option', { value, text: label,
                                          selected: V.state === value })));

  const adult = el('select', {
    onchange: (event) => { V.mature = event.target.value; V.offset = 0; load(); },
  }, [
    ['', 'Everything'],
    ['hide', 'Hide adult'],
    ['only', 'Adult only'],
  ].map(([value, label]) => el('option', { value, text: label, selected: V.mature === value })));

  const art = el('select', {
    onchange: (event) => {
      V.art = event.target.value;
      store('marquee.art', V.art);
      render();
    },
  }, [
    ['Named_Titles', 'Title screens'],
    ['Named_Snaps', 'In-game shots'],
    ['Named_Boxarts', 'Box art'],
  ].map(([value, label]) => el('option', { value, text: label, selected: V.art === value })));

  const getArt = el('button', {
    class: 'btn sm', text: 'Artwork',
    title: 'Download every picture so the library loads them from here, not from the internet',
    onclick: async (event) => {
      event.target.disabled = true;
      try { await startArt(V.art); } catch (error) { banner(error.message, 'bad'); } finally { event.target.disabled = false; }
    },
  });

  const views = el('div', { class: 'seg' },
    el('button', {
      class: V.view === 'posters' ? 'on' : '', text: 'Posters',
      onclick: () => setView('posters'),
    }),
    el('button', {
      class: V.view === 'table' ? 'on' : '', text: 'Table',
      onclick: () => setView('table'),
    }));

  const get = el('button', {
    class: 'btn primary sm', text: 'Download',
    title: 'Ask the download client for exactly the games these filters are showing',
    onclick: () => downloadDrawer(),
  });

  // Drawn icons, the same set the navigation uses.
  getArt.prepend(icon('download'));
  get.prepend(icon('download'));
  views.children[0].prepend(icon('overview'));
  views.children[1].prepend(icon('menu'));

  return { search, genres, statuses, adult, have, condition, state: checkResult,
    art, getArt, get,
           views };
}

function setView(view) {
  V.view = view;
  store('marquee.view', view);
  V.offset = 0;
  load();
}

export function fillGenres(select, plan) {
  if (!select || !plan) return;
  // The count is what the selection asks for, not what happens to be downloaded:
  // on a set nothing has arrived for yet, every genre would otherwise read (0).
  // A genre on the exclude list has nothing in the library, so it sits at the bottom
  // under its own heading rather than reading "Board Game (1)" among the others and
  // then answering with an empty page.
  const kept = (plan.genres || []).filter((genre) => !genre.excluded);
  const out = (plan.genres || []).filter((genre) => genre.excluded);
  const label = (genre) => `${genre.name} (${count(genre.wanted ?? genre.machines)})`;
  const signature = [...kept.map(label), '--', ...out.map(label)].join('|');
  // Rebuilt only when it would actually differ; the page polls every few seconds and
  // replacing the options under an open menu closes it in the reader's face.
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  const current = V.genre;
  clear(select);
  select.append(el('option', { value: '', text: 'All genres' }));
  kept.forEach((genre) => select.append(el('option', {
    value: genre.name, selected: genre.name === current, text: label(genre) })));
  if (out.length) {
    select.append(el('optgroup', { label: 'Left out of the library' },
      out.map((genre) => el('option', {
        value: genre.name, selected: genre.name === current, text: label(genre) }))));
  }
}

const FILTER_LABELS = {
  query: 'search', genre: 'genre', have: 'downloaded', condition: 'condition',
  state: 'check result', status: 'action', mature: 'adult',
};
const CONTROL_FOR = {
  query: 'libSearch', genre: 'libGenre', have: 'libHave', condition: 'libCondition',
  state: 'libState', status: 'libStatus', mature: 'libAdult',
};
const WORDS = {
  have: { yes: 'on disk', no: 'not downloaded' },
  condition: { good: 'fully emulated', flawed: 'imperfect' },
  mature: { hide: 'adult hidden', only: 'adult only' },
  status: { new: 'copy over', update: 'replace', move: 'move', keep: 'leave alone' },
  state: { stale: 'out of date', incomplete: 'missing an inherited ROM',
    current: 'verified current', damaged: 'damaged', absent: 'file missing' },
};

function describeFilter(key, value) {
  if (key === 'query') return `“${value}”`;
  return (WORDS[key] || {})[value] || value;
}

/* Every active filter as a chip that can be dropped on its own: with seven controls,
   "nothing matches" without saying which one emptied the page is a dead end. */
function chips() {
  return Object.entries(CONTROL_FOR)
    .filter(([key]) => V[key])
    .map(([key]) => {
      const drop = el('button', { class: 'btn sm', title: `Drop the ${FILTER_LABELS[key]} filter` },
        `${FILTER_LABELS[key]}: ${describeFilter(key, V[key])} ✕`);
      drop.onclick = () => {
        V[key] = '';
        V.offset = 0;
        const control = $(CONTROL_FOR[key]);
        if (control) { control.value = ''; control.classList.remove('active'); }
        load();
      };
      return drop;
    });
}

/* ---------------- detail drawer ---------------- */

function fact(term, value) {
  return value === null || value === undefined || value === '' || value === 0
    ? [] : [el('dt', { text: term }), el('dd', {}, value)];
}

/* A drawer is a dialog: it says so, takes focus when it opens, keeps Tab inside it,
   and hands focus back to whatever opened it. Before, a keyboard user opened a game
   and went on tabbing through the page hidden underneath. */
let returnFocus = null;

function drawerShell(label) {
  returnFocus = document.activeElement;
  const scrim = el('div', { class: 'scrim', onclick: close, id: 'drawerScrim' });
  const panel = el('aside', { class: 'drawer', id: 'drawer', role: 'dialog',
                              'aria-modal': 'true', 'aria-label': label, tabindex: '-1',
                              onkeydown: keepTabInside });
  return { scrim, panel };
}

function mount(scrim, panel) {
  document.body.append(scrim, panel);
  panel.focus();
}

function keepTabInside(event) {
  if (event.key !== 'Tab') return;
  const panel = event.currentTarget;
  const stops = [...panel.querySelectorAll(
    'a[href], button:not([disabled]), input, select, textarea, [tabindex="0"]')]
    .filter((node) => node.offsetParent !== null);
  if (!stops.length) { event.preventDefault(); return; }
  const first = stops[0];
  const last = stops[stops.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export async function open(name) {
  close();
  // Linkable from the library, where the drawer belongs. Opened from Selection it
  // used to rewrite the address to #library/<name>, and a reload then landed there.
  if (location.hash.startsWith('#library') && location.hash !== `#library/${name}`) {
    history.replaceState(null, '', `#library/${name}`);
  }
  const { scrim, panel } = drawerShell('Game details');
  panel.append(el('div', { class: 'scroll', text: 'Loading…' }));
  mount(scrim, panel);

  let m;
  try {
    m = await fetchMachine(name);
  } catch (error) {
    clear(panel);
    panel.append(el('div', { class: 'scroll' }, el('div', { class: 'banner bad', text: error.message })));
    return;
  }

  const controls = (m.controls || []).join(', ');
  const screen = [m.display, m.resolution, m.refresh ? `${m.refresh} Hz` : '',
    m.vertical ? 'vertical' : 'horizontal'].filter(Boolean).join(' · ');

  clear(panel);
  panel.append(
    el('header', {},
      el('div', {},
        el('h3', { text: m.description }),
        el('div', { class: 'sub', text: m.name })),
      el('button', { class: 'close', text: '×', onclick: close })),
    el('div', { class: 'scroll' },
      artFor(m),
      el('div', { class: 'rowflex', style: 'margin-bottom:16px' }, badges(m)),
      el('dl', { class: 'facts' },
        fact('Year', m.year),
        fact('Manufacturer', m.manufacturer),
        fact('Genre', m.genre),
        fact('Category', m.category),
        fact('Players', m.players),
        fact('Controls', [controls, m.buttons ? `${m.buttons} buttons` : ''].filter(Boolean).join(', ')),
        fact('Screen', screen),
        fact('Condition', conditionText(m)),
        fact('Checked', checkedText(m)),
        fact('Parent', m.parent ? el('a', { href: '#', text: m.parent,
          onclick: (event) => { event.preventDefault(); open(m.parent); } }) : ''),
        fact('Goes to', el('span', { class: 'mono', text: m.folder })),
        fact('Total size', m.bytes_human),
        fact('Signature', el('span', { class: 'mono dim', text: m.signature })),
      ),
      m.missing_disks && m.missing_disks.length
        ? el('div', { class: 'banner warn' },
            `${m.missing_disks.length} disk(s) not in the CHD set yet: ${m.missing_disks.join(', ')}`)
        : null,
      filesTable(m),
      m.clones && m.clones.length ? clonesBlock(m) : null));
}

function artFor(m) {
  return hero(m);
}

/* Two different answers to "where is it": what the download folder holds and is ready
   to copy, and what the library already holds. A machine that came from an older
   romset has the second and not the first, and reading only the first put "none of
   this machine's files are on disk yet" under a row marked `keep`. */
function filesTable(m) {
  const source = m.files || [];
  const library = m.library_files || [];
  if (!source.length && !library.length) {
    return el('div', { class: 'banner warn',
      text: 'None of this machine’s files are on disk yet.' });
  }
  const rows = source.length
    ? source.map((file) => [file.destination, file.bytes_human, ''])
    : library.map((file) => [file.destination, file.bytes_human,
      file.moving_to ? `→ ${file.moving_to}` : '']);
  return el('div', { class: 'panel', style: 'margin:0' },
    el('h2', {}, 'Files',
      el('span', { class: 'sub',
        text: source.length ? `${source.length} ready to copy`
          : `${library.length} in the library` })),
    el('div', { class: 'body tight' },
      el('table', { class: 'grid' },
        el('tbody', {}, rows.map(([path, size, moving]) =>
          el('tr', { style: 'cursor:default' },
            el('td', { class: 'mono', style: 'font-size:12px' },
              el('div', { text: path }),
              moving ? el('div', { class: 'dim', text: moving }) : null),
            el('td', { class: 'num nowrap', text: size })))))));
}

function clonesBlock(m) {
  return el('div', { style: 'margin-top:18px' },
    el('div', { class: 'muted', style: 'margin-bottom:8px', text: 'Other versions' }),
    el('div', { class: 'rowflex' }, m.clones.map((name) => el('button', {
      class: 'btn sm ghost', text: name, onclick: () => open(name),
    }))));
}

export function close() {
  const open = document.getElementById('drawer');
  document.getElementById('drawer')?.remove();
  document.getElementById('drawerScrim')?.remove();
  if (open && location.hash.startsWith('#library/')) {
    history.replaceState(null, '', '#library');
  }
  if (open && returnFocus && document.contains(returnFocus)) returnFocus.focus();
  if (open) returnFocus = null;
}

document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });

export { human };

/* Downloading what the filters are showing ----------------------------------
   Two passes, always. The first prices the selection -- which means putting the
   release in the download client stopped, reading its file table and touching none of
   its content -- and the second commits. Nobody should discover after the fact that a
   click added 1.3 TB to their queue. */

function currentFilters() {
  return { query: V.query, genre: V.genre, status: V.status, mature: V.mature,
           have: V.have, condition: V.condition, state: V.state,
           category: V.category || '' };
}

async function downloadDrawer() {
  close();
  const { scrim, panel } = drawerShell('Download these games');
  const body = el('div', { class: 'scroll' });
  panel.append(
    el('header', {},
      el('div', {}, el('h3', { text: 'Download these games' }),
        el('div', { class: 'sub', text: `${count(V.total)} in view` })),
      el('button', { class: 'close', text: '×', 'aria-label': 'Close', onclick: close })),
    body);
  mount(scrim, panel);

  clear(body);
  body.append(
    el('div', { class: 'muted', text: 'Reading the release…' }),
    el('div', { class: 'hint', text:
      'The set is added to the download client stopped, so only its list of files '
      + 'arrives. Nothing is fetched until you confirm.' }));

  let quote;
  try {
    quote = await post('/api/download', { filters: currentFilters(), dry_run: true });
  } catch (error) {
    clear(body);
    body.append(el('div', { class: 'banner bad', text: error.message }));
    return;
  }

  clear(body);
  if (quote.nothing) {
    body.append(el('div', { class: 'banner good', text: quote.message }));
    return;
  }

  const parts = quote.parts || [];
  body.append(
    el('div', { class: 'stat big-stat' },
      el('b', { text: quote.bytes_human }),
      el('span', { class: 'muted', text: `to download for ${count(quote.chosen)} games` })),
    el('div', { class: 'hint', text:
      `${count(quote.machines)} ROM${quote.machines === 1 ? '' : 's'}`
      + (quote.disks ? ` and ${count(quote.disks)} disk${quote.disks === 1 ? '' : 's'}` : '')
      + ` — the published sets total ${quote.set_bytes_human}.` }),
    el('table', { class: 'grid', style: 'margin:16px 0' },
      el('tbody', {}, parts.map((part) => el('tr', { style: 'cursor:default' },
        el('td', {}, el('b', { text: part.kind === 'chds' ? 'Disks' : 'ROMs' }),
          el('small', { class: 'dim', text: part.release || '' })),
        el('td', { class: 'num nowrap', text: part.error ? '—' : `${count(part.files)} files` }),
        el('td', { class: 'num nowrap', text: part.error ? '' : part.bytes_human })))))
  );

  const unavailable = parts.filter((part) => part.missing_count);
  if (unavailable.length) {
    body.append(el('div', { class: 'banner warn', text:
      unavailable.map((part) => `${part.missing_count} not in ${part.release}`).join('; ')
      + '. The CHD set trails the ROM set by a release or two, so a brand new machine’s '
      + 'disk is simply not published yet.' }));
  }
  for (const part of parts.filter((one) => one.error)) {
    body.append(el('div', { class: 'banner warn', text: part.error }));
  }

  const go = el('button', { class: 'btn primary', text: 'Download them' });
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = 'Asking the client…';
    try {
      const done = await post('/api/download', { filters: currentFilters() });
      clear(body);
      append(body,
        el('div', { class: 'banner good', text:
          `Queued — ${done.bytes_human} across ${(done.parts || []).length} torrent(s).` }),
        el('div', { class: 'hint', text:
          'Watch it on the Activity page. When it has finished, build a plan and '
          + 'transfer it into the library.' }),
        whereItLands(done.parts || []));
    } catch (error) {
      go.disabled = false;
      go.textContent = 'Download them';
      body.append(el('div', { class: 'banner bad', text: error.message }));
    }
  };
  body.append(el('div', { class: 'rowflex' }, go,
    el('button', { class: 'btn', text: 'Cancel', onclick: close })));
}

/* Where the client is putting it, and a way to point the library at it.

   On a fresh install nobody knows the folder's name in advance -- a torrent lands in
   one named after itself, below whatever save path the client is configured with --
   and a settings page cannot browse to a folder that does not exist yet. */
/* MAME's own verdict, in its own words. Everything in the library has passed the
   working filter, so this is never "does not run" -- it is what is imperfect about a
   machine that does. */
function conditionText(m) {
  if (!m.condition || !m.condition.length) {
    return m.driver_status === 'good' ? 'Fully emulated' : (m.driver_status || '');
  }
  return m.condition.join(', ');
}

/* What the last check of this file against the release found, in its own words. */
function checkedText(m) {
  if (!m.state) return '';
  if (m.state === 'current') return 'Matches the release';
  const detail = (m.state_detail || []).join(', ');
  const label = m.state === 'stale' ? 'Out of date'
    : m.state === 'incomplete' ? 'Missing an inherited ROM'
      : m.state === 'damaged' ? 'Cannot be read' : m.state;
  return detail ? `${label} — ${detail}` : label;
}

function landingRow(part) {
  const label = part.kind === 'chds' ? 'CHD' : 'ROM';
  const use = el('button', { class: 'btn sm', text: `Use as ${label} folder` });
  use.onclick = async () => {
    use.disabled = true;
    try {
      await post('/api/save', part.kind === 'chds'
        ? { chd_dir: part.path } : { rom_dir: part.path });
      use.textContent = 'Set';
    } catch (error) {
      use.disabled = false;
      use.textContent = error.message;
    }
  };
  const where = el('div', { style: 'flex:1;min-width:0' },
    el('b', { text: part.kind === 'chds' ? 'Disks' : 'ROMs' }),
    el('div', { class: 'mono dim', style: 'word-break:break-all', text: part.path }));
  const row = el('div', { class: 'rowflex', style: 'padding:10px 0' }, where, use);
  if (part.path_visible === false) {
    use.disabled = true;
    return el('div', {}, row, el('div', { class: 'banner warn', text:
      'That folder is not visible from here. The download will finish and the library '
      + 'step will find nothing. Add a remote path mapping in Settings → Download '
      + 'client so this app can read what the client writes.' }));
  }
  return row;
}

function whereItLands(parts) {
  const known = parts.filter((part) => part.path);
  if (!known.length) return null;
  return el('div', { class: 'panel', style: 'margin:16px 0 0' },
    el('h2', {}, 'Where it is going'),
    el('div', { class: 'body tight' }, known.map(landingRow)));
}
