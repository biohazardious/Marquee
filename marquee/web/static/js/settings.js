/* Settings, in the sections Sonarr puts them in: where the files go, what fetches
   them, and where releases come from. */

import { $, append, clear, el } from './util.js';
import { browse, post, state, testClient } from './api.js';

let current = null;
let refreshHook = null;

export function adopt(config) { current = config || {}; }

/* app.js owns the releases poll; the Fetch button here has to be able to start it,
   or the table it fetched never appears until the page is reloaded. */
export function onRefreshReleases(fn) { refreshHook = fn; }

/* Only what is actually on screen. A key left undefined is dropped by JSON.stringify,
   and the server leaves anything it was not sent alone -- so a save issued from
   somewhere else says nothing about the settings rather than saying they are empty.

   It used to fall back to the last adopted config, which reads as the same thing until
   that config has not arrived: then every field reported "" and one Save cleared the
   download client, its credentials and all three paths. Reporting nothing cannot do
   that; reporting a guess can. */
export function values() {
  const read = (id) => ($(id) ? $(id).value.trim() : undefined);
  const checked = (id) => ($(id) ? $(id).checked : undefined);
  const out = {
    rom_dir: read('rom_dir'), chd_dir: read('chd_dir'), copy_path: read('copy_path'),
    download_client: read('download_client'),
    download_username: read('download_username'),
    download_password: read('download_password'),
    download_dir: read('download_dir'),
    remote_path_mappings: $('remote_path_mappings')?.value,
    hardlink: checked('hardlink'),
    parents_only: checked('parents_only'),
    write_gamelist: checked('write_gamelist'),
    copy_artwork: checked('copy_artwork'),
    allow_mature: checked('allow_mature'),
    mature_rom_folder: read('mature_rom_folder'),
    mame_version: read('mame_version'),
  };
  for (const key of Object.keys(out)) if (out[key] === undefined) delete out[key];
  return out;
}

function field(id, label, note, value, type = 'text') {
  return el('label', { class: 'fld' },
    el('span', {}, label, note ? el('em', { text: ` — ${note}` }) : null),
    el('input', { type, id, value: value ?? '' }));
}

const REMOTE = /^(smb|ftp|ftps|sftp|ssh):\/\//i;

function folderField(id, label, note, value, { testable = false } = {}) {
  const input = el('input', { type: 'text', id, value: value ?? '' });
  const result = el('div', { class: 'fld-result' });
  // A share on the console cannot be walked by the picker, and pretending to
  // (it opened at "/") is what lost a whole live test. A remote URL gets tested
  // instead; a local path can have both.
  const test = async () => {
    clear(result);
    result.append(el('div', { class: 'muted small', text: 'Reaching it…' }));
    try {
      const answer = await post('/api/destination/test', { copy_path: input.value.trim() });
      clear(result);
      result.append(el('div', { class: 'banner good', style: 'margin:8px 0 0',
        text: `✓ ${answer.message}${answer.writable === false ? ' Not writable.' : ''}` }));
    } catch (error) {
      clear(result);
      result.append(el('div', { class: 'banner bad', style: 'margin:8px 0 0', text: `✗ ${error.message}` }));
    }
  };
  const browse = () => (REMOTE.test(input.value.trim()) ? test() : picker(input));
  return el('label', { class: 'fld' },
    el('span', {}, label, note ? el('em', { text: ` — ${note}` }) : null),
    el('div', { class: 'rowflex' },
      el('div', { style: 'flex:1' }, input),
      el('button', { class: 'btn sm', text: 'Browse', onclick: browse }),
      testable ? el('button', { class: 'btn sm', text: 'Test', onclick: test }) : null),
    result);
}

/* The release the romset is. Nothing else on this page matters as much: the XML and
   the catlist are fetched for exactly this version, and a fresh install with no
   version chosen used to stop with "could not find a mame*.xml". Left on Automatic,
   the folder name decides ("MAME 0.289 ROMs (non-merged)" says 0.289). */
let versions = null;

