/* The genre -> category -> game tree.

   One tree, three levels, every row a tick box. The rules underneath are small but
   fiddly -- turning one game back on inside a genre that was switched off wholesale
   has to leave its siblings off -- so they live in their own block and are exercised
   by tests/test_selection_logic.py under node. */

import { icon } from './icons.js';
import { $, append, badges, banner, clear, count, el, plural, sortControl } from './util.js';
import { INDENT, TREE_SORTS, branchRow, gameOrder, keepingFocus, orderCats, orderGenres,
  tickBox, wantedOf, weightOf } from './tree.js';
import { api, machines as fetchMachines } from './api.js';
import { open as openGame } from './library.js';

// Rows drawn for one category, and for one filtered view. The server caps it at 500.
const ROWS = 500;

export const S = {
  genres: [], cats: [], machines: {}, members: {}, romCat: {}, romBytes: {}, romLib: {},
  adultFolder: 'ZZ-Adult',
  outGenres: new Set(), outCats: new Set(), outRoms: new Set(),
  open: new Set(), openCats: new Set(), loading: new Set(),
  hits: null, hitTotal: 0, busy: false,
  sort: { sort: 'size', dir: 'desc' },
  // The same filters the library has. A tree organised by genre is the right shape
  // for browsing and the wrong one for "drop every imperfect shooter", so when any of
  // these is set the page switches to a flat list of what matched.
  filters: { query: '', genre: '', condition: '', mature: '', have: '' },
};

/* Where a game sits and what it weighs, kept for every row that has passed through --
   so unticking one can be taken off the running total without asking the server what
   it cost. */
function remember(m) {
  S.romCat[m.name] = m.category;
  if (m.bytes !== undefined) S.romBytes[m.name] = m.bytes;
  if (m.library !== undefined) S.romLib[m.name] = m.library;
}

let onChange = () => {};
export function onSelectionChange(fn) { onChange = fn; }

/* selection-logic:start  (extracted and exercised by tests/test_selection_logic.py) */
/* Three lists decide what is in: genres that are out wholesale, categories that are out,
   and individual games that are out. Unticking records at the level you clicked; ticking
   clears whatever was hiding it and pushes that exclusion down onto the siblings, so one
   game coming back never drags its neighbours with it.

   Every one of those rules needs to know a category's *whole* membership. Judging it on
   the rows that happened to be fetched -- 500 of the 965 in Slot Machine / Video Slot --
   makes the tick box report the wrong state and makes pushing an exclusion down quietly
   put the other 465 back. So membership is its own thing, `S.members`, and is loaded
   before any rule that needs it runs. */
/* A *group* is one genre on one side of the adult line: {genre, mature}. catlist marks
   adult categories in the category name itself, so "Tabletop / Mahjong * Mature *" is
   already a different category from "Tabletop / Mahjong" and is filed under its own
   folder -- the split is real data, not a flag we invent here. The tree shows it the
   way the library is laid out: the genres, then one ZZ-Adult branch with the same
   genres and categories underneath it. */
function allCatsOf(genre){ return S.cats.filter(c => c.genre === genre); }
function catsOf(group){
  return S.cats.filter(c => c.genre === group.genre
                       && Boolean(c.mature) === Boolean(group.mature));
}
function otherHalfOf(group){
  return S.cats.filter(c => c.genre === group.genre
                       && Boolean(c.mature) !== Boolean(group.mature));
}
function catOut(c){ return S.outGenres.has(c.genre) || S.outCats.has(c.name); }
function machineOut(m){
  return S.outRoms.has(m.name) || S.outGenres.has(m.genre) || S.outCats.has(m.category);
}
function membersOf(category){ return S.members[category] || null; }
function rollUp(states){
  if (!states.length) return "in";
  if (states.every(s => s === "out")) return "out";
  return states.every(s => s === "in") ? "in" : "some";
}
function catState(c){
  if (catOut(c)) return "out";
  const members = membersOf(c.name);
  if (members){
    const gone = members.filter(name => S.outRoms.has(name)).length;
    if (!gone) return "in";
    // Every game unticked one at a time is the same category as one unticked
    // wholesale, and has to look like it -- otherwise a bulk "leave all out" leaves a
    // row that still reads half-in and a box that puts everything back when clicked.
    return gone === members.length ? "out" : "some";
  }
  for (const name of S.outRoms) if (S.romCat[name] === c.name) return "some";
  return "in";
}
function groupState(group){ return rollUp(catsOf(group).map(catState)); }
function sideState(mature){
  return rollUp(S.cats.filter(c => Boolean(c.mature) === Boolean(mature)).map(catState));
}
function clearRoms(category){
  (membersOf(category) || []).forEach(name => S.outRoms.delete(name));
}
/* Directional, not a toggle. The adult branch switches a dozen groups at once and has
   to drive them all to the same state; a group that is half on would otherwise flip
   the wrong way. */
