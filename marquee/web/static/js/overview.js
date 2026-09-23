/* Overview: where the library stands and what to do next.

   The app is a pipeline -- a romset folder, a selection, the files still to fetch,
   the library on the console -- and until now no page said which step you were on.
   Seven pages of numbers is not the same as one answer to "what now?". */

import { $, ago, append, banner, clear, count, el, human, plural, pressable } from './util.js';
import { post, state } from './api.js';
import { icon } from './icons.js';

let navigate = null;

/* app.js owns navigation; the cards here only ask for it. */
export function onNavigate(fn) { navigate = fn; }

const go = (page) => () => navigate?.(page);

/* ---------------- the next thing to do ---------------- */

/* One recommendation, worked out from the same numbers the cards show. Ordered by
   what blocks what: nothing can be fetched before there is a plan, nothing can be
   transferred before it has been fetched. */
export function nextStep(data) {
  const config = data.config || {};
  const plan = data.plan;
  const busy = ['planning', 'copying', 'checking'].includes(data.state);
  if (busy) {
    return { tone: 'info', title: `${cap(data.state)}…`,
             text: 'Watch it on the Activity page; the numbers here update when it is done.',
             action: 'Activity', page: 'activity' };
  }
  if (!config.rom_dir || !config.copy_path) {
    return { tone: 'warn', title: 'Set your folders',
             text: 'Where the romset is, and where the library should go. Everything else follows from those two.',
             action: 'Open Settings', page: 'settings' };
  }
  if (!plan) {
    return { tone: 'warn', title: 'Build a plan',
             text: 'Reads the release and your folders and works out what the library would hold.',
             action: 'Build plan', run: 'plan' };
  }
  if (data.stale) {
    return { tone: 'warn', title: 'Your settings moved on',
             text: 'The plan was built from an earlier selection or different folders. Rebuild it before trusting the numbers.',
             action: 'Rebuild plan', run: 'plan' };
  }
  // A download finished after this plan was built: what arrived is not in it yet.
  // Plan first, then the transfer page has something to say about it.
  const arrived = data.queue?.finished_since_plan;
  if (arrived && arrived.games) {
    return { tone: 'good', title: `${plural(arrived.games, 'game', 'games')} arrived since the plan was built`,
             text: `${arrived.bytes_human} finished downloading. Build the plan to take them in, then Transfer puts them in the library.`,
             action: 'Build plan', run: 'plan' };
  }
  const missing = plan.missing_roms || 0;
  const transfer = (plan.sync?.new?.count || 0) + (plan.sync?.update?.count || 0)
    + (plan.sync?.move?.count || 0);
  if (transfer && plan.fits === false) {
    // Saying "ready" over a disk that cannot take it is the one thing the front
    // page must not do.
    return { tone: 'warn', title: `Not enough space for the transfer: short by ${plan.short_human}`,
             text: `${plan.needed_human} has to be written and ${plan.free_human} is free. Free some space, or leave more out on the Selection page.`,
             action: 'Open Selection', page: 'selection' };
  }
  if (transfer) {
    const copying = (plan.sync?.new?.count || 0) + (plan.sync?.update?.count || 0);
    return { tone: 'good', title: copying
      ? `${plan.to_transfer_human || ''} ready to go into the library`.trim()
      : `${plural(transfer, 'file', 'files')} to move within the library`,
             text: `${plural(transfer, 'file', 'files')} are waiting to be copied, replaced or moved. See exactly which before it runs.`,
             action: 'Preview the transfer', page: 'changes' };
  }
  if (missing) {
    return { tone: 'info', title: `${plural(missing, 'game', 'games')} are selected but not downloaded`,
             text: config.download_client
               ? 'Only the missing files are asked for; anything you are seeding keeps seeding.'
               : 'Set a download client in Settings and they can be fetched from here.',
             action: config.download_client ? 'See what they cost' : 'Open Settings',
             page: config.download_client ? 'wanted' : 'settings' };
  }
  if (plan.empty_folders) {
    return { tone: 'info', title: `${plural(plan.empty_folders, 'empty folder', 'empty folders')} in the library`,
             text: 'Games that left the library took their files with them but not their folders. '
               + 'A transfer removes them; nothing else changes.',
             action: 'Preview the transfer', page: 'changes' };
  }
  if (!Object.keys(plan.states || {}).length) {
    return { tone: 'info', title: 'Everything is in place',
             text: 'Every game the selection wants is in the library, by name and size. '
               + 'Checking it as well opens each file and compares it with the release — optional, and slow over a share.',
             action: 'Check the library', run: 'check' };
  }
  const when = plan.checked_at ? ` (checked ${ago(plan.checked_at)})` : '';
  return { tone: 'good', title: 'Up to date',
           text: `The library holds what the selection asks for, and every file that was checked matches MAME ${data.resolution?.xml_version || ''}${when}.`,
           action: 'Browse the library', page: 'library' };
}

