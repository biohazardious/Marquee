/* The shell: sidebar, routing, and the few actions that span pages. */

import { $, append, banner, clear, count, debounce, duration, el, human, plural } from './util.js';
import * as api from './api.js';
import * as changes from './changes.js';
import * as leftout from './leftout.js';
import * as library from './library.js';
import * as selection from './selection.js';
import * as settings from './settings.js';
import * as activity from './activity.js';
import * as wanted from './wanted.js';
import * as overview from './overview.js';
import { icon } from './icons.js';

/* In the order the work happens: what is here, what you want, what is missing, what
   the run will do, and then the machinery. The group labels say so. */
const PAGES = [
  { id: 'overview', label: 'Overview', icon: 'overview', group: '' },
  { id: 'library', label: 'Library', icon: 'library', group: 'Collection' },
  { id: 'selection', label: 'Selection', icon: 'selection' },
  { id: 'leftout', label: 'Left out', icon: 'leftout' },
  { id: 'wanted', label: 'Wanted', icon: 'wanted', group: 'Getting it there' },
  { id: 'changes', label: 'Transfer', icon: 'transfer' },
  { id: 'activity', label: 'Activity', icon: 'activity' },
  { id: 'settings', label: 'Settings', icon: 'settings', group: 'Setup' },
  { id: 'system', label: 'System', icon: 'system' },
];

/* "#library/galaga" opens the library with that game's panel already showing, so a
   game can be linked to and come back the same way after a reload. */
