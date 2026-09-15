/* The shell: sidebar, routing, and the few actions that span pages. */

import { $, banner, clear, count, debounce, el, human } from './util.js';
import * as api from './api.js';
import * as changes from './changes.js';
import * as leftout from './leftout.js';
import * as library from './library.js';
import * as selection from './selection.js';
import * as settings from './settings.js';
import * as activity from './activity.js';
import * as wanted from './wanted.js';

const PAGES = [
  { id: 'library', label: 'Library', icon: '🕹' },
  { id: 'selection', label: 'Selection', icon: '☑' },
  { id: 'wanted', label: 'Wanted', icon: '⌾' },
  { id: 'leftout', label: 'Left out', icon: '🗃' },
  { id: 'changes', label: 'Transfer', icon: '⇄' },
  { id: 'activity', label: 'Activity', icon: '⇅' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
  { id: 'system', label: 'System', icon: '☰' },
];

/* "#library/galaga" opens the library with that game's panel already showing, so a
   game can be linked to and come back the same way after a reload. */
function route() {
  const [name, detail] = (location.hash || '#library').slice(1).split('/');
  return { page: PAGES.some((entry) => entry.id === name) ? name : 'library', detail };
}

let page = route().page;

/* ---------------- routing ---------------- */

function show(next, detail) {
  page = next;
  const want = detail ? `#${next}/${detail}` : `#${next}`;
  if (location.hash !== want) location.hash = want;
  for (const entry of PAGES) {
    $(`page-${entry.id}`)?.classList.toggle('hidden', entry.id !== next);
    $(`nav-${entry.id}`)?.classList.toggle('on', entry.id === next);
  }
  $('pageTitle').textContent = PAGES.find((entry) => entry.id === next).label;
  $('sidebar')?.classList.remove('open');
  // A game's drawer belongs to the library page; it used to stay open over whatever
  // page Back navigated to.
  if (next !== 'library' || !detail) library.close();
  if (next === 'library') {
    library.load();
    library.artStatus().then(library.renderArtBar);
  }
  if (next === 'selection') {
    selection.enter(api.state.data.plan);
    selection.render();
    showSelectionSaveState();
  }
  if (next === 'leftout') leftout.load();
  if (next === 'changes') changes.load();
  if (next === 'wanted') wanted.load();
  if (next === 'activity') { activity.render(api.state.data); activity.poll(); }
  if (next === 'settings') { settings.render(); settings.renderReleases(api.state.releases); }
  if (next === 'system') renderSystem();
  if (detail && next === 'library') library.open(detail);
}

window.addEventListener('hashchange', () => {
  const next = route();
  if (next.page !== page || next.detail) show(next.page, next.detail);
});

/* ---------------- sidebar ---------------- */

function buildNav() {
  const nav = $('nav');
  clear(nav);
  for (const entry of PAGES) {
    nav.append(el('a', {
      href: `#${entry.id}`, id: `nav-${entry.id}`,
      class: entry.id === page ? 'on' : '',
      onclick: (event) => { event.preventDefault(); show(entry.id); },
    },
      el('span', { class: 'ico', text: entry.icon }),
      el('span', { text: entry.label }),
      entry.id === 'activity' ? el('span', { class: 'tag hidden', id: 'activityTag' }) : null,
      entry.id === 'wanted' ? el('span', { class: 'tag hidden', id: 'wantedTag' }) : null));
  }
}

function renderSidefoot(data) {
  const foot = $('sidefoot');
  clear(foot);
  const plan = data.plan;
  const resolution = data.resolution;
  foot.append(
    row('MAME', resolution?.xml_version ? `0.${String(resolution.xml_version).replace(/^0\./, '')}` : '—'),
    // What is in the library, not what happens to be sitting in the torrent folder.
    row('Games', plan ? count(plan.library_machines ?? plan.machines) : '—'),
    row('Library', plan ? (plan.library_bytes_human || plan.bytes_human) : '—'));
}

/* Checking the library against the release, rather than trusting the filenames.

   A name match says nothing: MAME replaces bad dumps and renames chips between
   releases, and a library that never looks inside carries those changes for ever.
   Reading each zip's directory is about a minute for 10,000 games and is exact. */
function libraryCheck(data) {
  const found = (data.plan || {}).states;
  const busy = data.state === 'checking';
  const button = el('button', {
    class: 'btn primary sm', disabled: busy || !data.plan,
    text: busy ? 'Checking…' : 'Check the library',
  });
  button.onclick = async () => {
    button.disabled = true;
    try { await api.post('/api/check', {}); } catch (error) { banner(error.message, 'bad'); }
  };

  const rows = found && Object.keys(found).length
    ? el('table', { class: 'grid' }, el('tbody', {},
      [['stale', 'Out of date'], ['damaged', 'Damaged'],
        ['incomplete', 'Missing an inherited ROM'], ['current', 'Matches the release']]
        .filter(([key]) => found[key])
        .map(([key, label]) => el('tr', { style: 'cursor:default' },
          el('td', {}, label),
          el('td', { class: 'num nowrap', text: count(found[key]) })))))
    : null;

  return el('div', { class: 'panel' },
    el('h2', {}, 'The library itself',
      el('span', { class: 'sub', text: 'is what is there still the right file?' }),
      el('span', { class: 'spacer' }), button),
    el('div', { class: 'body tight' },
      rows,
      el('div', { class: 'hint', text: rows
        ? 'Each zip was read and its ROMs compared with the release, entry by entry. '
          + 'Anything out of date counts as something to fetch.'
        : 'Nothing has looked inside the files yet. Until something does, a game '
          + 'counts as present because a file with its name is there \u2014 which says '
          + 'nothing about whether it is still the right one.' })));
}

/* Where the rest went. 4,323 of 0.289's 16,350 machines never reach the library, and
   showing only the two totals leaves that as a mystery the app never answers. */
function filteredOut(resolution) {
  const rows = resolution.filtered || [];
  if (!rows.length) return null;
  const total = rows.reduce((sum, entry) => sum + entry.count, 0);
  return el('div', { class: 'panel' },
    el('h2', {}, 'Left out',
      el('span', { class: 'sub', text: `${count(total)} machines` })),
    el('div', { class: 'body tight' },
      el('table', { class: 'grid' },
        el('tbody', {}, rows.map((entry) => el('tr', { style: 'cursor:default' },
          el('td', {}, entry.label),
          el('td', { class: 'num nowrap', text: count(entry.count) }))))),
      el('div', { class: 'hint', text:
        'These never reach the library or the selection tree. Everything that is left '
        + 'runs \u2014 see the Condition filter for how well.' })));
}

const row = (key, value) => el('div', { class: 'stat' },
  el('span', { text: key }), el('b', { text: value }));

/* ---------------- top actions ---------------- */

async function buildPlan() {
  const button = $('planBtn');
  button.disabled = true;
  try {
    await api.post('/api/save', { ...settings.values(), ...selection.lists() });
    await api.post('/api/plan', {});
    show('activity');
  } catch (error) {
    banner(error.message, 'bad');
  } finally {
    button.disabled = false;
  }
}

/* Transfer shows what a transfer would do rather than starting one.

   A run copies, replaces, renames and -- if asked -- deletes, and until this page
   existed the only way to find out which was to let it happen and read the log. The
   button that starts it is on the page, next to the figures it is about to act on. */
function showChanges() {
  show('changes');
  changes.load();
}

/* ---------------- unlock ---------------- */

/* Shown when the server wants a key and we do not have a good one. The key is printed
   once at startup and lives in /config/api_key; it is a password, so it gets a
   password field and is remembered rather than asked for again. */
function unlock() {
  if ($('unlockScrim')) return;
  const input = el('input', { type: 'password', id: 'keyInput',
    name: 'key', placeholder: 'API key', autocomplete: 'current-password' });
  // Nothing has been typed yet, so there is nothing to complain about yet.
  const note = el('div', { class: 'hint', text: '' });

  const submit = async () => {
    api.setKey(input.value.trim());
    try {
      await api.refresh();
      $('unlockScrim')?.remove();
      $('unlockPanel')?.remove();
      begin();
    } catch (error) {
      note.textContent = error.message;
      note.style.color = 'var(--bad)';
      input.select();
    }
  };

  document.body.append(
    el('div', { class: 'scrim', id: 'unlockScrim' }),
    el('form', { class: 'unlock', id: 'unlockPanel',
      onsubmit: (event) => { event.preventDefault(); submit(); } },
      el('div', { class: 'mark', text: '🕹' }),
      el('h2', { text: 'Marquee' }),
      el('p', { class: 'muted' },
        'This server is listening beyond this machine, so it asks for its API key.'),
      input,
      note,
      el('button', { class: 'btn primary', type: 'submit', text: 'Unlock' }),
      el('p', { class: 'hint', style: 'margin-bottom:0' },
        'The key is printed when the server starts and stored in ',
        el('code', { text: 'config/api_key' }), '.')));
  input.focus();
}

export { banner };

/* ---------------- system ---------------- */

function renderSystem() {
  const host = $('systemBody');
  clear(host);
  const data = api.state.data;
  const resolution = data.resolution || {};
  const destination = data.destination;

  host.append(el('div', {},
    el('div', { class: 'panel' },
      el('h2', {}, 'Status'),
      el('div', { class: 'body' },
        el('dl', { class: 'facts' },
          el('dt', { text: 'ROM folder' }), el('dd', { class: 'mono', text: resolution.rom_dir || '—' }),
          el('dt', { text: 'CHD folder' }), el('dd', { class: 'mono', text: resolution.chd_dir || '—' }),
          el('dt', { text: 'MAME XML' }), el('dd', { class: 'mono', text: resolution.xml_file || '—' }),
          el('dt', { text: 'XML version' }), el('dd', { text: resolution.xml_version || '—' }),
          el('dt', { text: 'catlist.ini' }), el('dd', { class: 'mono', text: resolution.catlist_file || '—' }),
          el('dt', { text: 'Machines read' }), el('dd', { text: count(resolution.machines_read) }),
          el('dt', { text: 'Machines kept' }), el('dd', { text: count(resolution.machines_kept) })))),
    libraryCheck(data),
    filteredOut(resolution),
    destination
      ? el('div', { class: 'panel' },
          el('h2', {}, 'Library on disk',
            el('span', { class: 'sub', text: destination.copy_path || '' })),
          el('div', { class: 'body' },
            el('dl', { class: 'facts' },
              el('dt', { text: 'MAME version' }), el('dd', { text: destination.mame_version || '—' }),
              el('dt', { text: 'Last run' }),
              el('dd', { text: destination.updated ? destination.updated.replace('T', ' ').slice(0, 19) : '—' }),
              el('dt', { text: 'Machines' }), el('dd', { text: count(destination.machines) }),
              el('dt', { text: 'Files' }), el('dd', { text: count(destination.files) }),
              el('dt', { text: 'Size' }), el('dd', { text: destination.bytes ? human(destination.bytes) : '—' }),
              el('dt', { text: 'Runs' }), el('dd', { text: count(destination.runs) }))))
      : null,
    el('div', { class: 'panel' },
      el('h2', {}, 'About'),
      el('div', { class: 'body muted' },
        el('p', { style: 'margin-top:0' },
          'Marquee builds a categorised MAME library out of the Pleasuredome sets, '
          + 'fetching only the machines you actually keep.'),
        el('p', {}, el('a', { href: 'https://pleasuredome.github.io/pleasuredome/mame/index.html',
          target: '_blank', rel: 'noreferrer', text: 'Pleasuredome index' }))))));
}

/* ---------------- wiring ---------------- */

function onState(data) {
  renderSidefoot(data);

  // A library folder that is not there plans happily and then writes hundreds of
  // gigabytes somewhere nobody meant -- inside a container, most likely.
  // A new MAME release is worth a line, because acting on it is a few gigabytes
  // rather than an afternoon.
  const watch = data.release_watch;
  const news = $('releaseNews');
  if (news) {
    news.classList.toggle('hidden', !(watch && watch.newer));
    if (watch && watch.newer) {
      clear(news);
      news.append(
        el('span', { text: `MAME ${watch.newest} is out — this library holds ${watch.library}. ` }),
        el('a', { href: '#wanted', text: 'See what moving would cost',
          onclick: (event) => { event.preventDefault(); show('wanted'); } }));
    }
  }

  const missing = data.resolution && data.resolution.destination_exists === false;
  $('destWarning').classList.toggle('hidden', !missing);
  if (missing) {
    $('destWarning').textContent =
      `The library folder does not exist: ${data.config?.copy_path || ''} — it will be `
      + 'created. Check the path if you expected it to be there already.';
  }

  if (data.plan) {
    const tag = $('wantedTag');
    if (tag) {
      const short = data.plan.missing_roms || 0;
      tag.classList.toggle('hidden', !short);
      tag.textContent = short > 999 ? `${Math.floor(short / 1000)}k` : count(short);
    }
    const arrived = !hadPlan;
    hadPlan = true;
    selection.adopt(data.plan, data.config);
    leftout.adopt(data.config);
    library.fillGenres($('libGenre'), data.plan);
    selection.fillGenres($('selGenre'), data.plan);
    $('planStats').textContent =
      `${count(data.plan.wanted)} games · ${data.plan.wanted_bytes_human || data.plan.bytes_human}`
      + (data.plan.machines ? ` · ${count(data.plan.machines)} on disk` : '');
    // The tree is drawn before the first poll lands, so it has to be redrawn when the
    // plan it describes finally arrives.
    showSelectionSaveState();
    if (page === 'selection') selection.enter(data.plan);
    if (arrived) {
      if (page === 'selection') selection.render();
      if (page === 'library') library.load();
      if (page === 'wanted') wanted.load();
    }
  }
  if (data.config) {
    // Adopted on every poll, not once: the form is rebuilt from this whenever the
    // page is opened, and after a save it used to show the values from before it,
    // so a second Save put them back. The form on screen is never redrawn here --
    // values() reads the inputs, not this -- so nothing being typed is disturbed.
    settings.adopt(data.config);
    const lists = {
      genres: data.config.blacklist_genres,
      categories: data.config.blacklist_categories,
      roms: data.config.blacklist_roms,
    };
    // The selection follows the file as long as nothing unsaved is on screen. "Put
    // back" on Left out writes to the file; without this the page kept the old
    // list and the next Build plan excluded the games again.
    if (!settingsAdopted || !selection.dirty()) selection.seed(lists);
    if (!settingsAdopted) {
      settingsAdopted = true;
      if (page === 'settings') settings.render();
    }
  }

  const busy = ['planning', 'copying'].includes(data.state);
  $('planBtn').disabled = busy;
  // It opens the preview rather than starting anything, so there is no reason to
  // lock it while a job runs -- watching what a run is doing is the point.
  $('copyBtn').disabled = !data.plan;
  $('cancelBtn').classList.toggle('hidden', !busy);
  const tag = $('activityTag');
  if (tag) {
    tag.classList.toggle('hidden', !busy);
    tag.textContent = '●';
  }

  if (page === 'activity') activity.render(data);
  if (page === 'selection') updateSelectionSummary();
  // The diff is worked out when a plan is built and taken again when the library is
  // checked, so the page that shows it has to come back for it when either finishes.
  if (page === 'changes' && lastState !== data.state) changes.load();
  lastState = data.state;
  // Drawn once on navigation, before the first poll has landed -- so it has to be
  // drawn again when the numbers it reports turn up.
  if (page === 'system') renderSystem();
}

let settingsAdopted = false;
let hadPlan = false;
let lastState = '';

function updateSelectionSummary() {
  const host = $('selectionSummary');
  if (!host) return;
  const plan = api.state.data.plan;
  const estimate = selection.estimate();
  const matches = plan && !dirty();
  // What the selection holds, not what happens to be downloaded -- on a fresh install
  // the second is zero and saying "0 games selected" under a full tree is nonsense.
  host.textContent = matches
    ? `${count(plan.wanted)} games · ${plan.wanted_bytes_human || plan.bytes_human} selected`
    : `about ${count(estimate.games)} games · ${human(estimate.bytes)} — build a plan to confirm`;
  showSelectionSaveState();
}

/* Unticking a genre is changing a setting, and a setting that disappears on refresh
   is not a setting. The tree used to be written only as a side effect of Build plan
   or of the Settings page's Save, neither of which is anywhere near it. */
function selectionUnsaved() {
  const saved = api.state.data.config;
  if (!saved) return false;
  const now = selection.lists();
  const same = (a, b) =>
    JSON.stringify([...(a || [])].sort()) === JSON.stringify([...(b || [])].sort());
  return !(same(saved.blacklist_genres, now.blacklist_genres)
    && same(saved.blacklist_categories, now.blacklist_categories)
    && same(saved.blacklist_roms, now.blacklist_roms));
}

function showSelectionSaveState() {
  const save = $('selSave');
  const revert = $('selRevert');
  if (!save) return;
  const unsaved = selectionUnsaved();
  save.disabled = !unsaved;
  save.textContent = unsaved ? 'Save selection' : 'Saved';
  revert?.classList.toggle('hidden', !unsaved);
}

async function saveSelection() {
  const button = $('selSave');
  button.disabled = true;
  try {
    // Only the three lists. Saving the settings form alongside them would quietly
    // commit edits somebody left half-finished on another page.
    await api.post('/api/save', selection.lists());
    // The poll's config is what "saved" is measured against, so it has to catch up
    // before the button can honestly say so.
    await api.refresh();
    banner('Selection saved.', 'good');
  } catch (error) {
    banner(error.message, 'bad');
  } finally {
    showSelectionSaveState();
  }
}

function revertSelection() {
  const saved = api.state.data.config || {};
  selection.seed({
    genres: saved.blacklist_genres,
    categories: saved.blacklist_categories,
    roms: saved.blacklist_roms,
  });
  selection.render();
  updateSelectionSummary();
}

/* The server states what each plan was built from, so "has the form moved on?" is a
   comparison rather than a flag that drifts whenever the page reloads. */
/* Whether the form has moved on from the plan that is in hand.

   A field the form did not report is unchanged, not cleared -- the settings page only
   reports what is actually on screen, so before it has been opened it reports nothing
   at all, and reading that as "" made an untouched selection claim it needed
   re-planning. */
function dirty() {
  const plan = api.state.data.plan;
  if (!plan || !plan.built_from) return true;
  const form = { ...settings.values(), ...selection.lists() };
  const built = plan.built_from;
  const same = (a, b) => JSON.stringify([...(a || [])].sort()) === JSON.stringify([...(b || [])].sort());
  const path = (key) => form[key] === undefined || built[key] === (form[key] || '');
  const list = (key) => form[key] === undefined || same(built[key], form[key]);
  return !(path('rom_dir') && path('chd_dir') && path('copy_path')
    && list('blacklist_genres') && list('blacklist_categories')
    && list('blacklist_roms'));
}

async function boot() {
  buildNav();
  const bar = library.toolbar();
  $('libSearch').replaceWith(bar.search);
  bar.search.id = 'libSearch';
  $('libGenre').replaceWith(bar.genres);
  bar.genres.id = 'libGenre';
  $('libHave').replaceWith(bar.have);
  bar.have.id = 'libHave';
  $('libCondition').replaceWith(bar.condition);
  bar.condition.id = 'libCondition';
  $('libState').replaceWith(bar.state);
  bar.state.id = 'libState';
  $('libStatus').replaceWith(bar.statuses);
  bar.statuses.id = 'libStatus';
  $('libAdult').replaceWith(bar.adult);
  bar.adult.id = 'libAdult';
  $('libArt').replaceWith(bar.art);
  bar.art.id = 'libArt';
  $('libGet').replaceWith(bar.get);
  bar.get.id = 'libGet';
  $('libGetArt').replaceWith(bar.getArt);
  bar.getArt.id = 'libGetArt';
  $('libViews').replaceWith(bar.views);
  bar.views.id = 'libViews';

  $('planBtn').onclick = buildPlan;
  $('copyBtn').onclick = showChanges;
  $('cancelBtn').onclick = async () => {
    try {
      const answer = await api.post('/api/cancel', {});
      if (!answer.cancelling) banner('Nothing to stop: planning runs to the end.', 'info');
    } catch (error) {
      banner(error.message, 'bad');
    }
  };
  $('menuBtn').onclick = () => $('sidebar').classList.toggle('open');
  const selBar = selection.toolbar();
  $('treeSearch').replaceWith(selBar.search);
  selBar.search.id = 'treeSearch';
  selBar.search.placeholder = 'Find a game…';
  selBar.search.oninput = debounce((event) => selection.search(event.target.value));
  $('selGenre').replaceWith(selBar.genres);
  selBar.genres.id = 'selGenre';
  $('selHave').replaceWith(selBar.have);
  selBar.have.id = 'selHave';
  $('selCondition').replaceWith(selBar.condition);
  selBar.condition.id = 'selCondition';
  $('selAdult').replaceWith(selBar.adult);
  selBar.adult.id = 'selAdult';
  $('selSave').onclick = saveSelection;
  $('selRevert').onclick = revertSelection;
  $('refreshReleases').onclick = async (event) => {
    event.target.disabled = true;
    try {
      await api.refreshReleases();
      pollReleases();
    } catch (error) {
      banner(error.message, 'bad');
    } finally {
      event.target.disabled = false;
    }
  };
  settings.onRefreshReleases(pollReleases);
  $('saveBtn').onclick = async () => {
    try {
      await api.post('/api/save', { ...settings.values(), ...selection.lists() });
      banner('Settings saved.', 'good');
    } catch (error) {
      banner(error.message, 'bad');
    }
  };

  selection.onSelectionChange(updateSelectionSummary);
  api.subscribe(onState);

  // Ask once before drawing anything. Starting the views first meant each of them
  // raced the answer and put up its own "bad key" banner behind the dialog that was
  // already asking for one.
  try {
    await api.refresh();
  } catch (error) {
    if (error instanceof api.Unauthorised) { unlock(); return; }
  }
  begin();
}

let begun = false;

function begin() {
  // Once. Unlocking again after the key changed mid-session used to start a second
  // poll loop and a second queue timer beside the first.
  if (begun) return;
  begun = true;
  api.startPolling(unlock);
  const start = route();
  show(start.page, start.detail);
  setInterval(() => { if (page === 'activity') activity.poll(); }, 3000);
  pollReleases();
}

async function pollReleases() {
  try {
    api.state.releases = await api.releases();
    if (page === 'settings') settings.renderReleases(api.state.releases);
    if (api.state.releases.running) setTimeout(pollReleases, 1500);
  } catch { /* the indexer is optional */ }
}

boot();