const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);

/* ---------------- render ---------------- */

let surveyAsked = false;

export function render(data) {
  const host = $('overviewBody');
  if (!host) return;
  clear(host);
  if (!data || !data.state) {
    host.append(el('div', { class: 'muted', style: 'padding:14px', text: 'Reading…' }));
    return;
  }
  // The folder walk is what the Source card shows. It used to run only when the
  // settings page asked for it, so the front page said "looking" for ever.
  if (!surveyAsked && data.config?.rom_dir && data.survey && !data.survey.data
      && !data.survey.running) {
    surveyAsked = true;
    post('/api/survey', {}).catch(() => {});
  }
  append(host, nextPanel(data), pipeline(data), lower(data));
}

function nextPanel(data) {
  const step = nextStep(data);
  const button = el('button', { class: `btn ${step.tone === 'warn' ? 'primary' : ''}`, text: step.action });
  button.onclick = async () => {
    if (step.page) { navigate?.(step.page); return; }
    button.disabled = true;
    try {
      await post(`/api/${step.run}`, {});
      navigate?.('activity');
    } catch (error) {
      banner(error.message, 'bad');
      button.disabled = false;
    }
  };
  return el('div', { class: `next ${step.tone}` },
    el('div', { class: 'next-mark' }, icon(step.tone === 'good' ? 'check' : step.tone === 'warn' ? 'warn' : 'bolt')),
    el('div', { class: 'next-text' },
      el('div', { class: 'next-title', text: step.title }),
      el('div', { class: 'muted', text: step.text })),
    button);
}

/* The four stages as cards in a row, each with the number that matters and the
   page it belongs to. The connectors between them are what says "this is an
   order, not a menu". */
function pipeline(data) {
  const plan = data.plan;
  const config = data.config || {};
  const survey = data.survey?.data || {};
  const roms = survey.roms;
  const missing = plan?.missing_roms || 0;
  const onDisk = plan?.machines || 0;
  const wanted = plan?.wanted || 0;
  const inLibrary = plan?.library_machines ?? data.destination?.machines ?? null;
  const libraryBytes = plan?.library_bytes_human
    || (data.destination?.bytes ? human(data.destination.bytes) : null);

  const partial = plan?.partial_roms || 0;
  const source = stage({
    name: 'Source', icon: 'folder', page: 'settings',
    // An empty torrent folder is only a problem while something is still missing:
    // once the library holds the lot, there is nothing that has to be there.
    state: !config.rom_dir ? 'off' : roms?.files ? (partial ? 'warn' : 'on')
      : (plan && !missing ? 'off' : 'warn'),
    value: plan ? count(onDisk) : (roms ? count(roms.files) : (config.rom_dir ? '…' : '—')),
    unit: plan ? 'games in the torrent folder, ready to transfer'
      : roms ? `files · ${roms.bytes_human || human(roms.bytes)}` : (config.rom_dir ? 'looking' : 'no folder set'),
    note: partial ? `${count(partial)} more still downloading` : (config.rom_dir ? shorten(config.rom_dir) : 'Where the romset is.'),
  });
  const selection = stage({
    name: 'Selection', icon: 'selection', page: 'selection',
    state: plan ? (data.stale ? 'warn' : 'on') : 'off',
    value: plan ? count(wanted) : '—',
    unit: plan ? `games · ${plan.wanted_bytes_human || plan.bytes_human}` : 'no plan yet',
    note: plan
      ? `${count((config.blacklist_genres || []).length)} genres left out`
      : 'What goes in, and what stays out.',
  });
  const fetch = stage({
    name: 'Fetch', icon: 'download', page: 'wanted',
    state: !plan ? 'off' : missing ? (config.download_client ? 'warn' : 'bad') : 'on',
    value: plan ? count(missing) : '—',
    unit: plan ? (missing ? `missing · ${plan.missing_bytes_human || 'size unknown'}` : 'nothing missing') : 'no plan yet',
    note: config.download_client ? 'qBittorrent connected' : 'no download client',
  });
  const transfer = plan?.sync
    ? (plan.sync.new.count + plan.sync.update.count + plan.sync.move.count) : 0;
  const library = stage({
    name: 'Library', icon: 'library', page: 'changes',
    state: !plan ? 'off' : transfer ? 'warn' : 'on',
    value: inLibrary === null ? (plan ? count(onDisk) : '—') : count(inLibrary),
    unit: libraryBytes ? `games · ${libraryBytes}` : (plan ? 'games' : 'nothing yet'),
    note: transfer
      ? `${plan.to_transfer_human} to transfer`
      : (config.copy_path ? shorten(config.copy_path) : 'Where the console reads from.'),
  });

  return el('div', { class: 'pipeline' },
    source, connector(), selection, connector(), fetch, connector(), library);
}