function route() {
  const [name, detail] = (location.hash || '#overview').slice(1).split('/');
  return { page: PAGES.some((entry) => entry.id === name) ? name : 'overview', detail };
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
  if (next === 'overview') overview.render(withStale(api.state.data));
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
    if (entry.group) nav.append(el('div', { class: 'group', text: entry.group }));
    nav.append(el('a', {
      href: `#${entry.id}`, id: `nav-${entry.id}`,
      class: entry.id === page ? 'on' : '',
      onclick: (event) => { event.preventDefault(); show(entry.id); },
    },
      icon(entry.icon),
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
  // Four figures that used to be three, and named for what they are. "Games 0" under
  // "1,144 on disk" read as a bug; it was the library, not the torrent folder.
  foot.append(
    row('Release', resolution?.xml_version ? `MAME ${resolution.xml_version}` : '—'),
    row('Selected', plan ? count(plan.wanted) : '—'),
    row('Downloaded', plan ? count(plan.machines) : '—'),
    row('In library', plan
      ? `${count(plan.library_machines ?? 0)} · ${plan.library_bytes_human || '0 B'}`
      : '—'));
  // Which Marquee this is, and whether a newer one has been tagged. "latest" on
  // Docker Hub says nothing about what is actually running; this does.
  const about = data.app;
  if (about) {
    const line = el('div', { class: 'stat about', title: `build ${about.build}` },
      el('span', { text: `Marquee ${about.version}` }),
      about.update?.newer
        ? el('a', { href: about.update.url, target: '_blank', rel: 'noreferrer',
          class: 'update', text: `${about.update.latest} is out` })
        : el('span', { class: 'dim', text: about.build }));
    foot.append(line);
  }
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
  /* The deep check reads every disk in full and hashes it against the torrent's own
     piece hashes: the one exact answer to "was this copied before it finished". It
     needs the download client and takes as long as reading the library does. */
  const deep = el('button', {
    class: 'btn sm', disabled: busy || !data.plan || !(data.config || {}).download_client,
    text: 'Deep check',
    title: 'Hash every disk against the torrent it came from (reads the whole library)',
  });
  deep.onclick = async () => {
    deep.disabled = true;
    try { await api.post('/api/check', { deep: true }); } catch (error) { banner(error.message, 'bad'); }
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
      el('span', { class: 'spacer' }), deep, button),
    el('div', { class: 'body tight' },
      rows,
      el('div', { class: 'hint', text: rows
        ? 'Each zip was read and its ROMs compared with the release, entry by entry; '
          + 'each disk was opened and, where a finished download or the torrent\u2019s '
          + 'piece hashes were there to compare against, held to them. '
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
      el('span', { class: 'sub', text: `${plural(total, 'machine', 'machines')}` })),
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
      el('div', { class: 'mark', 'aria-hidden': 'true', html: BRAND_MARK }),
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
      el('h2', {}, 'About',
        el('span', { class: 'sub', text: data.app ? `Marquee ${data.app.version}` : '' })),
      el('div', { class: 'body' },
        data.app ? el('dl', { class: 'facts' },
          el('dt', { text: 'Version' }), el('dd', { text: data.app.version }),
          el('dt', { text: 'Build' }), el('dd', { class: 'mono', text: data.app.build }),
          el('dt', { text: 'Newest' }),
          el('dd', {}, data.app.update?.latest
            ? (data.app.update.newer
              ? el('a', { href: data.app.update.url, target: '_blank', rel: 'noreferrer',
                text: `${data.app.update.latest} — pull the new image` })
              : data.app.update.latest === data.app.version
                ? `${data.app.update.latest} — this is it`
                : `${data.app.update.latest} is the newest tagged; this build is ahead of it`)
            : (data.app.update?.error ? `could not ask GitHub (${data.app.update.error})` : 'checking…'))) : null,
        el('p', { class: 'muted' },
          'Marquee builds a categorised MAME library out of the Pleasuredome sets, '
          + 'fetching only the machines you actually keep. '),
        el('p', { class: 'muted', style: 'margin-bottom:0' },
          el('a', { href: 'https://github.com/biohazardious/Marquee', target: '_blank',
            rel: 'noreferrer', text: 'Source and releases' }), ' · ',
          el('a', { href: 'https://hub.docker.com/r/biohazardious/marquee', target: '_blank',
            rel: 'noreferrer', text: 'Docker Hub' }), ' · ',
          el('a', { href: 'https://pleasuredome.github.io/pleasuredome/mame/index.html',
            target: '_blank', rel: 'noreferrer', text: 'Pleasuredome index' }))))));
}

/* ---------------- wiring ---------------- */

function onState(data) {
  renderSidefoot(data);
  renderStatusJob(data);

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
    renderPlanPill(data);
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
    if ($('settingsPath')) $('settingsPath').textContent = data.config.settings_path || '';
    const lists = {
      genres: data.config.blacklist_genres,
      categories: data.config.blacklist_categories,
      roms: data.config.blacklist_roms,
    };
    // The selection follows the file as long as nothing unsaved is on screen. "Put
    // back" on Left out writes to the file; without this the page kept the old
    // list and the next Build plan excluded the games again.
    if (!settingsAdopted || !selection.dirty() || selection.matches(lists)) {
      selection.seed(lists);
    }
    if (!settingsAdopted) {
      settingsAdopted = true;
      if (page === 'settings') settings.render();
    }
  }

  const busy = ['planning', 'copying', 'checking'].includes(data.state);
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
  if (page === 'overview') overview.render(withStale(data));
  if (page === 'selection') updateSelectionSummary();
  // The diff is worked out when a plan is built and taken again when the library is
  // checked, so the page that shows it has to come back for it when either finishes.
  if (page === 'changes' && lastState !== data.state) changes.load();
  // What the library holds changes when a transfer, a check or a plan finishes, and
  // the Library page read it once: it went on showing the old picture until reopened.
  if (page === 'library' && ['planning', 'copying', 'checking'].includes(lastState) && !busy) {
    library.load();
  }
  lastState = data.state;
  // In the tab's title too: a transfer runs for hours, and the tab strip is where it
  // is glanced at from.
  document.title = busy && data.progress
    ? `(${data.progress.percent}%) ${data.progress.stage} — ${BASE_TITLE}`
    : BASE_TITLE;
  // Drawn once on navigation, before the first poll has landed -- so it has to be
  // drawn again when the numbers it reports turn up.
  if (page === 'system') renderSystem();
  if (page === 'settings') settings.renderVersions(data.versions);
}

