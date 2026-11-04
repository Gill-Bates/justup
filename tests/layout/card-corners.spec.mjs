//
// tests/layout/card-corners.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Cards keep their rounded frame (DESIGN.md, "Cards and panels"): a header, body or table that touches a
// corner follows the card's inner radius (outer radius minus border width), so no square background
// paints over the curved border, with or without the check body collapsed. The shared card base
// never clips its content (dropdowns, tag suggestions, tooltips stay visible).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 900 }, { name: "390x844", width: 390, height: 844 }];
const PAGES = [
	{ id: "target-form", path: "/ui/targets/1/edit", prepare: async (page) => { await page.click("#target-http-tab").catch(() => {}); } },
	{ id: "targets", path: "/ui/targets" },
	{ id: "dashboard", path: "/" },
	{ id: "settings", path: "/ui/settings" },
	{ id: "notifications", path: "/ui/notifications" },
];
// Existing, documented clipping: `.ui-card` (page cards) and dashboard widget cards clip by design, so their
// content follows the rounding already. Every other card must stay unclipped.
const CLIPPING = ".ui-card, .dashboard-widget-card, .modal-content";

const GEOMETRY = (CLIPPING) => {
	const px = (value) => parseFloat(value) || 0;
	const corners = [["top", "left"], ["top", "right"], ["bottom", "left"], ["bottom", "right"]];
	const problems = [];
	let checked = 0;
	for (const card of document.querySelectorAll(".card, .x_panel")) {
		const rect = card.getBoundingClientRect();
		if (!rect.width || !rect.height) continue;
		const style = getComputedStyle(card);
		const outer = px(style.borderTopLeftRadius);
		const border = px(style.borderTopWidth);
		const inner = Math.max(0, outer - border);
		const label = `${card.tagName.toLowerCase()}.${[...card.classList].slice(0, 3).join(".")}`;
		if (/clip|hidden/.test(style.overflow) && !card.matches(CLIPPING)) problems.push(`${label}: the card clips its content (overflow ${style.overflow})`);
		for (const [v, h] of corners) {
			// The element that owns this corner: the first/last child whose box reaches it.
			const children = [...card.children].filter((c) => c.getBoundingClientRect().height > 0 && getComputedStyle(c).position !== "absolute");
			const child = v === "top" ? children[0] : children[children.length - 1];
			if (!child) continue;
			const cr = child.getBoundingClientRect();
			const reaches = (h === "left" ? cr.left - rect.left : rect.right - cr.right) <= border + 1 && (v === "top" ? cr.top - rect.top : rect.bottom - cr.bottom) <= border + 1;
			if (!reaches) continue;
			const cs = getComputedStyle(child);
			const paints = cs.backgroundColor !== "rgba(0, 0, 0, 0)" || cs.backgroundImage !== "none" || parseFloat(cs.borderBottomWidth) + parseFloat(cs.borderTopWidth) > 0;
			if (!paints || (child.matches(".ju-table-viewport") || /auto|scroll|hidden|clip/.test(cs.overflow))) continue;
			checked += 1;
			const radius = px(cs[`border${v[0].toUpperCase()}${v.slice(1)}${h[0].toUpperCase()}${h.slice(1)}Radius`]);
			if (radius + 0.5 < inner) problems.push(`${label} > ${child.tagName.toLowerCase()}.${[...child.classList].slice(0, 2).join(".")}: ${v}-${h} radius ${radius}px < inner ${inner}px`);
		}
	}
	return { problems, checked };
};

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`cards keep their rounded frame at ${viewport.name} ${theme} @card-corners`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			try {
				for (const entry of PAGES) {
					await page.goto(app.base + entry.path, { waitUntil: "load" });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await entry.prepare?.(page);
					await page.waitForTimeout(700);
					const { problems } = await page.evaluate(GEOMETRY, CLIPPING);
					assert.deepEqual(problems.filter((p) => !p.includes("ju-table-viewport")), [], `${entry.id}`);
				}

				// Collapsed check card: the header owns the bottom corners; its square background must not cover the border arc.
				await page.goto(app.base + "/ui/targets/1/edit", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.click("#target-http-tab").catch(() => {});
				await page.waitForTimeout(500);
				await page.evaluate(() => {
					const toggle = document.getElementById("enable_http_check");
					if (toggle.checked) toggle.click();
				});
				await page.waitForTimeout(400);
				const card = page.locator(".check-card:has(#enable_http_check)");
				await card.scrollIntoViewIfNeeded();
				const box = await card.boundingBox();
				const info = await card.evaluate((el) => {
					const header = el.querySelector(".card-header");
					return { header: getComputedStyle(header).backgroundColor, radius: parseFloat(getComputedStyle(el).borderTopLeftRadius), bodyHidden: getComputedStyle(el.querySelector(".check-body")).display === "none" };
				});
				assert.equal(info.bodyHidden, true, "the check body is collapsed");
				const size = 16;
				const clip = { x: box.x - 2, y: box.y + box.height - (size - 2), width: size, height: size };
								const png = (await page.screenshot({ clip })).toString("base64");
				const diagonal = await page.evaluate(async ({ png, size }) => {
					const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${png}`)).blob());
					const canvas = new OffscreenCanvas(image.width, image.height);
					const context2d = canvas.getContext("2d");
					context2d.drawImage(image, 0, 0);
					const scale = image.width / size;
					// Walk the bottom-left corner diagonally from outside to inside: the curved border has to show up
					// as a pixel unlike the header background (a square header would paint over it).
					const found = [];
					for (let i = 1; i <= 7; i++) {
						for (let dy = -1; dy <= 1; dy++) {
							const x = Math.round((2 + i) * scale), y = Math.round((size - 3 - i + dy) * scale);
							found.push(Array.from(context2d.getImageData(x, y, 1, 1).data.slice(0, 3)));
						}
					}
					return found;
				}, { png, size });
				const header = info.header.match(/\d+/g).map(Number);
				const arcVisible = diagonal.some((pixel) => pixel.some((value, i) => Math.abs(value - header[i]) >= 12));
				const close = !arcVisible;
				assert.equal(close, false, `no border pixel on the corner diagonal differs from the header background ${header}: the arc is overpainted`);
			} finally {
				await context.close();
			}
		});
	}
}
