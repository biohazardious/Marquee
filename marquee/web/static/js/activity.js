/* Activity: what the job is doing now, and what the download client is fetching. */

import { $, append, clear, count, duration, el, human, plural } from './util.js';
import { api, log, post, refreshQueue, state as store } from './api.js';

let lastQueue = null;

export async function poll() {
  // The same answer the status bar reads; one request serves both.
  lastQueue = store.queue || null;
  if (!lastQueue) { try { lastQueue = await refreshQueue(); } catch { lastQueue = null; } }
  renderQueue();
}

export function render(data) {
  const host = $('activityBody');
  if (!host) return;
  // Where the reader was in the log before the redraw. Every poll rebuilds the page,
  // and forcing the log to its end each time meant a warning a screen up could not
  // be read while a run was going.
  const previous = host.querySelector('.log');
  const place = previous
    ? { top: previous.scrollTop,
        atEnd: previous.scrollTop + previous.clientHeight >= previous.scrollHeight - 4 }
    : { top: 0, atEnd: true };
  clear(host);

  host.append(jobPanel(data), el('div', { id: 'queuePanel' }), el('div', { id: 'reclaimPanel' }),
    logPanel(place));
  renderQueue();
  renderReclaim();
}

/* ---------------- getting the space back ---------------- */

/* Kept across redraws: the page is rebuilt on every poll, and a price someone is
   reading -- or a Remove armed for a second click -- must not vanish under them. */
const R = { price: null, busy: false, error: '', done: '', armed: false };

async function askPrice() {
  R.busy = true; R.error = ''; R.done = ''; R.armed = false;
  renderReclaim();
  try { R.price = await api('/api/reclaim'); } catch (error) { R.error = error.message; }
  R.busy = false;
  renderReclaim();
}

async function removeReady() {
  const ready = (R.price?.torrents || []).filter((row) => row.reclaimable);
  if (!R.armed) { R.armed = true; renderReclaim(); return; }
  R.busy = true;
  renderReclaim();
  try {
    const done = await post('/api/reclaim', { hashes: ready.map((row) => row.hash) });
    R.done = `Removed ${plural(done.removed, 'torrent', 'torrents')} and freed ${done.freed_human}. `
      + 'Build the plan again so it stops expecting their files in the torrent folder.';
    R.price = null;
  } catch (error) {
    R.error = error.message;
  }
  R.busy = false; R.armed = false;
  renderReclaim();
}

function renderReclaim() {
  const host = $('reclaimPanel');
  if (!host) return;
  clear(host);
  const body = el('div', { class: 'body' });
  host.append(el('div', { class: 'panel' },
    el('h2', {}, 'Reclaim space', el('span', { class: 'sub', text: 'once the library has the files' })),
    body));
  if (R.done) body.append(el('div', { class: 'banner good', text: R.done }));
  if (R.error) body.append(el('div', { class: 'banner bad', text: R.error }));
  if (!R.price) {
    body.append(el('p', { class: 'muted', text:
      'A finished download sits in the torrent folder as well as in the library. Marquee can '
      + 'remove its own torrents, with their files, once every file is in the library — '
      + 'nothing else, and nothing before you have seen what it frees.' }),
    el('button', { class: 'btn', disabled: R.busy, text: R.busy ? 'Working it out…' : 'What could be freed?',
                   onclick: askPrice }));
    return;
  }
  const rows = R.price.torrents || [];
  if (!rows.length) {
    body.append(el('p', { class: 'muted', text: 'None of the torrents in the client are Marquee’s own.' }));
    return;
  }
  body.append(el('table', { class: 'grid' },
    el('tbody', {}, rows.map((row) => el('tr', { style: 'cursor:default' },
      el('td', {}, el('b', { text: row.name }),
        row.blocked ? el('div', { class: 'hint', text: row.blocked }) : null,
        row.linked ? el('div', { class: 'hint', text:
          `${plural(row.linked, 'file is a hardlink', 'files are hardlinks')} of the library’s copy: removing frees nothing for ${row.linked === 1 ? 'it' : 'them'}.` }) : null),
      el('td', { class: 'num nowrap', text: plural(row.files, 'file', 'files') }),
      el('td', { class: 'num nowrap' },
        row.reclaimable ? el('span', { class: 'badge new', text: `frees ${row.freed_human}` })
          : el('span', { class: 'badge keep', text: 'not yet' })))))));
  const ready = rows.filter((row) => row.reclaimable);
  body.append(el('p', { class: 'hint', text: R.price.note }));
  if (ready.length) {
    body.append(el('button', {
      class: 'btn danger', disabled: R.busy,
      text: R.armed
        ? `Remove ${plural(ready.length, 'torrent', 'torrents')} and free ${R.price.freed_human} — sure?`
        : `Remove ${plural(ready.length, 'torrent', 'torrents')} and free ${R.price.freed_human}`,
      onclick: removeReady }));
  }
  body.append(el('button', { class: 'btn ghost', style: 'margin-left:8px', text: 'Ask again', onclick: askPrice }));
}