function groupOff(group){
  // When the genre has nothing on the other side of the line, record it as a genre:
  // that is what the settings file has always held and it keeps it short.
  if (!otherHalfOf(group).length) S.outGenres.add(group.genre);
  else catsOf(group).forEach(c => S.outCats.add(c.name));
}
function groupOn(group){
  // A whole-genre exclusion covers the other side too, so it is pushed down onto that
  // side before it is cleared -- switching the adult half back on must not drag the
  // rest of the genre with it.
  if (S.outGenres.has(group.genre)){
    S.outGenres.delete(group.genre);
    otherHalfOf(group).forEach(c => S.outCats.add(c.name));
  }
  catsOf(group).forEach(c => { S.outCats.delete(c.name); clearRoms(c.name); });
}
function toggleGroup(group){
  if (groupState(group) === "in") groupOff(group); else groupOn(group);
  changed();
}
function toggleSide(mature){
  const on = sideState(mature) !== "in";
  const genres = [...new Set(S.cats.filter(c => Boolean(c.mature) === Boolean(mature))
                              .map(c => c.genre))];
  for (const genre of genres) (on ? groupOn : groupOff)({genre, mature});
  changed();
}
function toggleCat(c){
  if (catState(c) === "in"){ S.outCats.add(c.name); }
  else {
    if (S.outGenres.has(c.genre)){
      S.outGenres.delete(c.genre);
      allCatsOf(c.genre).forEach(o => { if (o.name !== c.name) S.outCats.add(o.name); });
    }
    S.outCats.delete(c.name);
    clearRoms(c.name);
  }
  changed();
}
function toggleMachine(m){
  if (!machineOut(m)){ S.outRoms.add(m.name); S.romCat[m.name] = m.category; changed(); return; }
  // It was hidden by its category or its whole genre; clearing that would silently bring
  // every sibling back with it, so the exclusion moves down a level instead.
  const hiddenByParent = S.outGenres.has(m.genre) || S.outCats.has(m.category);
  if (S.outGenres.has(m.genre)){
    S.outGenres.delete(m.genre);
    allCatsOf(m.genre).forEach(c => { if (c.name !== m.category) S.outCats.add(c.name); });
  }
  S.outCats.delete(m.category);
  if (hiddenByParent){
    (membersOf(m.category) || []).forEach(name => {
      if (name !== m.name){ S.outRoms.add(name); S.romCat[name] = m.category; }
    });
  }
  S.outRoms.delete(m.name);
  changed();
}
/* What each rule is about to need the membership of, before it runs. Unticking never
   needs any: it only ever adds one name to a list. Ticking does, because it has to
   push the exclusion it clears down onto siblings. */
function groupNeeds(group){
  return groupState(group) === "in" ? [] : [{genre: group.genre}];
}
function catNeeds(c){ return catState(c) === "in" ? [] : [{category: c.name}]; }
function needsMembers(m){
  if (!machineOut(m)) return [];
  if (S.outGenres.has(m.genre)) return [{genre: m.genre}];
  if (S.outCats.has(m.category)) return [{category: m.category}];
  return [];
}
/* Tick or untick a whole filtered list. Deliberately one call to toggleMachine per
   game rather than a second set of rules: unticking one game inside a genre that is
   out wholesale has to leave its siblings out, and there is no reason for that to be
   written twice. Rendering is held off until the end -- 1,500 redraws is a hang. */