export function renderVersions(payload) {
  versions = payload || null;
  const box = $('mameVersionBox');
  if (!box) return;
  const current = ($('mame_version') && $('mame_version').value) ?? (current_() || '');
  const list = (versions && versions.data) || [];
  const signature = `${list.map((entry) => entry.version).join('|')}|${Boolean(versions && versions.running)}`;
  // Polled every few seconds; rebuilding the <select> under an open menu closes it.
  if (box.dataset.signature === signature && $('mame_version')) return;
  box.dataset.signature = signature;
  clear(box);
  const select = el('select', { id: 'mame_version' },
    el('option', { value: '', text: 'Automatic — from the folder name' }),
    list.filter((entry) => entry.usable).map((entry) => el('option', {
      value: entry.version, text: `MAME ${entry.version}`, selected: entry.version === current })));
  if (current && !list.some((entry) => entry.version === current)) {
    // Chosen before the list arrived, or a release the list does not know: still
    // shown, still selected, never silently dropped.
    select.append(el('option', { value: current, text: `MAME ${current}`, selected: true }));
  }
  box.append(select);
  if (versions && versions.running && !list.length) {
    box.append(el('div', { class: 'hint', text: 'Reading the list of releases…' }));
  } else if (versions && versions.error && !list.length) {
    box.append(el('div', { class: 'hint', text: `Could not read the release list: ${versions.error}. Type the version into settings.ini, or leave Automatic.` }));
  } else if (!list.length) {
    post('/api/versions').catch(() => {});
  }
}

function current_() { return current ? current.mame_version : ''; }

