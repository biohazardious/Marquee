/* The genre -> category -> game tree, shared by Selection and Left out.

   The two pages drew the same rows from two copies of the same code, and the copies
   drifted -- one learned to re-fetch an open category and the other did not. They
   also shared the same gaps: tick boxes that were a "button" reading ✓, rows a
   keyboard could not open, and focus thrown back to the top of the page by every
   redraw. Both are answered once, here. */
import { count, el, human } from './util.js';

export const INDENT = [10, 26, 42, 58];

export const wantedOf = (cat) => cat.wanted ?? cat.machines ?? 0;
export const weightOf = (cat) => cat.wanted_bytes ?? cat.bytes ?? 0;

export function totals(cats) {
  return cats.reduce((sum, cat) => ({
    wanted: sum.wanted + wantedOf(cat),
    bytes: sum.bytes + weightOf(cat),
  }), { wanted: 0, bytes: 0 });
}

/* Space or Enter does what a click does, as on a real control. */
function operable(node, activate) {
  node.setAttribute('tabindex', '0');
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(event);
    }
  });
  node.addEventListener('click', activate);
  return node;
}

/* A tick box a screen reader can read: checked, unchecked, or mixed for a genre with
   some of its categories out. `state` is 'in', 'out' or 'some'. */
export function tickBox(state, label, onTick, focusKey) {
  const node = el('span', {
    class: 'tick', role: 'checkbox', title: label,
    'aria-checked': state === 'some' ? 'mixed' : String(state !== 'out'),
    'aria-label': label,
    'data-focus': focusKey,
  }, el('span', {
    class: `box ${state}`, 'aria-hidden': 'true',
    text: state === 'out' ? '' : state === 'some' ? '–' : '✓',
  }));
  return operable(node, (event) => { event.stopPropagation(); onTick(); });
}

/* A genre or category row. With `state` and `onTick` it carries a tick box (the
   Selection page); without them it only opens and closes (Left out). */
export function branchRow({ depth, label, open, onOpen, cats, state, onTick, id }) {
  const sum = totals(cats);
  const kind = depth === 0 ? 'genre' : 'cat';
  const twist = operable(el('span', {
    class: 'twist', role: 'button', text: open ? '▾' : '▸',
    'aria-expanded': String(Boolean(open)),
    'aria-label': `${open ? 'Close' : 'Open'} ${label}`,
    'data-focus': `open:${id}`,
  }), (event) => { event.stopPropagation(); onOpen(); });
  // Without a tick box the whole row opens it, as it always has on Left out; with
  // one, only the arrow and the name do, so a stray click cannot change a selection.
  const name = el('span', { class: 'name', text: label, onclick: onTick ? onOpen : null });
  return el('div', {
    class: `row ${kind} ${state === 'out' ? 'out' : ''}`,
    style: `padding-left:${INDENT[depth]}px`,
    onclick: onTick ? null : onOpen,
  },
    twist,
    onTick ? tickBox(state, `${label}: ${state === 'out' ? 'left out' : 'in the library'}`,
      onTick, `tick:${id}`) : null,
    name,
    el('span', { class: 'count', text: count(sum.wanted) }),
    el('span', { class: 'size', text: human(sum.bytes) }));
}

/* Redrawing a tree replaces every node, so whatever had focus is gone and a keyboard
   user is sent back to the top of the page after every tick. Remember which control
   it was by its key, and give focus back to its replacement. */
export function keepingFocus(host, draw) {
  const active = document.activeElement;
  const key = active && host.contains(active) ? active.getAttribute('data-focus') : null;
  draw();
  if (!key) return;
  const again = [...host.querySelectorAll('[data-focus]')]
    .find((node) => node.getAttribute('data-focus') === key);
  if (again) again.focus();
}
