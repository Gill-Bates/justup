//
// tests/layout/combined-note.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Chart area of a target without an HTTP check: no "HTTP check is not enabled" note anywhere and no
// leftover note container taking space (DESIGN.md, "Charts and data visualization").
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

for (const viewport of [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }]) {
	test(`a TCP-only target shows no HTTP status note at ${viewport.name} @combined-note`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
		const page = await context.newPage();
		try {
			await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
			const unique = `tcp-only-${viewport.name}-${test.info().project.name}`;
			const id = await page.evaluate(async (name) => {
				const response = await window.apiFetch("/targets", {
					method: "POST", headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ name, host: `${name}.example.org`, enable_tcp_connect: true, tcp_host: `${name}.example.org`, tcp_ports: "25" }),
				});
				return response.status === 201 ? (await response.json()).id : `HTTP ${response.status}`;
			}, unique);
			assert.equal(typeof id, "number", `target not created: ${id}`);
			await page.goto(`${app.base}/ui/targets/${id}`, { waitUntil: "load" });
			await page.waitForSelector("#combined-status-note", { state: "attached", timeout: 20000 });
			await waitForChartReady(page, "chart-combined", { allowNoData: true, timeout: 20000 });
			await waitForVisualStability(page);
			const result = await page.evaluate(() => {
				const note = document.getElementById("combined-status-note");
				const area = document.querySelector("#combined-status-note")?.closest(".target-combined, .combined-footer, section, div") || document.body;
				return {
					noteText: note.textContent.trim(),
					noteBox: note.getBoundingClientRect().height,
					anywhere: /HTTP check is not enabled/i.test(document.body.textContent) || /HTTP check is not enabled/i.test(area.textContent),
				};
			});
			assert.equal(result.anywhere, false, "the 'HTTP check is not enabled' note is still in the page");
			assert.equal(result.noteText, "");
			assert.equal(result.noteBox, 0, "an empty note container still takes space");
		} finally {
			await context.close();
		}
	});
}