export function render() {
  const host = $('settingsBody');
  if (!host) return;
  clear(host);
  // Never draw the form before the settings have arrived. Empty fields are
  // indistinguishable from cleared ones once they exist, and a Save from that state
  // wipes the configuration they were supposed to be showing.
  if (current === null) {
    host.append(el('div', { class: 'muted', style: 'padding:14px',
                            text: 'Reading settings…' }));
    return;
  }
  const config = current;

  // The version box is filled after the form is on the page, from the catalogue.
  queueMicrotask(() => renderVersions(versions));

  host.append(
    el('div', { class: 'panel' },
      el('h2', {}, 'Media management',
        el('span', { class: 'sub', text: 'where the romset is, and where the library goes' })),
      el('div', { class: 'body' },
        folderField('rom_dir', 'ROM folder', 'the flat folder of .zip files', config.rom_dir),
        folderField('chd_dir', 'CHD folder', 'one subfolder per machine', config.chd_dir),
        folderField('copy_path', 'Library', 'local path, or smb://host/share/path — Test reaches it',
          config.copy_path, { testable: true }),
        el('label', { class: 'fld' },
          el('span', {}, 'MAME release', el('em', { text: ' — the XML and catlist are fetched for exactly this version' })),
          el('div', { id: 'mameVersionBox' })),
        el('label', { class: 'check' },
          el('input', { type: 'checkbox', id: 'hardlink', checked: config.hardlink }),
          el('span', {}, el('b', { text: 'Hardlink instead of copying' }),
            el('div', { class: 'hint', text:
              'When the library sits on the same filesystem as the downloads, this costs no '
              + 'extra space and lets seeding carry on. A hardlinked file is the same file, '
              + 'so anything that writes to a library ROM writes through to the torrent too.' }))),
        el('label', { class: 'check' },
          el('input', { type: 'checkbox', id: 'parents_only', checked: config.parents_only }),
          el('span', {}, el('b', { text: 'One game, one ROM' }),
            el('div', { class: 'hint', text:
              'Keep each game\u2019s parent and leave its other revisions out. On MAME '
              + '0.289 that is 7,538 fewer machines and 80 GB less \u2014 Dragon\u2019s Lair '
              + 'alone ships four revisions at 11.5 GB each. A game whose parent did '
              + 'not survive the filters keeps one version, so nothing disappears.' }))),
        el('label', { class: 'check' },
          el('input', { type: 'checkbox', id: 'allow_mature', checked: config.allow_mature }),
          el('span', {}, el('b', { text: 'Include adult titles' }),
            el('div', { class: 'hint', text: 'Filed under their own folder.' }))),
        field('mature_rom_folder', 'Adult folder', null, config.mature_rom_folder))),

    el('div', { class: 'panel' },
      el('h2', {}, 'On the console',
        el('span', { class: 'sub', text: 'what EmulationStation sees' })),
      el('div', { class: 'body' },
        el('label', { class: 'check' },
          el('input', { type: 'checkbox', id: 'write_gamelist',
            checked: config.write_gamelist }),
          el('span', {}, el('b', { text: 'Write gamelist.xml' }),
            el('div', { class: 'hint', text:
              'One list at the root of the library, so the console shows '
              + '\u201cMetal Slug - Super Vehicle-001, 1996, Nazca, 2 players\u201d '
              + 'instead of \u201cmslug\u201d. Everything in it is already known \u2014 '
              + 'nothing is scraped.' }))),
        el('label', { class: 'check' },
          el('input', { type: 'checkbox', id: 'copy_artwork',
            checked: config.copy_artwork }),
          el('span', {}, el('b', { text: 'Copy artwork into the library' }),
            el('div', { class: 'hint', text:
              'Place the downloaded pictures beside the games so the console shows '
              + 'them. Download them first from the Library page.' }))))),

    el('div', { class: 'panel' },
      el('h2', {}, 'Download client',
        el('span', { class: 'sub', text: 'qBittorrent' }),
        el('span', { class: 'spacer' }),
        el('button', { class: 'btn sm', id: 'testClient', text: 'Test', onclick: runTest })),
      el('div', { class: 'body' },
        el('div', { id: 'clientResult' }),
        field('download_client', 'URL', 'e.g. http://192.168.1.10:8080/', config.download_client),
        el('div', { class: 'rowflex', style: 'gap:14px;align-items:flex-start' },
          el('div', { style: 'flex:1' }, field('download_username', 'Username', null, config.download_username)),
          el('div', { style: 'flex:1' }, field('download_password', 'Password', null, config.download_password, 'password'))),
        folderField('download_dir', 'Download folder',
          'where this app can read the client\u2019s downloads', config.download_dir),
        el('label', { class: 'fld' },
          el('span', {}, 'Remote path mappings',
            el('em', { text: ' — one “remote → local” per line' })),
          el('textarea', { id: 'remote_path_mappings', placeholder: '/downloads -> /mnt/nas/downloads' },
            config.remote_path_mappings || ''),
          el('div', { class: 'hint', text:
            'Where downloads go is decided in qBittorrent, per category \u2014 this app '
            + 'never dictates it. But qBittorrent reports paths as it sees them, so if it '
            + 'runs in another container or on another host, rewrite them here or the '
            + 'library step will find nothing.' })))),

    el('div', { class: 'panel' },
      el('h2', {}, 'Indexer', el('span', { class: 'sub', text: 'Pleasuredome' })),
      el('div', { class: 'body' },
        el('p', { class: 'muted', style: 'margin-top:0' },
          'Releases come from the public Pleasuredome index. Only the non-merged full ROM '
          + 'set and the merged CHD set are used to build a library — non-merged zips are '
          + 'self-contained, which is what makes picking single games work.'),
        el('div', { id: 'releaseList' }))),
  );
}

async function runTest() {
  const button = $('testClient');
  const result = $('clientResult');
  button.disabled = true;
  button.textContent = 'Testing…';
  clear(result);
  try {
    const answer = await testClient(values());
    result.append(el('div', { class: 'banner good', text: `✓ ${answer.message}` }));
  } catch (error) {
    result.append(el('div', { class: 'banner bad', text: `✗ ${error.message}` }));
  } finally {
    button.disabled = false;
    button.textContent = 'Test';
  }
}

