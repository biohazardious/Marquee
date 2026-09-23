/* Small shared helpers. No framework: one user, one page, plain DOM. */

export const $ = (id) => document.getElementById(id);

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'data') Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* Node.append() stringifies whatever is not a node, so `cond ? thing : null` appends
   the literal word "null" to the page. el() already filters those out of its own
   children; this is the same filter for an existing node. */
export function append(node, ...children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* A remembered preference, or `fallback`. Storage can be switched off or full, and
   then even reading it throws -- at module load, which left the whole page blank. */
export function stored(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

export function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not remembered, and nothing else lost */
  }
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

export function human(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes || 0, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit === 0 ? 0 : value < 10 ? 2 : 1)} ${units[unit]}`;
}

export const count = (n) => (n || 0).toLocaleString();

/* "1 file", "2 files": the page said "Also delete the 1 files nothing wants". */
export const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many}`;

export function duration(seconds) {
  // 8,640,000 is qBittorrent's "no idea": it reports exactly that for anything that
  // is not moving, and the guard used to let it through as a confident "2400h 0m".
  if (!seconds || seconds < 0 || seconds >= 8640000) return '-';
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
}

/* "just now", "40 minutes ago", "3 days ago" for a unix time in seconds. */
export function ago(when) {
  if (!when) return '';
  const seconds = Math.max(0, Date.now() / 1000 - when);
  if (seconds < 90) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${plural(minutes, 'minute', 'minutes')} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${plural(hours, 'hour', 'hours')} ago`;
  return `${plural(Math.round(hours / 24), 'day', 'days')} ago`;
}

export function debounce(fn, wait = 220) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

/* libretro-thumbnails names its files after MAME's own <description>, with the
   characters a filesystem dislikes replaced by underscores. That means a screenshot
   URL is derivable from data we already have, with no lookup table and no scraper.

   Asked for through our own server: it serves the picture from disk once it has been
   downloaded, and redirects to the source until then -- so the page looks the same
   either way and gets faster, and works offline, as the cache fills. */
const UNSAFE = /[&*/:`<>?\\|"]/g;

export function artUrl(description, kind = 'Named_Titles') {
  if (!description) return null;
  return `/art/${kind}/${encodeURIComponent(description.replace(UNSAFE, '_'))}.png`;
}

/* Not every machine has a thumbnail -- coverage is around three quarters, and the
   gaps are obscure gambling and bootleg sets. An <img> that 404s must not leave a
   broken-image icon, so the initials stand in. */
export function shot(machine, kind, suffix = '') {
  const wrap = el('div', { class: 'shot' });
  const initials = el('div', { class: 'fallback', text: (machine.name || '??').slice(0, 4).toUpperCase() });
  wrap.append(initials);
  const url = artUrl(machine.description, kind);
  if (!url) return wrap;
  // Transparent, never display:none. A lazily-loaded image with no layout box is
  // never in the viewport, so the browser never fetches it, so `load` never fires and
  // it never becomes visible -- which is a deadlock, and the reason no poster in the
  // grid ever showed a picture. Keeping the box and fading it in works, and the
  // initials show through underneath until the image lands.
  const img = el('img', { loading: 'lazy', alt: '', src: url + suffix });
  img.addEventListener('load', () => { initials.remove(); img.classList.add('on'); });
  img.addEventListener('error', () => img.remove());
  wrap.append(img);
  return wrap;
}

/* One large image for the detail panel: title screen, then in-game shot, then box
   art. When libretro has none of them -- about a quarter of the set, mostly gambling
   and laserdisc machines -- the whole block collapses rather than leaving a screen of
   empty placeholder. */
export function hero(machine, kinds = ['Named_Titles', 'Named_Snaps', 'Named_Boxarts']) {
  const wrap = el('div', { class: 'shot hero-shot' });
  wrap.style.display = 'none';
  let index = 0;
  const attempt = () => {
    if (index >= kinds.length) return;
    const url = artUrl(machine.description, kinds[index]);
    index += 1;
    if (!url) { attempt(); return; }
    const img = el('img', { alt: '', src: url, class: 'on' });
    img.addEventListener('load', () => { clear(wrap); wrap.append(img); wrap.style.display = ''; });
    img.addEventListener('error', attempt);
  };
  attempt();
  return wrap;
}

export function badges(machine) {
  const out = [];
  if (machine.chd) out.push(el('span', { class: 'badge chd', text: 'CHD' }));
  if (machine.vertical) out.push(el('span', { class: 'badge vert', text: 'VERT' }));
  if (machine.clone) out.push(el('span', { class: 'badge clone', text: 'CLONE' }));
  // Everything listed has passed MAME's working filter, so this is not "broken" --
  // it is what MAME says is imperfect or missing about a machine that does run.
  if (machine.condition && machine.condition.length) {
    out.push(el('span', { class: 'badge flaw', title: machine.condition.join(', '),
                          text: 'IMPERFECT' }));
  }
  if (machine.mature) out.push(el('span', { class: 'badge adult', text: '18+' }));
  // In the source folder but not finished -- a placeholder the torrent client has
  // allocated, or a download on its way. Not "missing": it is coming.
  if (machine.partial) out.push(el('span', { class: 'badge move', text: 'downloading' }));
  else if (machine.status) out.push(el('span', { class: `badge ${machine.status}`, text: machine.status }));
  return out;
}

/* A passing notice at the top of the page. Here rather than in app.js so any module
   can report a failed action; a button whose promise rejects in silence looks dead. */
export function banner(text, kind = 'info') {
  const host = document.getElementById('banners');
  if (!host) return;
  const note = el('div', { class: `banner ${kind}`, text });
  host.append(note);
  setTimeout(() => note.remove(), 7000);
}

/* Keyboard access for things that are clickable but are not buttons: a poster, a
   table row, a tree tick. Tab reaches them, Enter or Space activates them. */
export function pressable(node, activate) {
  node.setAttribute('tabindex', '0');
  node.setAttribute('role', 'button');
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(event);
    }
  });
  return node;
}

export function subtitle(machine) {
  return [machine.year, machine.manufacturer].filter(Boolean).join(' · ');
}