function stage({ name, icon: glyph, page, state: tone, value, unit, note }) {
  // Focusable was not enough: Enter and Space did nothing.
  return pressable(el('div', { class: `stage ${tone}`, onclick: go(page) },
    el('div', { class: 'stage-head' }, icon(glyph), el('span', { text: name }),
      el('i', { class: 'dot', title: TONE_WORD[tone] })),
    el('div', { class: 'stage-value', text: value }),
    el('div', { class: 'stage-unit', text: unit }),
    el('div', { class: 'stage-note', text: note })), go(page));
}

const TONE_WORD = { on: 'ready', warn: 'needs attention', bad: 'blocked', off: 'not started' };

const connector = () => el('div', { class: 'connector' }, icon('arrow'));

function shorten(path) {
  return path.length > 34 ? `…${path.slice(-33)}` : path;
}

/* ---------------- the lower half: health, space, history ---------------- */

function lower(data) {
  return el('div', { class: 'overview-grid' },
    healthPanel(data),
    spacePanel(data),
    historyPanel(data));
}

function healthPanel(data) {
  const config = data.config || {};
  const plan = data.plan;
  const watch = data.release_watch || {};
  const checked = Object.keys(plan?.states || {}).length > 0;
  const stale = plan?.states?.stale || 0;
  const damaged = plan?.states?.damaged || 0;
  const items = [
    line(config.copy_path
      ? (data.resolution?.destination_exists === false ? 'warn' : 'good') : 'warn',
      'Library folder',
      !config.copy_path ? 'not set'
        : data.resolution?.destination_exists === false ? 'does not exist yet — it will be created'
        : config.copy_path),
    data.resolution?.chd_set_found === false && plan?.disks_to_fetch
      ? line('warn', 'CHD folder',
        `no CHD set under ${config.chd_dir || config.rom_dir} yet — `
        + `${count(plan.disks_to_fetch)} wanted disks are neither there nor in the library; `
        + 'fetch them from the Wanted page')
      : null,
    line(config.download_client ? 'good' : 'off', 'Download client',
      config.download_client ? config.download_client : 'none — files are only categorised, not fetched'),
    line(watch.newer ? 'warn' : (watch.newest ? 'good' : 'off'), 'Release',
      watch.newer ? `MAME ${watch.newest} is out; this library holds ${watch.library}`
        : watch.newest ? `MAME ${watch.newest} is the newest published` : 'release list not read yet'),
    consoleLine(data.resolution?.console),
    line(!plan ? 'off' : !checked ? 'off' : (stale || damaged) ? 'warn' : 'good', 'Library check',
      !plan ? 'no plan yet' : !checked ? 'not checked yet — optional; it opens every file'
        : (stale || damaged) ? `${plural(stale + damaged, 'game', 'games')} out of date or damaged`
          + (plan.checked_at ? ` · checked ${ago(plan.checked_at)}` : '')
        : `${plural((plan.states.current || 0) + (plan.states.incomplete || 0), 'game', 'games')} match the release`
          + (plan.checked_at ? ` · checked ${ago(plan.checked_at)}` : '')),
    plan?.fits === false
      ? line('bad', 'Free space', `short by ${plan.short_human} for the next transfer`)
      : null,
    data.app ? line(data.app.update?.newer ? 'warn' : 'good', 'Marquee',
      data.app.update?.newer
        ? `${data.app.version} running; ${data.app.update.latest} is out — pull the new image`
        : `${data.app.version} (${data.app.build})`) : null,
  ];
  return el('div', { class: 'panel' },
    el('h2', {}, 'Health'),
    el('div', { class: 'body tight' }, el('ul', { class: 'health' }, items)));
}

