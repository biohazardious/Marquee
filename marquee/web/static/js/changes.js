/* What pressing Transfer will actually do, before it does it.

   Everything here has been worked out since the first version -- the sync report is
   what the copy runs from -- and none of it was ever on screen. A run that copies,
   replaces, renames and deletes should be readable beforehand, not afterwards in a
   log: which games arrive, which are replaced and why, which files are about to be
   deleted and what they were. */

import { icon } from './icons.js';
import { $, append, clear, count, el, human, plural, pressable, store, stored } from './util.js';
import { api, lock, post, state } from './api.js';

const V = {
  data: null, kind: '', query: '', offset: 0, error: '', busy: false,
  // How the rows of a kind are shown: one list, or the Selection page's tree of
  // genre -> category -> game. Remembered, because it is a way of reading, not a
  // one-off choice.
  view: stored('marquee.changes.view', 'list'),
  open: new Set(), branches: {}, loading: new Set(),
  // Deleting is never the default, and never carried over from a run.
  remove: false,
  // The delete has to be asked for twice: once with the box, once on the button.
  // Holds the number of files the second click was armed for, so a reload that
  // changes what would be deleted asks again.
  armed: false,
};

/* Their own colours, the same ones the library's status badges use. */
const TONE = {
  new: 'new', update: 'update', move: 'move', keep: 'keep', orphan: 'bad',
  fetch: 'missing',
};

export async function load(kind) {
  if (lock.on) return;
  if (kind !== undefined) { V.kind = kind; V.offset = 0; }
  // A confirmation belongs to the figures it was given against. Kept across a
  // reload, one click deleted whatever the new plan said to.
  V.armed = false;
  V.busy = true;
  render();
  try {
    V.data = await api('/api/changes?' + new URLSearchParams({
      kind: V.kind, q: V.query, offset: V.offset, limit: 200,
      group: V.view === 'tree' ? '1' : '0',
    }).toString());
    V.error = '';
    V.branches = {};
  } catch (error) {
    V.error = error.message;
  }
  V.busy = false;
  render();
}

function setView(view) {
  V.view = view;
  store('marquee.changes.view', view);
  V.offset = 0;
  load();
}

/* The games of one category, fetched when its branch is opened. */
async function loadBranch(category) {
  const key = `${V.kind}|${category}`;
  if (V.branches[key] || V.loading.has(key)) return;
  V.loading.add(key);
  try {
    const found = await api('/api/changes?' + new URLSearchParams({
      kind: V.kind, q: V.query, category, limit: 500,
    }).toString());
    V.branches[key] = found.rows;
  } catch (error) {
    V.branches[key] = [];
    V.error = error.message;
  } finally {
    V.loading.delete(key);
    render();
  }
}

function pick(kind) {
  V.kind = V.kind === kind ? '' : kind;
  V.offset = 0;
  V.query = '';
  load();
}

/* ---------------- the five things a run can do ---------------- */

function kindRow(entry) {
  const chosen = V.kind === entry.kind;
  const nothing = !entry.files;
  const choose = () => { if (!nothing) pick(entry.kind); };
  const row = el('tr', {
    // Fetch first is not something this run does, so it sits apart from the four
    // that are; it is here because leaving it out is what makes "387 out of date"
    // and "Replace: 0 files" look like a contradiction.
    class: `${chosen ? 'on' : ''}${entry.kind === 'fetch' ? ' apart' : ''}`,
    style: nothing ? 'cursor:default;opacity:.55' : '',
    onclick: choose,
  },
    el('td', {}, el('span', { class: `badge ${TONE[entry.kind]}`, text: entry.label })),
    el('td', { class: 'muted', text: entry.note }),
    el('td', { class: 'num nowrap',
      text: entry.kind === 'orphan' ? '—' : `${plural(entry.machines, 'game', 'games')}` }),
    el('td', { class: 'num nowrap',
      text: entry.kind === 'fetch' ? '—' : `${plural(entry.files, 'file', 'files')}` }),
    el('td', { class: 'num nowrap', text: entry.bytes ? human(entry.bytes) : '—' }));
  return nothing ? row : pressable(row, choose);
}

function summaryPanel(data) {
  return el('div', { class: 'panel' },
    el('h2', {}, 'What the next transfer will do',
      el('span', { class: 'sub', text: `${data.to_transfer_human} to copy` })),
    el('div', { class: 'body tight' },
      el('table', { class: 'grid' },
        el('tbody', {}, data.kinds.map(kindRow))),
      el('div', { class: 'hint',
        text: 'Pick a row to see exactly which games and files it means. Nothing here '
          + 'has happened yet — this is the difference between what the library '
          + 'holds and what your selection asks for.' })));
}