let quiet = false;
function setAll(machines, wanted){
  quiet = true;
  try {
    for (const m of machines){
      if (wanted === !machineOut(m)) continue;
      toggleMachine(m);
    }
  } finally {
    quiet = false;
  }
  changed();
}
/* selection-logic:end */

export { allCatsOf, catNeeds, catOut, catState, catsOf, groupNeeds, groupOff, groupOn,
         groupState,
         machineOut, membersOf, needsMembers, otherHalfOf, setAll, sideState,
         toggleCat, toggleGroup, toggleMachine, toggleSide };

function changed() {
  if (quiet) return;
  render();
  onChange();
}

/* Two different things, fetched separately on purpose.

   The *catalogue* is every machine there is, with its category and its weight, and it
   is what all the tick rules are decided on. It arrives in one request -- about 12,000
   entries of three short fields -- because the rules genuinely need all of it: only
   the largest 500 rows of a category are ever drawn, and a rule judged on those puts
   the other 411 of Video Slot back without saying so.

   *Rows* are what gets drawn: descriptions, badges, sizes, artwork. Those stay capped
   and are fetched per category, because most categories are never opened. */
let catalogue = null;
let catalogueFor = null;

/* Every request from this page asks for the excluded machines too. The tree lists
   every category so that one can be put back, and "Board Game / Cards: 1" with
   nothing inside it when opened was the alternative. */
function slim(query) {
  return api('/api/selection?' + new URLSearchParams({ ...(query || {}), excluded: '1' }).toString());
}

/* Fetched once per plan. Re-fetching it on every toggle would be absurd, and holding
   a stale one across a re-plan would decide the rules on machines that no longer
   exist. */
function loadCatalogue(signature) {
  if (catalogue && catalogueFor === signature) return catalogue;
  if (catalogueFor !== signature) S.machines = {};      // drawn rows are stale too
  catalogueFor = signature;
  catalogue = slim().then((answer) => {
    const byCategory = new Map();
    for (const m of answer.machines) {
      remember(m);
      if (!byCategory.has(m.category)) byCategory.set(m.category, []);
      byCategory.get(m.category).push(m.name);
    }
    S.members = Object.fromEntries(byCategory);
    // A category the answer never mentions holds nothing the selection wants.
    for (const cat of S.cats) if (!S.members[cat.name]) S.members[cat.name] = [];
    return S.members;
  }).catch((error) => {
    catalogue = null;                       // so the next attempt tries again
    throw error;
  });
  return catalogue;
}

function ready() {
  return catalogue || loadCatalogue(catalogueFor);
}

/* Opening the page. The catalogue is what makes every figure and every tick box on it
   correct, so it is fetched on arrival rather than on the first click -- but not
   before, because someone who never opens this page should not pay for it. */
export function enter(plan) {
  if (!plan) return;
  const signature = `${plan.wanted}:${plan.machines}:${plan.bytes}`;
  if (catalogue && catalogueFor === signature) return;
  loadCatalogue(signature).then(() => { render(); onChange(); })
    .catch((error) => banner(`Could not read the selection: ${error.message}`, 'bad'));
}

async function loadCategory(name) {
  if (S.machines[name] || S.loading.has(name)) return;
  S.loading.add(name);
  render();
  try {
    const [found] = await Promise.all([
      fetchMachines({ category: name, limit: ROWS, ...gameOrder(S.sort), excluded: '1' }),
      ready(),
    ]);
    S.machines[name] = found.rows;
    found.rows.forEach(remember);
  } catch (error) {
    // Kept as empty, so the redraw does not ask again as fast as the server fails.
    S.machines[name] = [];
    banner(`Could not read ${name}: ${error.message}`, 'bad');
  } finally {
    S.loading.delete(name);
    render();
  }
}

/* Every toggle goes through one of these, so no click can reach a rule that is
   missing the membership it decides on. */
async function tickGroup(group) {
  if (groupNeeds(group).length) await ready();
  toggleGroup(group);
}

async function tickSide(mature) {
  await ready();
  toggleSide(mature);
}