// The sidebar's own mark, for the one screen that has no sidebar.
const BRAND_MARK = '<svg viewBox="0 0 32 32" width="40" height="40">'
  + '<rect x="4" y="6" width="24" height="20" rx="3" fill="#241a05"/>'
  + '<path d="M8 26V10h3v9h2v-9h3v16zM19 12h6v3h-6zM19 17h6v3h-6z" fill="#f5a524"/></svg>';

let settingsAdopted = false;
let hadPlan = false;
let lastState = '';
const BASE_TITLE = document.title || 'Marquee';

/* A selection ticked and not saved is lost with the tab. Ask first, the way any
   editor does. */
window.addEventListener('beforeunload', (event) => {
  if (!selectionUnsaved()) return;
  event.preventDefault();
  event.returnValue = '';
});

/* ---------------- the status bar ---------------- */

/* How fast the current stage is moving, from what the last half minute of polls
   said, so the bar can say "3m left" rather than only a percentage. Bytes for a
   transfer, machines for a check or a parse; the arithmetic is the same. */
const trend = { stage: null, samples: [] };

function rateOf(progress) {
  const now = Date.now();
  if (trend.stage !== progress.stage) { trend.stage = progress.stage; trend.samples = []; }
  trend.samples.push({ t: now, done: progress.done });
  trend.samples = trend.samples.filter((sample) => now - sample.t <= 30000);
  const first = trend.samples[0];
  const last = trend.samples[trend.samples.length - 1];
  if (!first || last.t - first.t < 1500) return 0;
  return (last.done - first.done) / ((last.t - first.t) / 1000);
}

function renderStatusJob(data) {
  const host = $('statusJob');
  if (!host) return;
  clear(host);
  const state = data.state || 'idle';
  const progress = data.progress;
  const plan = data.plan;

  const bar = (percent) => el('span', { class: 'mini-bar' },
    el('i', { style: `width:${Math.max(0, Math.min(100, percent))}%` }));

  if (['planning', 'checking', 'copying'].includes(state)) {
    const verb = { planning: 'Building the plan', checking: 'Checking the library',
      copying: 'Transferring' }[state];
    const parts = [el('span', { class: 'pulse' }), el('b', { text: verb })];
    if (progress && progress.total) {
      const rate = rateOf(progress);
      const left = rate > 0 ? (progress.total - progress.done) / rate : 0;
      const bytes = progress.stage === 'copy';
      const figure = bytes
        ? `${human(progress.done)} of ${human(progress.total)}`
        : `${count(progress.done)} of ${count(progress.total)}`;
      parts.push(el('span', { class: 'muted', text: progress.stage === 'copy' || progress.stage === 'verify'
        || progress.stage === 'parse' ? figure : progress.stage }));
      parts.push(bar(progress.percent));
      if (bytes && rate > 0) parts.push(el('span', { class: 'muted', text: `${human(rate)}/s` }));
      if (left >= 1) parts.push(el('span', { class: 'accent', text: `${duration(left)} left` }));
      if (progress.detail) parts.push(el('span', { class: 'dim detail', text: progress.detail }));
    } else if (progress && progress.stage) {
      parts.push(el('span', { class: 'muted', text: progress.stage }));
    } else {
      parts.push(el('span', { class: 'muted', text: 'starting…' }));
    }
    append(host, ...parts);
    host.className = 'status-job live';
    return;
  }
  if (state === 'error') {
    append(host, icon('warn'), el('b', { text: 'Stopped' }),
      el('span', { class: 'muted detail', text: data.error || '' }));
    host.className = 'status-job bad';
    return;
  }
  if (state === 'done' && data.summary) {
    const s = data.summary;
    append(host, icon('check'), el('b', { text: s.cancelled ? 'Transfer stopped early' : 'Transfer finished' }),
      el('span', { class: 'muted', text:
        `${count(s.copied)} copied · ${count(s.moved)} moved · ${count(s.updated)} replaced`
        + (s.deleted ? ` · ${count(s.deleted)} deleted` : '')
        + (s.failed ? ` · ${count(s.failed)} failed` : '') + ` · ${s.bytes_human} in ${duration(s.seconds)}` }));
    host.className = `status-job ${s.failed ? 'warn' : 'good'}`;
    return;
  }
  if (plan) {
    const moving = (plan.sync?.new?.count || 0) + (plan.sync?.update?.count || 0)
      + (plan.sync?.move?.count || 0);
    append(host, icon('check'), el('b', { text: 'Plan ready' }),
      el('span', { class: 'muted', text: moving
        ? `${plural(moving, 'file', 'files')} · ${plan.to_transfer_human} to transfer`
        : 'nothing to transfer' }));
    host.className = 'status-job';
    return;
  }
  append(host, icon('info'), el('span', { class: 'muted', text: 'Nothing running — build a plan to start.' }));
  host.className = 'status-job off';
}