/* Until something has read inside the files, "replace" only knows what the sizes say,
   and a redump that weighs the same as the dump it replaces is invisible to that. */
function checkNote(data) {
  if (data.checked) {
    const found = data.states || {};
    const bad = (found.stale || 0) + (found.damaged || 0);
    if (!bad) {
      return el('div', { class: 'banner good' },
        'Every game in the library was read and matches this release.');
    }
    // Where they end up depends on whether there is a file here to put in their
    // place. Saying "counted under Replace" when Replace reads zero is the page
    // telling you something it can see is not true.
    const replacing = (data.kinds.find((one) => one.kind === 'update') || {}).machines;
    return el('div', { class: 'banner warn' },
      `${plural(bad, 'game', 'games')} in the library are not what this release says they are. `
      + (replacing
        ? 'The ones with a downloaded file to put in their place are under Replace; '
          + 'the rest are under Fetch first.'
        : 'There is nothing in the download folder to put in their place, so they are '
          + 'under Fetch first — they have to be downloaded before a transfer can fix '
          + 'them.'));
  }
  const button = el('button', { class: 'btn sm', text: 'Check the library' });
  button.onclick = async () => {
    button.disabled = true;
    try { await post('/api/check', {}); } catch (error) { V.error = error.message; render(); }
  };
  return el('div', { class: 'banner info' },
    'The library has not been checked, so "Replace" goes by name and size only — '
    + 'a redump that weighs the same as the file it replaces is not caught. ',
    button);
}

/* ---------------- deleting ---------------- */

function deletePanel(data) {
  const entry = data.kinds.find((one) => one.kind === 'orphan') || { files: 0 };
  const box = el('input', { type: 'checkbox' });
  box.checked = V.remove;
  box.onchange = () => { V.remove = box.checked; V.armed = false; render(); };
  return el('label', { class: 'check' }, box,
    el('span', {},
      el('b', { text: `Also delete the ${plural(entry.files, 'file', 'files')} nothing wants` }),
      el('div', { class: 'hint',
        text: entry.files
          ? `${human(entry.bytes)} would be removed. Only .zip and .chd files are ever `
            + 'touched; artwork, gamelists and anything else are left alone. Leave this '
            + 'unticked and they simply stay where they are.'
          : 'There is nothing at the destination that the selection does not want.' })));
}

/* ---------------- the rows for one kind ---------------- */

function gameRow(row) {
  return el('tr', { style: 'cursor:default' },
    el('td', { class: 'title-cell' },
      el('b', { text: row.description || row.name }),
      el('small', { text: row.name })),
    el('td', { class: 'muted', text: row.category || row.folder }),
    el('td', {}, row.state && row.state !== 'current'
      ? el('span', { class: 'badge update', title: (row.state_detail || []).join(', '),
        text: row.state })
      : null,
    row.chd ? el('span', { class: 'badge chd', text: 'CHD' }) : null),
    // A move is the interesting row on an upgrade: the same file, one folder out,
    // renamed rather than fetched again. Both ends of it, or it says nothing.
    el('td', { class: 'muted mono small' },
      row.from ? el('div', { text: row.from }) : null,
      row.from ? el('div', { text: `→ ${row.paths[0]}` }) : null,
      !row.from && row.files > 1 ? `${row.files} files` : null),
    el('td', { class: 'num nowrap', text: row.bytes_human }));
}

/* Wanted, with nothing here to copy from. A row about a game that does not exist on
   this machine yet: no path to show, and a reason instead of a status. */
function fetchRow(row) {
  return el('tr', { style: 'cursor:default' },
    el('td', { class: 'title-cell' },
      el('b', { text: row.description || row.name }),
      el('small', { text: row.name })),
    el('td', { class: 'muted', text: row.category || row.folder }),
    el('td', {}, row.chd ? el('span', { class: 'badge chd', text: 'CHD' }) : null),
    el('td', { class: 'muted small', text: row.why }),
    el('td', { class: 'num nowrap', text: row.bytes_human }));
}

function fileRow(row) {
  return el('tr', { style: 'cursor:default' },
    el('td', { class: 'title-cell' },
      el('b', { text: row.description || row.name }),
      el('small', { class: 'mono', text: row.path })),
    el('td', { class: 'muted', text: row.why }),
    el('td', {}),
    el('td', {}),
    el('td', { class: 'num nowrap', text: row.bytes_human }));
}

/* ---------------- the tree ---------------- */

const INDENT = [10, 26, 50];