async function tickCat(cat) {
  if (catNeeds(cat).length) await ready();
  toggleCat(cat);
}

async function tickMachine(m) {
  if (needsMembers(m).length) await ready();
  toggleMachine(m);
}

export function filtering() {
  return Object.values(S.filters).some(Boolean);
}

function params() {
  const f = S.filters;
  return { q: f.query, genre: f.genre, condition: f.condition,
           mature: f.mature, have: f.have };
}

/* Typing narrows the filter while the previous answer is still in the air, and the
   slower one must not land last. */
let generation = 0;

export async function setFilter(key, value) {
  S.filters[key] = (value || '').trim();
  const mine = (generation += 1);
  if (!filtering()) { S.hits = null; S.hitTotal = 0; S.busy = false; render(); return; }
  S.busy = true;
  render();
  try {
    const found = await fetchMachines({ ...params(), limit: ROWS, ...gameOrder(S.sort), excluded: '1' });
    if (mine !== generation) return;
    S.hits = found.rows;
    S.hitTotal = found.total;
    found.rows.forEach(remember);
  } finally {
    if (mine === generation) S.busy = false;
  }
  if (mine === generation) render();
}

export const search = (needle) => setFilter('query', needle);

function clearFilters() {
  generation += 1;
  for (const key of Object.keys(S.filters)) S.filters[key] = '';
  S.hits = null;
  S.hitTotal = 0;
  S.busy = false;
  // The controls, too. Clearing the state and leaving the bar showing "Imperfect"
  // over an unfiltered tree is worse than not offering the button.
  for (const id of ['treeSearch', 'selGenre', 'selHave', 'selCondition', 'selAdult']) {
    const node = $(id);
    if (node) node.value = '';
  }
  render();
}

/* Acting on the match, not on the page of it. 500 rows are drawn; the filter may
   cover 1,500 games, and "untick all of these" has to mean all of them. */
async function everyMatch() {
  const answer = await api('/api/selection?' + new URLSearchParams(params()).toString());
  answer.machines.forEach(remember);
  return answer.machines;
}

/* Leaving a filtered view out writes one name per game into the settings -- 3,229 of
   them for the Imperfect filter -- and the list that comes out carries no memory of
   the rule that made it. Worth saying out loud before it happens rather than after. */
const LOUD = 50;

async function applyToAll(wanted, button) {
  const label = button.dataset.label || button.textContent;
  button.dataset.label = label;
  const confirmed = button.dataset.confirm === 'yes';
  button.disabled = true;
  button.textContent = 'Working…';
  let asking = false;
  try {
    const machines = await everyMatch();
    if (!wanted && machines.length >= LOUD && !confirmed) {
      asking = true;
      button.textContent = `Leave ${plural(machines.length, 'game', 'games')} out — confirm`;
      return;
    }
    await ready();
    // setAll redraws, which replaces this button; the label only has to survive the
    // confirmation step above.
    setAll(machines, wanted);
  } finally {
    button.dataset.confirm = asking ? 'yes' : '';
    if (button.textContent === 'Working…') button.textContent = label;
    button.disabled = false;
  }
}

/* The tree, shaped like the library on disk: the genres, then one branch named after
   the adult folder holding the same genres and categories underneath it. That is
   where those games are actually filed, and showing them mixed into the ordinary
   genres meant the page and the folder tree disagreed. */

function categoryRows(cat, depth) {
  const open = S.openCats.has(cat.name);
  const swap = () => {
    if (open) S.openCats.delete(cat.name);
    else { S.openCats.add(cat.name); loadCategory(cat.name); }
    render();
  };
  const rows = [branchRow({
    depth, label: cat.label || cat.name, state: catState(cat),
    onTick: () => tickCat(cat), open, onOpen: swap, cats: [cat], id: `cat:${cat.name}`,
  })];
  if (!open) return rows;
  if (S.loading.has(cat.name)) {
    rows.push(el('div', { class: 'row game dim', text: 'loading…' }));
    return rows;
  }
  if (!S.machines[cat.name]) loadCategory(cat.name);
  const games = S.machines[cat.name] || [];
  games.forEach((m) => rows.push(gameRow(m, false, depth + 1)));
  // The tick box still covers all of them -- membership is loaded in full -- but the
  // list is capped, and silently showing 500 of 911 is a lie.
  if (games.length < wantedOf(cat)) {
    rows.push(el('div', {
      class: 'row game dim',
      style: `padding-left:${INDENT[Math.min(depth + 1, 3)]}px`,
      text: `showing the largest ${count(games.length)} of ${count(wantedOf(cat))} `
        + '— use the filters above to reach the rest',
    }));
  }
  return rows;
}