function renderStatusClient(queue) {
  const host = $('statusClient');
  if (!host) return;
  clear(host);
  if (!queue || !queue.configured) {
    append(host, el('i', { class: 'dot off' }), el('span', { class: 'dim', text: 'No download client' }));
    host.className = 'status-client off';
    return;
  }
  if (queue.error) {
    // The short reason; the whole sentence is in the tooltip and on Activity.
    const reason = queue.error.length > 60 ? queue.error.split(': ').pop() : queue.error;
    append(host, el('i', { class: 'dot bad' }), el('b', { text: 'qBittorrent unreachable' }),
      el('span', { class: 'muted detail', title: queue.error, text: reason }));
    host.className = 'status-client bad';
    return;
  }
  if (queue.downloading) {
    append(host, el('i', { class: 'dot live' }),
      el('b', { text: `Downloading ${count(queue.downloading)} ${queue.downloading === 1 ? 'release' : 'releases'}` }),
      el('span', { class: 'muted', text: `${queue.speed_human} · ${queue.bytes_left_human} left` }),
      queue.eta ? el('span', { class: 'accent', text: `${duration(queue.eta)} left` }) : null);
    host.className = 'status-client live';
    return;
  }
  append(host, el('i', { class: 'dot good' }), el('b', { text: 'qBittorrent connected' }),
    el('span', { class: 'muted', text: queue.torrents.length
      ? `${count(queue.torrents.length)} of this app's torrents · ${count(queue.seeding || 0)} seeding`
      : 'nothing of this app\'s in the queue' }));
  host.className = 'status-client good';
}

/* Whether the plan in hand still describes the settings on screen. */
function withStale(data) {
  return { ...data, stale: Boolean(data.plan) && dirty() };
}

/* The topbar's one-line answer to "is the plan current?", in place of a run of
   numbers the sidebar already shows. */
function renderPlanPill(data) {
  const pill = $('planStats');
  if (!pill) return;
  clear(pill);
  const plan = data.plan;
  if (!plan) { pill.className = 'planpill off'; pill.textContent = 'No plan yet'; return; }
  if (dirty()) {
    pill.className = 'planpill warn';
    append(pill, icon('warn'), 'Settings changed — rebuild the plan');
    return;
  }
  const moving = (plan.sync?.new?.count || 0) + (plan.sync?.update?.count || 0)
    + (plan.sync?.move?.count || 0);
  pill.className = `planpill ${moving ? 'good' : ''}`;
  append(pill, icon(moving ? 'transfer' : 'check'),
    moving ? `${plan.to_transfer_human} to transfer` : 'Plan is current');
}

