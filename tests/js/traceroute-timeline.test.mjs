//
// tests/js/traceroute-timeline.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../app/static/js/traceroute-map.js', import.meta.url), 'utf8');
function el(extra = {}) {
  const handlers = {};
  const attrs = {};
  const node = {
    dataset: {}, attrs, handlers, hidden: false, textContent: '', title: '',
    classes: new Set(),
    classList: {
      toggle(name, on) { if (on) node.classes.add(name); else node.classes.delete(name); },
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    removeAttribute(k) { delete attrs[k]; },
    hasAttribute(k) { return k in attrs; },
    addEventListener(type, fn) { handlers[type] = fn; },
    removeEventListener(type) { delete handlers[type]; },
    contains() { return true; },
    closest() { return node; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    scrollIntoView() {},
    ...extra,
  };
  return node;
}
function setup() {
  const events = [];
  const rows = [
    el({ dataset: { hopFirst: '1', hopLast: '1' } }),
    el({ dataset: { hopFirst: '2', hopLast: '4' } }),
    el({ dataset: { hopFirst: '5', hopLast: '5' } }),
  ];
  const hop = el({ dataset: { hopFirst: '5', hopLast: '5', detail: 'Hop 5 · 10.0.0.5' } });
  const group = el({ dataset: { hopFirst: '2', hopLast: '4', detail: 'Hops 2–4' } });
  hop.setAttribute('data-tr-hop', '');
  group.setAttribute('data-tr-group', '');
  group.setAttribute('aria-expanded', 'false');
  group.setAttribute('aria-controls', 'tr-group-2-4');
  const members = el();
  members.hidden = true;
  const li = el();
  const track = el({ scrollWidth: 900, clientWidth: 300, scrollLeft: 0 });
  const detail = el({ textContent: 'Select a hop to see its details.' });
  const diagnostics = el({ querySelectorAll: () => rows });
  const section = el({
    dispatchEvent(evt) { events.push(evt.detail); },
    closest: (sel) => (sel === '.traceroute-diagnostics' ? diagnostics : section),
    querySelector: (sel) => (sel === '[data-tr-track]' ? track : sel === '[data-tr-detail]' ? detail : null),
    querySelectorAll: () => [hop, group],
  });
  hop.closest = (sel) => (sel.includes('data-tr-hop') ? hop : li);
  group.closest = (sel) => (sel.includes('data-tr-hop') ? group : li);
  const sandbox = {
    window: { addEventListener() {}, removeEventListener() {} },
    document: {
      readyState: 'loading', body: { addEventListener() {} }, addEventListener() {},
      createElement: () => el(), getElementById: (id) => (id === 'tr-group-2-4' ? members : null),
    },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    Intl, console, setTimeout, clearTimeout,
  };
  vm.runInNewContext(source, sandbox);
  const root = el({ querySelectorAll: (sel) => (sel.includes('route-overview') ? [section] : []), matches: () => false });
  sandbox.window.TracerouteMap.initAllTimelines(root);
  const click = (btn) => section.handlers.click({ target: btn });
  return { section, hop, group, members, li, track, detail, rows, events, click };
}
test('selecting a hop marks it pressed, highlights overlapping rows and notifies the map', () => {
  const t = setup();
  t.click(t.hop);
  assert.equal(t.hop.attrs['aria-pressed'], 'true');
  assert.deepEqual(t.rows.map((r) => r.classes.has('is-linked')), [false, false, true]);
  assert.equal(t.detail.textContent, 'Hop 5 · 10.0.0.5');
  assert.deepEqual(JSON.parse(JSON.stringify(t.events.at(-1))), { on: true, first: 5, last: 5 });
  t.click(t.hop);
  assert.equal(t.hop.attrs['aria-pressed'], 'false');
  assert.deepEqual(JSON.parse(JSON.stringify(t.events.at(-1))), { on: false });
  assert.ok(t.rows.every((r) => !r.classes.has('is-linked')));
});
test('a group toggles aria-expanded and its member list, and highlights its whole range', () => {
  const t = setup();
  t.click(t.group);
  assert.equal(t.group.attrs['aria-expanded'], 'true');
  assert.equal(t.members.hidden, false);
  assert.ok(t.li.classes.has('is-open'));
  assert.ok(t.rows[1].classes.has('is-linked') && !t.rows[0].classes.has('is-linked'));
  t.click(t.group);
  assert.equal(t.group.attrs['aria-expanded'], 'false');
  assert.equal(t.members.hidden, true);
});
test('the scroll track is a tab stop only while it actually scrolls, and arrow keys scroll it', () => {
  const t = setup();
  assert.equal(t.track.attrs.tabindex, '0');
  let prevented = false;
  t.track.handlers.keydown({ target: t.track, key: 'ArrowRight', preventDefault() { prevented = true; } });
  assert.equal(t.track.scrollLeft, 120);
  assert.ok(prevented);
});
