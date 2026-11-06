//
// tests/js/signal-template-preview.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

test("preview knows exactly the placeholders the server renders", () => {
  const source = readFileSync(new URL("../../app/services/notifications/email_template.py", import.meta.url), "utf8");
  const block = source.match(/ALLOWED_PLACEHOLDERS = frozenset\(\s*\{(.*?)\}\s*\)/s)[1];
  const server = [...block.replace(/#.*$/gm, "").matchAll(/"([^"]+)"/g)].map(m => m[1]).sort();
  const leaves = (obj, prefix = "") => Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object" ? leaves(v, `${prefix}${k}.`) : [`${prefix}${k}`]);
  const { flags, ...placeholders } = sampleContext("down");
  assert.ok(flags);
  assert.deepEqual(leaves(placeholders).sort(), server);
});

test("preview renders flags and objects empty like the server", () => {
  const ctx = sampleContext("down");
  assert.equal(renderTemplatePreview("[{{flags.cta}}|{{flags}}|{{status}}]", ctx), "[||]");
});

test("preview mirrors the server's conditional block syntax and flag whitelist", () => {
  const server = readFileSync(new URL("../../app/services/notifications/email_template.py", import.meta.url), "utf8");
  const client = readFileSync(new URL("../../app/static/js/settings.signal-actions.js", import.meta.url), "utf8");
  const serverPattern = server.match(/_CONDITIONAL_RE = re\.compile\(\s*r"([^"]+)"/)[1];
  const clientPattern = client.match(/const CONDITIONAL_RE = \/(.+)\/gs;/)[1].replaceAll("\\/", "/");
  assert.equal(clientPattern, serverPattern);
  const block = server.match(/CONDITIONAL_FLAGS = frozenset\(\s*\{(.*?)\}\s*\)/s)[1];
  const serverFlags = [...block.replace(/#.*$/gm, "").matchAll(/"([^"]+)"/g)].map(m => m[1]).sort();
  assert.deepEqual(Object.keys(sampleContext("down").flags).sort(), serverFlags);
});
