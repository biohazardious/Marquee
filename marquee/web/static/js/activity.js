/* Activity: what the job is doing now, and what the download client is fetching. */

import { $, append, clear, count, duration, el, human } from './util.js';
import { log, queue as fetchQueue } from './api.js';

let lastQueue = null;

export async function poll() {
  try { lastQueue = await fetchQueue(); } catch { lastQueue = null; }
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

function jobPanel(data) {
  const state = data.state || 'idle';
  const progress = data.progress;
  const summary = data.summary;

  const body = el('div', { class: 'body' });

  if (state === 'idle') {
    body.append(el('div', { class: 'muted', text: 'Nothing running.' }));
  } else if (progress) {
    append(body,
      el('div', { class: 'rowflex', style: 'margin-bottom:8px' },
        el('b', { text: progress.stage }),
        el('span', { class: 'spacer', style: 'flex:1' }),
        el('span', { class: 'muted', text: `${progress.percent}%` })),
      el('div', { class: 'bar' }, el('i', { style: `width:${progress.percent}%` })),
      progress.detail ? el('div', { class: 'hint', text: progress.detail }) : null);
  } else {
    body.append(el('div', { class: 'muted', text: `${state}…` }));
  }

  if (summary) {
    body.append(el('div', { class: 'cards', style: 'margin-top:18px' },
      stat('Copied', count(summary.copied)),
      stat('Updated', count(summary.updated)),
      stat('Moved', count(summary.moved)),
      stat('Skipped', count(summary.skipped)),
      stat('Transferred', summary.bytes_human, summary.rate_human)));
  }
  if (data.error) body.append(el('div', { class: 'banner bad', text: data.error }));

  return el('div', { class: 'panel' },
    el('h2', {}, 'Current job', el('span', { class: 'sub', text: state })), body);
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
      el('span', { class: 'sub', text: lastQueue?.configured ? 'qBittorrent' : 'no client configured' }),
      el('span', { class: 'spacer' }),
      lastQueue?.speed
        ? el('span', { class: 'muted', text: `${human(lastQueue.speed)}/s` })
        : null),
    body);
  host.append(panel);

  if (!lastQueue || !lastQueue.configured) {
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
          el('i', { style: `width:${Math.round(torrent.progress * 100)}%` }))),
      el('td', {}, el('span', { class: `badge ${torrent.phase === 'complete' ? 'new' : 'move'}`, text: torrent.state })),
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
