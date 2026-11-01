//
// tests/js/blob-download.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";
test("downloadBlob clicks a download link and revokes the URL only after a delay", async () => {
  const log = [];
  const timers = [];
  globalThis.URL = { createObjectURL: () => "blob:x", revokeObjectURL: url => log.push(["revoke", url]) };
  globalThis.setTimeout = (fn, ms) => { timers.push([fn, ms]); return 1; };
  globalThis.document = {
    body: { appendChild: a => log.push(["append", a.href]), removeChild: () => log.push(["remove"]) },
    createElement: () => ({ click() { log.push(["click", this.download]); } }),
  };
  const { downloadBlob } = await import("../../app/static/js/utils/blob-download.js");
  downloadBlob({}, "backup.tar.bz2");
  assert.deepEqual(log, [["append", "blob:x"], ["click", "backup.tar.bz2"], ["remove"]]);
  assert.equal(timers.length, 1);
  assert.ok(timers[0][1] >= 1000);
  timers[0][0]();
  assert.deepEqual(log.at(-1), ["revoke", "blob:x"]);
});