function updateSelectionSummary() {
  const host = $('selectionSummary');
  if (!host) return;
  const plan = api.state.data.plan;
  const estimate = selection.estimate();
  const matches = plan && !dirty();
  // What the selection holds, not what happens to be downloaded -- on a fresh install
  // the second is zero and saying "0 games selected" under a full tree is nonsense.
  clear(host);
  host.append(matches
    ? `${plural(plan.wanted, 'game', 'games')} · ${plan.wanted_bytes_human || plan.bytes_human} selected`
    : `about ${plural(estimate.games, 'game', 'games')} · ${human(estimate.bytes)} — build a plan to confirm`);
  // The part that is easy to miss: unticking a game that is already on the console
  // is asking for it to leave. Said here, where the tick was, and not only under
  // "Delete" on the Transfer page after a rebuild.
  if (estimate.removed) {
    host.append(el('span', { class: 'leaving', title:
      'These are in the library now. After a rebuild the Transfer page lists them under '
      + 'Delete, and they go only if you tick "Also delete" there.' },
      `· ${count(estimate.removed)} in the library · ${human(estimate.removedBytes)} to delete`));
  }
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
  const flag = (key) => form[key] === undefined || Boolean(built[key]) === Boolean(form[key]);
  // Everything that shapes the plan. Parents-only, the adult folder and the release
  // used to be left out, so changing one never said the plan was out of date.
  return !(path('rom_dir') && path('chd_dir') && path('copy_path')
    && path('mature_rom_folder') && path('mame_version')
    && flag('allow_mature') && flag('parents_only')
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
  $('menuBtn').append(icon('menu'));
  for (const box of document.querySelectorAll('.search:not(:has(.ico))')) box.prepend(icon('search'));
  $('menuBtn').onclick = () => $('sidebar').classList.toggle('open');
  overview.onNavigate(show);
  api.subscribeQueue(renderStatusClient);
  api.subscribeQueue(() => { if (page === 'activity') activity.poll(); });
  // "/" jumps to the search box of whatever page has one -- the habit every
  // modern list app has trained.
  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.target.matches('input, textarea, select')) return;
    const box = document.querySelector(`#page-${page} .search input`);
    if (box) { event.preventDefault(); box.focus(); box.select(); }
  });
  // A filter that is set looks set. Every <select> in a toolbar gets the accent
  // border while it is narrowing something.
  document.addEventListener('change', (event) => {
    const select = event.target;
    if (select.matches('.toolbar select, .libsum select')) {
      // Narrowing means "not the first choice": the art-kind picker always has a
      // value and is never a filter.
      const first = select.options[0] ? select.options[0].value : '';
      select.classList.toggle('active', select.value !== first);
    }
  });
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
  // Every filter says what it filters. Eight bare menus reading "Any", "Everything"
  // and "Title screens" in a row left the reader to guess which was which.
  for (const [id, caption] of [
    ['libGenre', 'Genre'], ['libHave', 'Have'], ['libCondition', 'Emulation'],
    ['libState', 'Check'], ['libStatus', 'Transfer'], ['libAdult', 'Adult'],
    ['libArt', 'Art'], ['selGenre', 'Genre'], ['selHave', 'Have'],
    ['selCondition', 'Emulation'], ['selAdult', 'Adult'],
  ]) {
    const control = $(id);
    if (!control) continue;
    const wrap = el('label', { class: 'pick' }, el('span', { text: caption }));
    control.replaceWith(wrap);
    wrap.append(control);
  }
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
  // poll loop and a second queue timer beside the first -- but the state poll ends
  // on a 401, so it is restarted (startPolling ignores a loop already running).
  if (begun) { api.startPolling(unlock); return; }
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
    if (page === 'settings') settings.renderVersions(api.state.data.versions);
    if (api.state.releases.running) setTimeout(pollReleases, 1500);
  } catch { /* the indexer is optional */ }
}

boot();
