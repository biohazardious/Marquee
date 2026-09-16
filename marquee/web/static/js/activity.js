/* Activity: what the job is doing now, and what the download client is fetching. */

import { $, append, clear, count, duration, el, human } from './util.js';
import { log, refreshQueue, state as store } from './api.js';

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

  host.append(jobPanel(data), el('div', { id: 'queuePanel' }), logPanel(place));
  renderQueue();
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
      text: `${count(data.plan.wanted)} games selected, ${count(data.plan.machines)} on disk. `
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
  if (!lastQueue.torrents.length) {
    body.append(el('div', { class: 'body' },
      el('div', { class: 'muted', text: 'No downloads. Nothing in the client belongs to this app yet.' })));
    return;
  }

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

function logPanel(place = { top: 0, atEnd: true }) {
  const lines = log();
  const box = el('div', { class: 'log' },
    lines.slice(-300).map((event) => el('div', { class: event.kind, text: event.text })));
  requestAnimationFrame(() => { box.scrollTop = place.atEnd ? box.scrollHeight : place.top; });
  return el('div', { class: 'panel' },
    el('h2', {}, 'Log', el('span', { class: 'sub', text: `${count(lines.length)} lines` })),
    el('div', { class: 'body' }, lines.length ? box : el('div', { class: 'muted', text: 'Nothing logged yet.' })));
}