/* What a branch holds, in words: games and files, and whatever makes this kind what
   it is -- disks for the weight, flagged files for a replace, origins for a move. */
function detailOf(node, kind) {
  const parts = [`${count(node.machines)} ${kind === 'orphan' ? 'files' : node.machines === 1 ? 'game' : 'games'}`];
  if (kind !== 'orphan' && node.files !== node.machines) parts.push(`${plural(node.files, 'file', 'files')}`);
  if (node.disks) parts.push(`${count(node.disks)} with CHD`);
  if (node.flagged) parts.push(`${count(node.flagged)} flagged by the check`);
  if (kind === 'move' && node.moved) parts.push('renamed in place');
  if (node.share !== undefined) parts.push(`${node.share}% of this run`);
  return parts.join(' · ');
}

function treeRow(depth, label, open, onOpen, node, kind) {
  return pressable(el('div', {
    class: `row ${depth === 0 ? 'genre' : 'cat'}`,
    style: `padding-left:${INDENT[depth]}px`, onclick: onOpen,
  },
    el('span', { class: 'twist', text: open ? '▾' : '▸' }),
    el('span', { class: 'name', text: label }),
    el('span', { class: 'detail muted small', text: detailOf(node, kind) }),
    el('span', { class: 'size', text: human(node.bytes) })), onOpen);
}

function treeGame(row) {
  const isFile = row.kind === 'orphan';
  return el('div', { class: 'row game', style: `padding-left:${INDENT[2]}px` },
    el('span', { class: 'twist' }),
    el('span', { class: 'name' },
      el('span', { text: row.description || row.name }),
      el('small', { text: isFile ? row.path : row.name })),
    el('span', { class: 'rowflex' },
      row.chd ? el('span', { class: 'badge chd', text: 'CHD' }) : null,
      row.state && row.state !== 'current'
        ? el('span', { class: 'badge update', text: row.state }) : null,
      row.from ? el('span', { class: 'muted small mono', text: `from ${row.from}` }) : null,
      row.why && (isFile || row.kind === 'fetch')
        ? el('span', { class: 'muted small', text: row.why }) : null),
    el('span', { class: 'size', text: row.bytes_human }));
}

function treePanel(data) {
  const nodes = [];
  for (const genre of data.tree || []) {
    const gkey = `g:${genre.name}`;
    const gopen = V.open.has(gkey);
    nodes.push(treeRow(0, genre.name, gopen,
      () => { gopen ? V.open.delete(gkey) : V.open.add(gkey); render(); },
      genre, data.kind));
    if (!gopen) continue;
    for (const cat of genre.categories) {
      const ckey = `c:${cat.name}`;
      const copen = V.open.has(ckey);
      nodes.push(treeRow(1, cat.label || cat.name, copen,
        () => {
          if (copen) V.open.delete(ckey);
          else V.open.add(ckey);
          render();
        }, cat, data.kind));
      if (!copen) continue;
      const key = `${V.kind}|${cat.name}`;
      const rows = V.branches[key];
      if (!rows) {
        // Asked for here, not on the click: a branch left open across a reload --
        // switching to the list and back, a new search -- used to sit on "loading…"
        // until it was closed and opened again.
        loadBranch(cat.name);
        nodes.push(el('div', { class: 'row game dim', text: 'loading…' }));
      } else rows.forEach((row) => nodes.push(treeGame(row)));
    }
  }
  return nodes.length
    ? el('div', { class: 'tree', style: 'padding:6px 8px' }, nodes)
    : el('div', { class: 'muted', style: 'padding:12px 2px', text: 'Nothing matches.' });
}

function pager(data) {
  const shown = data.offset + data.rows.length;
  if (data.total <= data.rows.length && !data.offset) return null;
  return el('div', { class: 'rowflex', style: 'padding:8px 2px' },
    el('span', { class: 'muted',
      text: `${count(data.offset + 1)}–${count(shown)} of ${count(data.total)}` }),
    el('span', { style: 'flex:1' }),
    el('button', {
      class: 'btn sm', disabled: !data.offset,
      text: 'Back', onclick: () => { V.offset = Math.max(0, V.offset - 200); load(); },
    }),
    el('button', {
      class: 'btn sm', disabled: shown >= data.total,
      text: 'More', onclick: () => { V.offset += 200; load(); },
    }));
}