export function renderReleases(payload) {
  const host = $('releaseList');
  if (!host) return;
  clear(host);

  if (payload?.running) {
    host.append(el('div', { class: 'muted', text: 'Reading the index…' }));
    return;
  }
  if (payload?.error) {
    host.append(el('div', { class: 'banner bad', text: payload.error }));
  }
  if (!payload?.data) {
    host.append(el('button', {
      class: 'btn', text: 'Fetch releases',
      onclick: async (event) => {
        event.target.disabled = true;
        event.target.textContent = 'Fetching…';
        try {
          await post('/api/releases/refresh');
          refreshHook?.();
        } catch (error) {
          event.target.disabled = false;
          event.target.textContent = error.message;
        }
      },
    }));
    return;
  }

  const full = payload.data.filter((release) => release.full_set);
  host.append(el('table', { class: 'grid' },
    el('thead', {}, el('tr', {},
      el('th', { text: 'Set' }), el('th', { text: 'Version' }),
      el('th', { text: 'Variant' }), el('th', { text: 'Infohash' }))),
    el('tbody', {}, payload.data.map((release) => el('tr', { style: 'cursor:default' },
      el('td', {}, el('b', { text: release.name }),
        release.from_version ? el('small', { class: 'dim', text: ` update from ${release.from_version}` }) : null),
      el('td', { class: 'mono', text: release.version }),
      el('td', { class: 'muted', text: release.variant || '-' }),
      el('td', { class: 'mono dim', style: 'font-size:11px', text: release.infohash.slice(0, 12) }))))));
  host.append(el('div', { class: 'hint' },
    `${full.length} full sets of ${payload.data.length} listed.`));
}

/* ---------------- folder picker ---------------- */

async function picker(input) {
  const scrim = el('div', { class: 'scrim', onclick: () => shut() });
  const panel = el('aside', { class: 'drawer' });
  const body = el('div', { class: 'scroll' });
  const onKey = (event) => { if (event.key === 'Escape') shut(); };
  const shut = () => {
    scrim.remove(); panel.remove();
    document.removeEventListener('keydown', onKey);
  };
  document.addEventListener('keydown', onKey);

  panel.append(
    el('header', {}, el('div', {}, el('h3', { text: 'Choose a folder' })),
      el('button', { class: 'close', text: '×', onclick: shut })),
    body);
  document.body.append(scrim, panel);

  let here = input.value.trim() || '/';
  const show = async () => {
    clear(body);
    body.append(el('div', { class: 'muted', style: 'margin-bottom:10px' }, 'Loading…'));
    let listing;
    try {
      listing = await browse(here);
    } catch (error) {
      clear(body);
      body.append(el('div', { class: 'banner bad', text: error.message }));
      return;
    }
    here = listing.path;
    clear(body);
    append(body,
      // Somewhere to start from. In a container the useful paths are the mounted
      // volumes and there is no home directory to fall back on.
      el('div', { class: 'rowflex', style: 'margin-bottom:12px' },
        (listing.roots || []).map((root) => el('button', {
          class: `btn sm ${root === here ? 'primary' : ''}`, text: root,
          onclick: () => { here = root; show(); },
        }))),
      el('div', { class: 'mono', style: 'margin-bottom:12px;word-break:break-all', text: here }),
      el('div', { class: 'rowflex', style: 'margin-bottom:14px' },
        el('button', { class: 'btn primary sm', text: 'Use this folder',
          onclick: () => { input.value = here; shut(); } }),
        listing.parent ? el('button', { class: 'btn sm', text: '↑ Up',
          onclick: () => { here = listing.parent; show(); } }) : null),
      listing.note ? el('div', { class: 'banner warn', text: listing.note }) : null,
      el('div', { class: 'tree' }, (listing.entries || []).map((entry) => el('div', {
        class: 'row', onclick: () => { here = entry.path; show(); },
      }, el('span', { class: 'twist', text: '▸' }), el('span', { class: 'name', text: entry.name })))));
    if (!(listing.entries || []).length) {
      body.append(el('div', { class: 'empty', text: 'No subfolders here.' }));
    }
  };
  show();
}

export { state };