function groupRows(group, depth, label) {
  const cats = orderCats(catsOf(group), S.sort);
  if (!cats.length) return [];
  const key = `${group.mature ? '18+' : ''}${group.genre}`;
  const open = S.open.has(key);
  const swap = () => { open ? S.open.delete(key) : S.open.add(key); render(); };
  const rows = [branchRow({
    depth, label: label || group.genre, state: groupState(group),
    onTick: () => tickGroup(group), open, onOpen: swap, cats, id: `genre:${key}`,
  })];
  if (open) cats.forEach((cat) => rows.push(...categoryRows(cat, depth + 1)));
  return rows;
}

function treeNodes() {
  const nodes = [];
  const names = S.genres.map((genre) => genre.name);
  for (const name of orderGenres(names, S.cats.filter((cat) => !cat.mature), S.sort)) {
    const rows = groupRows({ genre: name, mature: false }, 0);
    if (rows.length) nodes.push(rows);
  }

  const adult = S.cats.filter((cat) => cat.mature);
  if (!adult.length) return nodes;

  const open = S.open.has(' adult');
  const swap = () => {
    open ? S.open.delete(' adult') : S.open.add(' adult');
    render();
  };
  const rows = [branchRow({
    depth: 0, label: S.adultFolder, state: sideState(true),
    onTick: () => tickSide(true), open, onOpen: swap, cats: adult, id: 'adult',
  })];
  if (open) {
    orderGenres(names, adult, S.sort)
      .filter((name) => adult.some((cat) => cat.genre === name))
      .forEach((name) => rows.push(...groupRows({ genre: name, mature: true }, 1)));
  }
  nodes.push(rows);
  return nodes;
}

function gameRow(m, flat = false, depth = 2) {
  const out = machineOut(m);
  // Two targets, not one. The tick box decides whether the game is in the library;
  // the name opens it, the way it does everywhere else. A row that did both from one
  // click meant you could not look at a game without also changing your mind about it.
  return el('div', {
    class: `row game ${flat ? 'flat' : ''} ${out ? 'out' : ''}`,
    style: flat ? null : `padding-left:${INDENT[Math.min(depth, 3)]}px`,
  },
    el('span', { class: 'twist' }),
    tickBox(out ? 'out' : 'in', `${m.description}: ${out ? 'left out' : 'in the library'}`,
      () => tickMachine(m), `tick:game:${m.name}`),
    el('span', { class: 'name link', title: 'Details', role: 'link', tabindex: '0',
                 'aria-label': `Details of ${m.description}`,
                 onclick: () => openGame(m.name),
                 onkeydown: (event) => {
                   if (event.key === 'Enter') { event.preventDefault(); openGame(m.name); }
                 } },
      el('span', { text: m.description }),
      el('small', { text: m.name })),
    el('span', { class: 'rowflex' }, badges(m)),
    el('span', { class: 'size', text: m.bytes_human || '—' }));
}

/* What a filtered view can do that a tree cannot: act on the whole match at once. */
function bulkBar() {
  const shown = (S.hits || []).length;
  const untick = el('button', { class: 'btn sm', text: 'Leave all out' });
  const tick = el('button', { class: 'btn sm', text: 'Put all back' });
  untick.onclick = () => applyToAll(false, untick);
  tick.onclick = () => applyToAll(true, tick);
  return el('div', { class: 'rowflex bulkbar' },
    el('b', { text: `${count(S.hitTotal)} ${S.hitTotal === 1 ? 'game' : 'games'} match` }),
    shown < S.hitTotal
      ? el('span', { class: 'muted', text: `showing the largest ${count(shown)}` })
      : null,
    el('span', { style: 'flex:1' }),
    untick, tick,
    el('button', { class: 'btn sm ghost', text: 'Clear filters', onclick: clearFilters }));
}

