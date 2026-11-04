//
// tests/js/target-wizard-preview.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../app/static/js/target-wizard.js', import.meta.url), 'utf8');
const optionalGroupsSource = readFileSync(new URL('../../app/static/js/utils/optional-groups.js', import.meta.url), 'utf8');
const groupsSource = readFileSync(new URL('../../app/static/js/utils/check-groups.js', import.meta.url), 'utf8');
function el(extra = {}) {
  const listeners = {};
  return {
    value: '', checked: false, hidden: false, disabled: false, textContent: '', dataset: {}, type: 'text', tagName: 'INPUT',
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    fire(name) { return Promise.all((listeners[name] || []).map((fn) => fn({ preventDefault() {} }))); },
    replaceChildren() {}, append() {}, checkValidity: () => true, setAttribute() {}, removeAttribute() {}, focus() {}, scrollIntoView() {}, after() {},
    querySelector: () => null, querySelectorAll: () => [],
    ...extra,
  };
}
function setup() {
  const ids = {};
  const get = (id) => (ids[id] ||= el());
  const sections = [0, 1, 2, 3, 4].map(() => el({ querySelector: () => el() }));
  const form = el({
    querySelectorAll: (sel) => (sel === '.target-wizard-step' ? sections : []),
    querySelector: () => ({ value: 'website' }),
  });
  ids['target-wizard-form'] = form;
  ids['wizard-address'] = el({ value: 'example.org' });
  ids['wizard-ports'] = el();
  const pending = [];
  const sandbox = {
    window: {
      apiFetch: (url) => (url === '/recipients'
        ? Promise.resolve({ ok: true, status: 200, json: async () => [] })
        : new Promise((resolve) => pending.push(resolve))),
      location: { assign() {} },
    },
    document: {
      getElementById: get,
      querySelectorAll: () => [],
      createElement: () => el(),
    },
    console, URL, JSON, Number, Boolean, Array, Object, Set,
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(optionalGroupsSource, sandbox.window);
  vm.runInNewContext(groupsSource, sandbox.window);
  vm.runInNewContext(source, sandbox);
  const answer = (index, values) => pending[index]({
    ok: true, status: 200,
    json: async () => ({ values, conflicts: [], suggested_https: false }),
  });
  return { get, sections, pending, answer };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const visible = (sections) => sections.findIndex((section) => !section.hidden);
test('a preview answer is applied when the form did not change', async () => {
  const t = setup();
  const next = t.get('wizard-next');
  await t.get('wizard-address').fire('input');
  const first = next.fire('click'); // step 0 -> 1 (no request)
  await first;
  assert.equal(visible(t.sections), 1);
  const second = next.fire('click');
  await tick();
  assert.equal(t.pending.length, 1);
  t.answer(0, { http_url: 'https://example.org' });
  await second;
  assert.equal(visible(t.sections), 2);
});
test('a preview answer is discarded when the user went back meanwhile', async () => {
  const t = setup();
  const next = t.get('wizard-next');
  await next.fire('click');
  const second = next.fire('click');
  await tick();
  await t.get('wizard-back').fire('click');
  assert.equal(visible(t.sections), 0);
  t.answer(0, {});
  await second;
  assert.equal(visible(t.sections), 0);
});
test('a preview answer is discarded when the address changed meanwhile', async () => {
  const t = setup();
  const next = t.get('wizard-next');
  await next.fire('click');
  const second = next.fire('click');
  await tick();
  t.get('wizard-address').value = 'other.example';
  t.answer(0, {});
  await second;
  assert.equal(visible(t.sections), 1);
});