/* What each state means to a person, and which of the three steps it belongs to.
   "planned…" with an ellipsis read as if something were still happening. */
const STATES = {
  idle: ['Nothing running', 'plan'],
  planning: ['Building the plan', 'plan'],
  planned: ['Plan ready', 'plan'],
  checking: ['Checking the library', 'check'],
  copying: ['Transferring', 'transfer'],
  done: ['Transfer finished', 'transfer'],
  error: ['Stopped on an error', 'plan'],
};

function jobPanel(data) {
  const state = data.state || 'idle';
  const [label, step] = STATES[state] || [state, 'plan'];
  const progress = data.progress;
  const summary = data.summary;
  const running = ['planning', 'checking', 'copying'].includes(state);

  const body = el('div', { class: 'body' });

  // The three things a job can be, as a strip: which one this is, and what it is
  // doing inside it.
  body.append(el('div', { class: 'steps' },
    ...[['plan', 'Plan'], ['check', 'Check'], ['transfer', 'Transfer']].map(([key, name]) =>
      el('span', { class: `step ${key === step ? (running ? 'live' : 'on') : ''}`, text: name }))));

  if (running && progress) {
    append(body,
      el('div', { class: 'rowflex', style: 'margin:14px 0 8px' },
        el('b', { text: progress.stage }),
        el('span', { class: 'spacer', style: 'flex:1' }),
        el('span', { class: 'muted', text: `${progress.percent}%` })),
      el('div', { class: 'bar' }, el('i', { style: `width:${progress.percent}%` })),
      progress.detail ? el('div', { class: 'hint', text: progress.detail }) : null);
  } else if (running) {
    body.append(el('div', { class: 'muted', style: 'margin-top:14px', text: `${label}…` }));
  } else if (state === 'planned' && data.plan) {
    body.append(el('div', { class: 'muted', style: 'margin-top:14px',
      text: `${plural(data.plan.wanted, 'game', 'games')} selected: `
        + `${count(data.plan.library_machines ?? 0)} in the library, `
        + `${count(data.plan.machines)} in the torrent folder. `
        + 'The Transfer page says what a run would do.' }));
  }

  if (summary) {
    body.append(el('div', { class: 'cards', style: 'margin-top:18px' },
      stat('Copied', count(summary.copied)),
      stat('Replaced', count(summary.updated)),
      stat('Moved', count(summary.moved)),
      stat('Deleted', count(summary.deleted)),
      stat('Left alone', count(summary.skipped)),
      summary.failed ? stat('Failed', count(summary.failed)) : null,
      stat('Transferred', summary.bytes_human, `${summary.rate_human} · ${duration(summary.seconds)}`)));
    if (summary.cancelled) body.append(el('div', { class: 'banner warn', text: 'Stopped before the end. Whatever arrived is kept; the next run continues from there.' }));
  }
  if (data.error) body.append(el('div', { class: 'banner bad', text: data.error }));

  return el('div', { class: 'panel' },
    el('h2', {}, running ? el('span', { class: 'pulse' }) : null, label), body);
}