/* "Nothing matches those filters" is true and useless when one of them is the
   culprit and the others are innocent -- 14 Handheld games and none downloaded reads
   as a broken filter unless the page says which one emptied it. Each is droppable on
   its own, so the answer is one click away rather than a reset. */
const FILTER_LABELS = {
  query: 'search', genre: 'genre', condition: 'condition',
  mature: 'adult', have: 'downloaded',
};

const CONTROL_FOR = {
  query: 'treeSearch', genre: 'selGenre', condition: 'selCondition',
  mature: 'selAdult', have: 'selHave',
};

function describeFilter(key, value) {
  if (key === 'have') return value === 'yes' ? 'on disk' : 'not downloaded';
  if (key === 'mature') return value === 'only' ? 'adult only' : 'adult hidden';
  if (key === 'condition') return value === 'good' ? 'fully emulated' : 'imperfect';
  if (key === 'query') return `“${value}”`;
  return value;
}

function nothingMatched() {
  const active = Object.entries(S.filters).filter(([, value]) => value);
  const chips = active.map(([key, value]) => {
    const drop = el('button', { class: 'btn sm', title: `Drop the ${FILTER_LABELS[key]} filter` },
      `${FILTER_LABELS[key]}: ${describeFilter(key, value)} ✕`);
    drop.onclick = () => {
      const control = $(CONTROL_FOR[key]);
      if (control) control.value = '';
      setFilter(key, '');
    };
    return drop;
  });
  return el('div', { class: 'empty' },
    el('div', { class: 'big' }, icon('selection')),
    el('div', { text: active.length > 1
      ? 'No game matches all of these at once.'
      : 'Nothing matches that.' }),
    el('div', { class: 'rowflex', style: 'justify-content:center;margin-top:12px' }, chips));
}

export function render() {
  const host = $('tree');
  if (!host) return;
  keepingFocus(host, () => draw(host));
}

function draw(host) {
  clear(host);

  if (S.busy && !S.hits) {
    host.append(el('div', { class: 'muted', style: 'padding:10px', text: 'Filtering…' }));
    return;
  }

  if (filtering()) {
    append(host, bulkBar());
    (S.hits || []).forEach((m) => host.append(gameRow(m, true)));
    if (!(S.hits || []).length) {
      host.append(nothingMatched());
    }
    return;
  }

  for (const node of treeNodes()) host.append(...node);

  if (!S.genres.length) {
    host.append(el('div', { class: 'empty' },
      el('div', { class: 'big' }, icon('selection')),
      el('div', { text: 'Build a plan first and the genres will appear here.' })));
  }
}

/* What the selection weighs, when the plan is out of date and cannot say. */
/* What the current ticks add up to.

   Counted from the server's own per-category totals, minus the games individually
   left out -- not by adding up the rows that happen to be loaded. Only 500 rows of a
   965-game category are ever fetched, so counting those was short by 465 games every
   time one was expanded. */

export function estimate() {
  let games = 0, bytes = 0;
  const totals = new Map(S.cats.map((cat) => [cat.name, cat]));
  for (const cat of S.cats) {
    if (catOut(cat)) continue;
    games += wantedOf(cat);
    bytes += weightOf(cat);
  }
  for (const name of S.outRoms) {
    const cat = totals.get(S.romCat[name]);
    if (!cat || catOut(cat)) continue;       // already not counted
    games -= 1;
    bytes -= S.romBytes[name] || 0;
  }
  return { games: Math.max(games, 0), bytes: Math.max(bytes, 0), ...leaving() };
}

/* What unticking has taken out of the library itself: games that are on the console
   now and would be deleted -- only if asked, on the Transfer page -- once the plan is
   rebuilt. Known once the catalogue has been read; until then nothing is claimed. */
