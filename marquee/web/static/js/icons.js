/* One stroke icon set, drawn here rather than pulled from a font or a CDN: the page
   works offline and the pictures have to look like one family. Every icon is a
   24-unit box, 1.75 stroke, round joins -- the same pen for all of them, which is
   what the emoji they replace could never manage across platforms. */

import { el } from './util.js';

const PATHS = {
  overview: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="5" rx="1.5"/><rect x="13" y="10" width="8" height="11" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/>',
  library: '<path d="M4 6.5A1.5 1.5 0 0 1 5.5 5h13A1.5 1.5 0 0 1 20 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/><path d="M8 5v14M16 5v14M4 10h4M4 14h4M16 10h4M16 14h4"/>',
  selection: '<path d="M4 6.5A1.5 1.5 0 0 1 5.5 5h13A1.5 1.5 0 0 1 20 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/><path d="m8.5 12 2.5 2.5 5-5"/>',
  wanted: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
  leftout: '<path d="M4 8h16v10a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18z"/><path d="M4 8l1.2-3.2A1.5 1.5 0 0 1 6.6 4h10.8a1.5 1.5 0 0 1 1.4.8L20 8M10 12h4"/>',
  transfer: '<path d="M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
  system: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  warn: '<path d="M12 3.5 21 19.5H3z"/><path d="M12 10v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M4 19h16"/>',
  play: '<path d="M7 5.5v13l11-6.5z"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  folder: '<path d="M3.5 7A1.5 1.5 0 0 1 5 5.5h4l2 2h8A1.5 1.5 0 0 1 20.5 9v8.5A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
  disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
  bolt: '<path d="M13 3 5 13.5h6L10.5 21 19 10.5h-6z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
};

export function icon(name, extra = '') {
  const body = PATHS[name] || PATHS.info;
  return el('span', {
    class: `ico ${extra}`.trim(), 'aria-hidden': 'true',
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" `
      + `stroke-linecap="round" stroke-linejoin="round">${body}</svg>`,
  });
}

export const NAMES = Object.keys(PATHS);