function stat(key, value, note) {
  return el('div', { class: 'card' },
    el('div', { class: 'k', text: key }),
    el('div', { class: 'v', text: value }),
    note ? el('div', { class: 'n', text: note }) : null);
}

function renderQueue() {
  const host = $('queuePanel');
  if (!host) return;
  clear(host);

  const body = el('div', { class: 'body tight' });
  const panel = el('div', { class: 'panel' },
    el('h2', {}, 'Downloads',
      el('span', { class: 'sub', text: !lastQueue ? '' : lastQueue.configured ? 'qBittorrent' : 'no client configured' }),
      el('span', { class: 'spacer' }),
      lastQueue?.speed
        ? el('span', { class: 'muted', text: `${human(lastQueue.speed)}/s` })
        : null),
    body);
  host.append(panel);

  if (!lastQueue) {
    body.append(el('div', { class: 'body' }, el('div', { class: 'muted', text: 'Asking the download client…' })));
    return;
  }
  if (!lastQueue.configured) {
    body.append(el('div', { class: 'body' },
      el('div', { class: 'muted', text: 'Set a download client in Settings to fetch releases from here.' })));
    return;
  }
  if (lastQueue.error) {
    body.append(el('div', { class: 'body' }, el('div', { class: 'banner bad', text: lastQueue.error })));
    return;
  }
  const jobs = lastQueue.jobs || [];
  if (!lastQueue.torrents.length && !jobs.length) {
    body.append(el('div', { class: 'body' },
      el('div', { class: 'muted', text: 'No downloads. Nothing in the client belongs to this app yet.' })));
    return;
  }
  // What was asked for, as games -- then the torrents underneath, for the detail.
  jobs.forEach((job) => body.append(jobCard(job)));
  if (!lastQueue.torrents.length) return;

  body.append(el('div', { class: 'subhead', text: 'Torrents in the client' }));
  body.append(el('table', { class: 'grid' },
    el('thead', {}, el('tr', {},
      el('th', { text: 'Release' }), el('th', { text: 'State' }),
      el('th', { class: 'num', text: 'Progress' }),
      el('th', { class: 'num', text: 'Selected' }),
      el('th', { class: 'num', text: 'Of set' }),
      el('th', { class: 'num', text: 'Speed' }),
      el('th', { class: 'num', text: 'Left' }))),
    el('tbody', {}, lastQueue.torrents.map((torrent) => el('tr', { style: 'cursor:default' },
      el('td', {}, el('b', { text: torrent.name }),
        el('div', { class: 'bar', style: 'margin-top:6px;max-width:280px' },
          el('i', { style: `width:${Math.round(torrent.progress * 100)}%` })),
        // "error" in qBittorrent is nearly always a folder it cannot write to --
        // the path as *it* sees it, which is what the message has to name.
        torrent.phase === 'failed'
          ? el('div', { class: 'bad small', style: 'margin-top:6px' },
            `qBittorrent reports “${torrent.state}”`
            + (torrent.error_message ? `: ${torrent.error_message}` : '')
            + `. Its save path is ${torrent.save_path || 'unknown'}. `
            + ((torrent.error_message || '').toLowerCase().includes('permission denied')
              ? 'That is ownership: qBittorrent cannot write there. Run Marquee with '
                + 'qBittorrent’s PUID/PGID and give the torrent folder back to that user.'
              : 'Check that folder exists and is writable on qBittorrent’s side.'))
          : null),
      el('td', {}, el('span', { class: `badge ${torrent.phase === 'complete' ? 'new' : torrent.phase === 'failed' ? 'bad' : 'move'}`, text: torrent.state })),
      el('td', { class: 'num', text: `${Math.round(torrent.progress * 100)}%` }),
      el('td', { class: 'num nowrap', text: human(torrent.size) }),
      // The gap between these two columns is the whole point of the app.
      el('td', { class: 'num nowrap dim', text: human(torrent.total_size) }),
      el('td', { class: 'num nowrap', text: torrent.speed ? `${human(torrent.speed)}/s` : '-' }),
      el('td', { class: 'num nowrap', text: duration(torrent.eta) }))))));
}