function rowsPanel(data) {
  const entry = data.kinds.find((one) => one.kind === data.kind);
  if (!entry) return null;
  const search = el('input', {
    type: 'text', placeholder: 'Find one…', value: V.query, id: 'changeSearch',
  });
  search.onchange = () => { V.query = search.value.trim(); V.offset = 0; load(); };

  const draw = { orphan: fileRow, fetch: fetchRow }[data.kind] || gameRow;
  const tree = V.view === 'tree';
  const body = tree
    ? treePanel(data)
    : data.rows.length
      ? el('table', { class: 'grid' }, el('tbody', {}, data.rows.map(draw)))
      : el('div', { class: 'muted', style: 'padding:12px 2px', text: 'Nothing matches.' });

  const views = el('div', { class: 'seg' },
    el('button', { class: tree ? '' : 'on', text: 'List', onclick: () => setView('list') }),
    el('button', { class: tree ? 'on' : '', text: 'By genre', onclick: () => setView('tree') }));
  views.children[0].prepend(icon('menu'));
  views.children[1].prepend(icon('selection'));

  return el('div', { class: 'panel' },
    el('h2', {}, entry.label,
      el('span', { class: 'sub',
        text: `${count(data.total)} ${data.kind === 'orphan' ? 'files' : 'games'} · `
          + `${data.bytes_shown_human}` }),
      el('span', { class: 'spacer' }),
      el('span', { class: 'search' }, icon('search'), search),
      views,
      el('button', { class: 'btn sm', text: 'Close', onclick: () => pick(data.kind) })),
    el('div', { class: 'body tight' }, body, tree ? null : pager(data),
      data.kind === 'fetch'
        ? el('div', { class: 'hint' },
          'This run cannot do anything with these: there is no file here to place. ',
          el('a', { href: '#wanted', text: 'Wanted' }),
          ' prices them and asks the download client for them.')
        : null));
}

/* ---------------- running it ---------------- */

function goPanel(data) {
  const busy = ['planning', 'copying', 'checking'].includes(state.data.state);
  const go = el('button', {
    class: 'btn primary', disabled: busy,
    text: busy ? 'Busy…' : 'Start transfer',
  });
  const deleting = Boolean(V.remove && data.delete_bytes);
  const orphans = data.kinds.find((one) => one.kind === 'orphan') || { files: 0 };
  const armed = deleting && V.armed === orphans.files;
  if (deleting) {
    go.textContent = armed
      ? `Delete ${plural(orphans.files, 'file', 'files')} and start — sure?`
      : `Delete ${plural(orphans.files, 'file', 'files')} and start`;
    go.classList.add('danger');
  }
  go.onclick = async () => {
    if (deleting && !armed) {
      // The first click arms it; the second does it. Ticking the box was already a
      // choice, but a run that removes 11 GB should not start on one press.
      V.armed = orphans.files;
      render();
      return;
    }
    go.disabled = true;
    try {
      await post('/api/copy', { delete_orphans: Boolean(V.remove) });
      // Never carried over: the next visit starts with the box clear again.
      V.remove = false;
      V.armed = false;
      location.hash = '#activity';
    } catch (error) {
      V.error = error.message;
      V.armed = false;
      render();
    }
  };
  const note = data.waiting
    ? el('span', { class: 'muted' },
      `${count(data.waiting)} more games are selected but not downloaded yet — `,
      el('a', { href: '#wanted', text: 'see what they cost' }))
    : null;
  // Everything the button is about to do, not just the copying: on an upgrade most
  // of the work is renames, and "0 B to copy" beside 944 of them reads as "nothing
  // will happen".
  const moving = data.kinds.find((one) => one.kind === 'move') || { files: 0 };
  return el('div', { class: 'rowflex', style: 'gap:12px;margin-top:4px' },
    go,
    el('b', { text: `${data.to_transfer_human} to copy` }),
    moving.files
      ? el('b', { text: `${plural(moving.files, 'file', 'files')} to move` })
      : null,
    V.remove && data.delete_bytes
      ? el('b', { class: 'bad', text: `${data.delete_bytes_human} to delete` })
      : null,
    el('span', { style: 'flex:1' }),
    note);
}

export function render() {
  const host = $('changesBody');
  if (!host) return;
  clear(host);

  if (V.error) host.append(el('div', { class: 'banner bad', text: V.error }));

  const data = V.data;
  if (!data || !data.compared) {
    host.append(el('div', { class: 'empty' },
      el('div', { class: 'big' }, icon('transfer')),
      el('div', {
        text: V.busy
          ? 'Working out the difference…'
          : 'Build a plan first. Marquee then compares it with what the library '
            + 'already holds, and everything the next transfer would do is listed here.',
      })));
    return;
  }

  append(host, summaryPanel(data), checkNote(data), rowsPanel(data),
    deletePanel(data), goPanel(data));
}
