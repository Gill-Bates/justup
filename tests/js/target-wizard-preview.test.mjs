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
const portScanSource = readFileSync(new URL('../../app/static/js/utils/port-scan-core.js', import.meta.url), 'utf8');
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
function setup(profileValue = 'website') {
  const ids = {};
  const get = (id) => (ids[id] ||= el());
  const sections = [0, 1, 2, 3, 4].map(() => el({ querySelector: () => el() }));
  const form = el({
    querySelectorAll: (sel) => (sel === '.target-wizard-step' ? sections : []),
    querySelector: () => ({ value: profileValue, focus() {} }),
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
  vm.runInNewContext(portScanSource, sandbox.window);
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
test('the checks step mirrors the address-step ports and blocks a TCP check without one', async () => {
  const t = setup();
  const next = t.get('wizard-next');
  await next.fire('click');
  t.get('wizard-ports').value = '25,587';
  await t.get('wizard-ports').fire('input');
  assert.equal(t.get('wizard-tcp-summary-value').textContent, '25, 587');
  t.get('wizard-ports').value = '';
  await t.get('wizard-ports').fire('input');
  assert.equal(t.get('wizard-tcp-summary-value').textContent, 'None chosen yet');
  // Step 2 -> 3 with the TCP switch on and no port stays on the checks step.
  const second = next.fire('click');
  await tick();
  t.answer(0, {});
  await second;
  assert.equal(visible(t.sections), 2);
  t.get('wizard-tcp').checked = true;
  t.get('wizard-interval').checkValidity = () => true;
  await next.fire('click');
  assert.equal(visible(t.sections), 2);
  assert.equal(t.get('wizard-tcp-summary-error').textContent, 'Choose at least one TCP port.');
  assert.equal(t.pending.length, 1, 'no preview request was sent');
});

async function tcpProfileValidation(ports, udpPorts) {
  const t = setup('tcp');
  await t.get('wizard-next').fire('click'); // step 0 -> 1
  t.get('wizard-ports').value = ports;
  t.get('wizard-udp-ports').value = udpPorts;
  const second = t.get('wizard-next').fire('click');
  await tick();
  return { requested: t.pending.length, message: t.get('wizard-error').textContent, second };
}
test('the TCP profile accepts UDP-only ports on the address step', async () => {
  assert.equal((await tcpProfileValidation('', '53')).requested, 1);
});
test('the TCP profile accepts TCP-only ports on the address step', async () => {
  assert.equal((await tcpProfileValidation('443', '')).requested, 1);
});
test('the TCP profile rejects an address step with neither TCP nor UDP ports', async () => {
  const r = await tcpProfileValidation('', '');
  assert.equal(r.requested, 0);
  assert.equal(r.message, 'Choose at least one TCP or UDP port.');
});
test('the TCP profile rejects an invalid UDP port even without TCP ports', async () => {
  const r = await tcpProfileValidation('', '70000');
  assert.equal(r.requested, 0);
  assert.match(r.message, /UDP port numbers/);
});