const JOB_STATE = {
  waiting: ['Waiting for peers', 'move'], downloading: ['Downloading', 'move'],
  complete: ['Arrived', 'new'], removed: ['Removed from the client', 'bad'],
};

/* One download Marquee started: which games, how many are in, what is left, and --
   the part the queue could never say -- what this release does not carry. */
function jobCard(job) {
  const [word, tone] = JOB_STATE[job.state] || [job.state, 'keep'];
  const percent = job.bytes ? Math.round((job.bytes_done / job.bytes) * 100) : 0;
  const card = el('div', { class: `fetch ${job.state}` },
    el('div', { class: 'fetch-head' },
      el('b', { text: job.label || 'Download' }),
      el('span', { class: `badge ${tone}`, text: word }),
      el('span', { class: 'spacer' }),
      el('span', { class: 'muted small', text: job.releases.join(' + ') })),
    el('div', { class: 'fetch-figures' },
      el('span', { class: 'fetch-count', text: `${count(job.games_done)}` }),
      el('span', { class: 'muted', text: ` of ${plural(job.games, 'game', 'games')} in` }),
      el('span', { class: 'spacer' }),
      el('span', { class: 'muted', text: job.state === 'complete'
        ? human(job.bytes) : `${human(job.bytes - job.bytes_done)} left of ${human(job.bytes)}` })),
    el('div', { class: 'bar' }, el('i', { style: `width:${percent}%` })));
  if (job.error) card.append(el('div', { class: 'banner bad', text: job.error }));
  if (job.state === 'complete' && job.completed_at) {
    card.append(el('div', { class: 'hint', text:
      'Everything asked for is here. Build the plan to take it in, then transfer it to the library.' }));
  }
  if ((job.arriving || []).length) {
    const more = (job.arriving_count || job.arriving.length) - job.arriving.length;
    card.append(el('details', { class: 'fetch-list' },
      el('summary', { text: `Still arriving: ${plural(job.arriving_count || job.arriving.length, 'game', 'games')}` }),
      el('ul', {}, job.arriving.map((row) => el('li', {},
        el('span', { class: 'mono', text: row.game }),
        el('span', { class: 'bar thin' }, el('i', { style: `width:${Math.round(row.progress * 100)}%` }))))),
      more > 0 ? el('div', { class: 'muted small', text: `and ${count(more)} more` }) : null));
  }
  (job.missing || []).forEach((gap) => card.append(el('div', { class: 'fetch-gap' },
    el('b', { text: `${plural(gap.count, gap.kind === 'chds' ? 'disk' : 'game', gap.kind === 'chds' ? 'disks' : 'games')} not in ${gap.release}` }),
    el('div', { class: 'hint', text: gap.why }),
    el('div', { class: 'mono small muted', text: gap.names.join(', ')
      + (gap.count > gap.names.length ? ` … and ${count(gap.count - gap.names.length)} more` : '') }))));
  return card;
}

function logPanel(place = { top: 0, atEnd: true }) {
  const lines = log();
  const box = el('div', { class: 'log' },
    lines.slice(-300).map((event) => el('div', { class: event.kind, text: event.text })));
  requestAnimationFrame(() => { box.scrollTop = place.atEnd ? box.scrollHeight : place.top; });
  return el('div', { class: 'panel' },
    el('h2', {}, 'Log', el('span', { class: 'sub', text: `${plural(lines.length, 'line', 'lines')}` })),
    el('div', { class: 'body' }, lines.length ? box : el('div', { class: 'muted', text: 'Nothing logged yet.' })));
}
