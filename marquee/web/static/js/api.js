/* Everything that talks to the server, plus the poll loop the pages read from. */

/* The API key. It arrives once in the URL the server prints at startup, and is kept
   after that -- asking someone to paste a key out of a container log on every visit is
   not a login. Sent as a header, so it never sits in a URL again. */
const STORE = 'marquee.key';

function initialKey() {
  const fromUrl = new URLSearchParams(location.search).get('token');
  if (fromUrl) {
    try { localStorage.setItem(STORE, fromUrl); } catch { /* private window */ }
    // Take it out of the address bar so it is not bookmarked or shoulder-surfed.
    history.replaceState(null, '', location.pathname + location.hash);
    return fromUrl;
  }
  try { return localStorage.getItem(STORE) || ''; } catch { return ''; }
}

let key = initialKey();

export function setKey(value) {
  key = value || '';
  try {
    if (key) localStorage.setItem(STORE, key);
    else localStorage.removeItem(STORE);
  } catch { /* private window */ }
}

export class Unauthorised extends Error {}

/* True once the server has refused the key we hold. Views check it so they render
   nothing rather than each stacking up its own "bad key" banner behind the dialog
   that is already asking for one. */
export const lock = { on: false };

export async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (key) headers['X-Token'] = key;
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { /* an empty body is fine */ }
  if (response.status === 401) {
    lock.on = true;
    throw new Unauthorised(payload.error || 'This server needs an API key.');
  }
  lock.on = false;
  // Only the status decides. A 200 whose body carries an `error` field is data --
  // the job snapshot says what the last run stopped on, the queue says why the
  // client could not be reached -- and treating it as a failed request meant that
  // once a job hit an error, every poll threw and the page froze on the state
  // before it, never showing the error at all.
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

export const post = (path, body) =>
  api(path, { method: 'POST', body: JSON.stringify(body || {}) });

/* One poll loop for the whole page. Views subscribe; nobody starts their own timer,
   so opening five tabs does not mean five requests a second. */
export const state = {
  data: {},
  cursor: 0,
  listeners: new Set(),
  releases: null,
  queue: null,
};

export function subscribe(fn) {
  state.listeners.add(fn);
  if (state.data.state) fn(state.data);
  return () => state.listeners.delete(fn);
}

function announce() { for (const fn of state.listeners) fn(state.data); }

let events = [];

export function log() { return events; }

let inflight = null;

/* One refresh at a time. A save and the poll used to ask with the same cursor at the
   same moment, and both answers appended the same log lines. */
export function refresh() {
  if (!inflight) inflight = doRefresh().finally(() => { inflight = null; });
  return inflight;
}

/* The plan and the settings are most of every answer and seldom change, so the
   revisions already in hand go with the request and the server leaves out whatever
   they still describe. A key left out keeps its value in the merge below. */
function stateUrl(after) {
  const query = new URLSearchParams({ after });
  if (state.data.plan && state.data.plan_rev) query.set('plan_rev', state.data.plan_rev);
  if (state.data.config && state.data.config_rev) query.set('config_rev', state.data.config_rev);
  return `/api/state?${query.toString()}`;
}

async function doRefresh() {
  let snapshot = await api(stateUrl(state.cursor));
  if (snapshot.sequence !== undefined && snapshot.sequence < state.cursor) {
    // The job was reset: its numbering started over, so ours is ahead of it and
    // would skip every line the new run has already written. Start again.
    state.cursor = 0;
    events = [];
    snapshot = await api(stateUrl(0));
  }
  if (snapshot.sequence !== undefined) state.cursor = snapshot.sequence;
  if (snapshot.events && snapshot.events.length) {
    events = events.concat(snapshot.events).slice(-600);
  }
  // Events arrive as a delta; everything else is a full picture each time -- and the
  // server sends every key, null when there is nothing, so a re-plan does not keep
  // showing the previous run's summary. The two exceptions, plan and config, are
  // left out only when the revision sent says the page already has them.
  const { events: _drop, ...rest } = snapshot;
  state.data = { ...state.data, ...rest };
  announce();
  return state.data;
}

let polling = false;

/* Safe to call again: a loop already running is left alone. A 401 ends the loop --
   there is no point asking with a key the server refused -- and unlocking starts it
   again. It used not to: the page went on showing a frozen state after the new key
   was accepted. */
export function startPolling(onUnauthorised) {
  if (polling) return;
  polling = true;
  const tick = async () => {
    try {
      await refresh();
    } catch (error) {
      if (error instanceof Unauthorised) {
        polling = false;
        onUnauthorised?.(error);
        return;
      }
      /* anything else: the page shows the last good state */
    }
    const busy = ['planning', 'copying', 'checking'].includes(state.data.state);
    setTimeout(tick, busy ? 700 : 4000);
  };
  tick();
  startQueuePolling();
}

/* The download client's queue, read on its own clock: it is what the status bar
   on every page shows, so it is fetched once for the page rather than by whichever
   view happens to be open. Quicker while something is coming down. */
const queueListeners = new Set();

export function subscribeQueue(fn) {
  queueListeners.add(fn);
  if (state.queue) fn(state.queue);
  return () => queueListeners.delete(fn);
}

export async function refreshQueue() {
  try {
    state.queue = await api('/api/queue');
  } catch (error) {
    if (error instanceof Unauthorised) throw error;
    state.queue = { configured: true, error: error.message, torrents: [], unreachable: true };
  }
  for (const fn of queueListeners) fn(state.queue);
  return state.queue;
}

let queueStarted = false;

export function startQueuePolling() {
  if (queueStarted) return;
  queueStarted = true;
  const tick = async () => {
    try { await refreshQueue(); } catch { /* the key dialog is already up */ }
    const active = state.queue && state.queue.downloading;
    setTimeout(tick, active ? 3000 : 8000);
  };
  tick();
}

export const machines = (params) =>
  api('/api/machines?' + new URLSearchParams(params).toString());

export const machine = (name) =>
  api('/api/machine?' + new URLSearchParams({ name }).toString());

export const browse = (path) =>
  api('/api/browse?' + new URLSearchParams({ path }).toString());

export const releases = () => api('/api/releases');
export const refreshReleases = () => post('/api/releases/refresh');
export const queue = () => api('/api/queue');
export const testClient = (body) => post('/api/client/test', body);
