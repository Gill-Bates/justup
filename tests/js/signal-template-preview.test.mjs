//
// tests/js/signal-template-preview.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";
function makeEl() {
  return {
    style: {}, dataset: {}, classList: { add() {}, remove() {}, contains: () => false },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, setAttribute() {},
  };
}
globalThis.document = {
  getElementById: () => makeEl(), createElement: () => makeEl(),
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
};
globalThis.window = globalThis;
globalThis.window.apiFetch = async () => ({ ok: true });
const { renderTemplatePreview, sampleContext } = await import("../../app/static/js/settings.signal-actions.js");

// Same shape as DEFAULT_SIGNAL_TEMPLATE_DOWN in app/services/notifications/signal_template.py.
const DEFAULT_DOWN = "{{status.emoji}} [ {{target.name}} ]  {{status.text}}\nStarted:   {{alert.started_at}}\n{{#if cta}}Link:         {{system.url}}\n{{/if cta}}";

test("preview resolves conditional blocks of the default template", () => {
  const out = renderTemplatePreview(DEFAULT_DOWN, sampleContext("down"));
  assert.ok(!out.includes("{{"), out);
  assert.match(out, /Link:\s+https:\/\/justup\.local\/ui/);
});

test("preview drops blocks of unknown or false flags like the server", () => {
  const ctx = sampleContext("down");
  assert.equal(renderTemplatePreview("a{{#if resolved}}X{{/if resolved}}b", ctx), "ab");
  assert.equal(renderTemplatePreview("a{{#if nope}}X{{/if nope}}b", ctx), "ab");
  assert.equal(renderTemplatePreview("a{{#if resolved}}X{{/if resolved}}b", sampleContext("up")), "aXb");
});

test("preview fills the status, badge and logo placeholders the server renders", () => {
  const out = renderTemplatePreview(
    "{{status.label}}|{{status.accent_color}}|{{status.foreground_color}}|{{status.text_color}}|{{target.check_badges}}|{{logo_url}}",
    sampleContext("down"),
  );
  assert.ok(out.split("|").every(part => part.length > 0), out);
});