/* Only when Settings names a console MAME older than the set. */
function consoleLine(fit) {
  if (!fit) return null;
  if (fit.error) {
    return line('warn', 'Console MAME', `could not read MAME ${fit.version}; every game stays where it is`);
  }
  const apart = (fit.newer || 0) + (fit.missing || 0);
  return line(apart ? 'info' : 'good', 'Console MAME', apart
    ? `${fit.version}: ${plural(fit.newer || 0, 'game needs', 'games need')} a newer MAME, `
      + `${plural(fit.missing || 0, 'game misses', 'games miss')} a ROM \u2014 filed apart`
    : `${fit.version} runs every game in the selection`);
}

function line(tone, label, text) {
  return el('li', { class: tone },
    icon(tone === 'good' ? 'check' : tone === 'bad' || tone === 'warn' ? 'warn' : 'info'),
    el('b', { text: label }),
    // Paths are long and the line truncates them: the whole of it on hover.
    el('span', { class: 'muted', text, title: text }));
}

function spacePanel(data) {
  const plan = data.plan;
  const body = el('div', { class: 'body' });
  if (!plan || !plan.free_human) {
    body.append(el('div', { class: 'muted', text: plan
      ? 'This library\u2019s server does not say how much room is left (FTP cannot). Check the console\u2019s disk before a large transfer.'
      : 'Build a plan to see what the next transfer needs.' }));
  } else if (!plan.needed) {
    // Nothing to write: the question is how full the disk already is. A large "0 B"
    // over an empty bar said nothing about a disk that was 99% full.
    const used = plan.disk_used_percent;
    const tight = used !== undefined && used >= 90;
    append(body,
      el('div', { class: 'big-stat' },
        el('b', { class: tight ? 'warn' : '', text: plan.free_human }),
        el('span', { class: 'muted', text: plan.disk_human
          ? `free of ${plan.disk_human} · ${used}% full` : 'free' })),
      used !== undefined
        ? el('div', { class: `bar ${tight ? 'warn' : ''}` }, el('i', { style: `width:${used}%` }))
        : null,
      el('div', { class: 'hint', text: tight
        ? 'Nothing to write in the next transfer, but the library\u2019s disk is nearly full. A new release needs room for the games that changed.'
        : 'Nothing to write in the next transfer.' }));
  } else {
    const share = plan.free ? Math.min(100, Math.round((plan.needed / plan.free) * 100)) : 100;
    append(body,
      el('div', { class: 'big-stat' },
        el('b', { class: plan.fits ? '' : 'bad', text: plan.needed_human }),
        el('span', { class: 'muted', text: `to write · ${plan.free_human} free`
          + (plan.disk_human ? ` of ${plan.disk_human}` : '') })),
      el('div', { class: `bar ${plan.fits ? '' : 'bad'}` }, el('i', { style: `width:${share}%` })),
      el('div', { class: 'hint', text: plan.fits
        ? `${share}% of the free space on the library's disk.`
        : `Short by ${plan.short_human}. Free some space or leave more out.` }));
  }
  return el('div', { class: 'panel' }, el('h2', {}, 'Space',
    el('span', { class: 'sub', text: 'on the library\u2019s disk' })), body);
}

function historyPanel(data) {
  const history = data.destination?.history || [];
  const body = el('div', { class: 'body tight' });
  if (!history.length) {
    body.append(el('div', { class: 'body muted', text: 'No run recorded in this library yet. Each transfer from now on is listed here.' }));
  } else {
    body.append(el('table', { class: 'grid' }, el('tbody', {},
      [...history].reverse().map((run) => el('tr', { style: 'cursor:default' },
        el('td', { class: 'nowrap dim', text: (run.date || '').replace('T', ' ').slice(0, 16) }),
        el('td', {}, el('b', { text: run.to ? `MAME ${run.to}` : '—' }),
          run.from && run.from !== run.to ? el('small', { class: 'dim', text: ` from ${run.from}` }) : null),
        el('td', { class: 'muted', text: summarise(run) }),
        el('td', { class: 'num nowrap', text: human(run.bytes || 0) }))))));
  }
  return el('div', { class: 'panel span2' }, el('h2', {}, 'Recent runs',
    el('span', { class: 'sub', text: `${count(data.destination?.runs || 0)} in all` })), body);
}

function summarise(run) {
  const parts = [];
  if (run.copied) parts.push(`${count(run.copied)} copied`);
  if (run.updated) parts.push(`${count(run.updated)} replaced`);
  if (run.moved) parts.push(`${count(run.moved)} moved`);
  if (run.deleted) parts.push(`${count(run.deleted)} deleted`);
  if (run.cancelled) parts.push('stopped early');
  return parts.join(' · ') || 'nothing to do';
}

/* Redrawn on every poll while shown; cheap, the page is a few dozen nodes. */
export function watch() {
  return state.data;
}