function leaving() {
  let removed = 0, removedBytes = 0;
  const totals = new Map(S.cats.map((cat) => [cat.name, cat]));
  for (const [name, inLibrary] of Object.entries(S.romLib)) {
    if (!inLibrary) continue;
    const cat = totals.get(S.romCat[name]);
    const out = S.outRoms.has(name) || (cat && catOut(cat));
    if (!out) continue;
    removed += 1;
    removedBytes += S.romBytes[name] || 0;
  }
  return { removed, removedBytes, known: Object.keys(S.romLib).length > 0 };
}

export function adopt(plan, config) {
  S.genres = plan.genres || [];
  S.cats = plan.categories || [];
  if (config && config.mature_rom_folder) S.adultFolder = config.mature_rom_folder;
}

export function lists() {
  return {
    blacklist_genres: [...S.outGenres],
    blacklist_categories: [...S.outCats],
    blacklist_roms: [...S.outRoms],
  };
}

let seeded = null;

export function seed({ genres, categories, roms }) {
  S.outGenres = new Set(genres || []);
  S.outCats = new Set(categories || []);
  S.outRoms = new Set(roms || []);
  seeded = JSON.stringify(lists());
}

/* Whether the selection on screen differs from what it was last seeded with -- that
   is, whether there is something unsaved that a re-seed from the file would lose. */
export function dirty() {
  return seeded !== null && JSON.stringify(lists()) !== seeded;
}

/* Whether the saved lists are exactly what is on screen: a save has caught up with
   it. Nothing is lost by seeding from them then, and not doing so left the page
   "dirty" for ever after the first save -- so it stopped following the file, and a
   "Put back" on Left out was undone by the next Build plan. */
export function matches({ genres, categories, roms }) {
  const sorted = (list) => JSON.stringify([...(list || [])].sort());
  const now = lists();
  return sorted(now.blacklist_genres) === sorted(genres)
    && sorted(now.blacklist_categories) === sorted(categories)
    && sorted(now.blacklist_roms) === sorted(roms);
}


/* The filter bar. Same questions the library asks, because the answer to "which of
   these do I actually want" is the same question in both places -- it is only the
   verb that differs. */
export function toolbar() {
  const pick = (key, options) => {
    const node = el('select', {
      onchange: (event) => setFilter(key, event.target.value),
    }, options.map(([value, label]) => el('option', {
      value, text: label, selected: S.filters[key] === value,
    })));
    return node;
  };

  // Every level of the tree, and the games when a category opens: asked again in
  // the new order, since a capped list sorted here would only reorder its first 500.
  const sorter = sortControl('selection', TREE_SORTS, (how) => {
    S.sort = how;
    S.machines = {};
    if (filtering()) setFilter('query', S.filters.query);
    render();
  }, ['size', 'desc']);
  S.sort = sorter.get();
  return {
    sort: sorter.node,
    search: el('input', {
      type: 'text', placeholder: 'Find a game…', value: S.filters.query,
    }),
    genres: pick('genre', [['', 'All']]),
    condition: pick('condition', [
      ['', 'Any'],
      ['good', 'Fully emulated'],
      ['flawed', 'Imperfect'],
    ]),
    have: pick('have', [
      ['', 'All'],
      ['no', 'Not downloaded'],
      ['yes', 'On disk'],
    ]),
    adult: pick('mature', [
      ['', 'Shown'],
      ['hide', 'Hidden'],
      ['only', 'Only'],
    ]),
  };
}

export function fillGenres(select, plan) {
  if (!select || !plan) return;
  const labels = (plan.genres || []).map(
    (genre) => `${genre.name} (${count(genre.wanted ?? genre.machines)})`);
  // Rebuilt only when it would actually differ. The page polls every few seconds, and
  // replacing the options under an open menu closes it in the reader's face.
  if (select.dataset.signature === labels.join('|')) return;
  select.dataset.signature = labels.join('|');
  const current = S.filters.genre;
  clear(select);
  select.append(el('option', { value: '', text: 'All' }));
  (plan.genres || []).forEach((genre, index) => select.append(el('option', {
    value: genre.name, selected: genre.name === current, text: labels[index],
  })));
}
