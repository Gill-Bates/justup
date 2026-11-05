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

// Fixed scale factor of the pixel probe: a quarter-pixel grid makes the samples independent of the host display.
const DSF = 4;
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
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: DSF, reducedMotion: "reduce", storageState: adminState });
			const page = await context.newPage();
			await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => {
				const style = document.createElement("style");
				style.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
				document.head.append(style);
			}));
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
				// A header color unlike the page and the border makes any square paint over the arc unmistakable.
				await card.evaluate((el) => el.querySelector(".card-header").style.setProperty("background-color", "rgb(255, 0, 255)", "important"));
				const box = await card.boundingBox();
				const info = await card.evaluate((el) => {
					const header = el.querySelector(".card-header");
					return { header: getComputedStyle(header).backgroundColor, radius: parseFloat(getComputedStyle(el).borderTopLeftRadius), bodyHidden: getComputedStyle(el.querySelector(".check-body")).display === "none" };
				});
				assert.equal(info.bodyHidden, true, "the check body is collapsed");
				// Deterministic sampling: a fixed device scale factor, the corner snapped to the device pixel grid and
				// a clip on whole CSS pixels, so every sample sits at the same place in every run and engine.
				// Fractional layout positions are snapped differently per engine: put the card on whole CSS pixels first.
				await card.evaluate((el) => {
					el.style.height = `${Math.round(el.getBoundingClientRect().height)}px`;
					for (let i = 0; i < 4; i++) {
						const frac = el.getBoundingClientRect().top % 1;
						if (Math.abs(frac) < 0.01 || Math.abs(frac - 1) < 0.01) break;
						el.style.position = "relative";
						el.style.top = `${(parseFloat(el.style.top) || 0) + (1 - frac)}px`;
					}
				});
				const aligned = await card.evaluate((el) => { const r = el.getBoundingClientRect(); return [r.top % 1, r.bottom % 1, r.left % 1].every((f) => f < 0.01 || f > 0.99); });
				assert.ok(aligned, `the card sits on whole CSS pixels, so the corner pixels are deterministic: ${JSON.stringify(await card.evaluate((el) => el.getBoundingClientRect().toJSON()))}`);
				const corner = await card.evaluate((el) => {
					const dsf = devicePixelRatio, rect = el.getBoundingClientRect();
					return { dsf, left: Math.round(rect.left), bottom: Math.round(rect.bottom), radius: parseFloat(getComputedStyle(el).borderBottomLeftRadius), border: parseFloat(getComputedStyle(el).borderBottomWidth) };
				});
				assert.equal(corner.dsf, DSF, "the device scale factor is fixed");
				assert.ok(corner.radius > 0, "the card has a rounded frame");
				const size = 16;
				const clip = { x: Math.floor(corner.left) - 2, y: Math.ceil(corner.bottom) - (size - 2), width: size, height: size };
				const png = (await page.screenshot({ clip, animations: "disabled", caret: "hide", scale: "device" })).toString("base64");
				const samples = await page.evaluate(async ({ png, size, clip, corner }) => {
					const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${png}`)).blob());
					const canvas = new OffscreenCanvas(image.width, image.height);
					const context2d = canvas.getContext("2d", { willReadFrequently: true });
					context2d.drawImage(image, 0, 0);
					const scale = image.width / size;
					const at = (dx, dy) => Array.from(context2d.getImageData(Math.floor((corner.left - clip.x + dx) * scale), Math.floor((corner.bottom - clip.y - dy) * scale), 1, 1).data.slice(0, 3));
					// dx/dy: CSS px from the card's outer left/bottom edge. The arc centre sits (radius, radius) from the corner.
					const grid = [];
					for (let i = 0; i < 16; i++) grid.push(corner.border + 0.125 + i * 0.25);
					const outside = [], arc = [];
					for (const dx of grid) {
						for (const dy of grid) {
							const distance = Math.hypot(corner.radius - dx, corner.radius - dy);
							if (dx <= corner.radius && dy <= corner.radius && distance > corner.radius + 0.75) outside.push(at(dx, dy));
							if (Math.abs(dx - dy) < 0.01 && Math.abs(distance - (corner.radius - corner.border / 2)) < 0.3) arc.push(at(dx, dy));
						}
					}
					return { outside, arc, inside: at(corner.radius + 2, corner.radius + 2) };
				}, { png, size, clip, corner });
				const header = [255, 0, 255];
				const isHeader = (pixel) => pixel.every((value, i) => Math.abs(value - header[i]) < 40);
				assert.ok(isHeader(samples.inside), `the header is not painted ${header} inside the card (got ${samples.inside}): the probe is blind`);
				assert.ok(samples.outside.length >= 3, `only ${samples.outside.length} samples outside the rounded frame`);
				// The pixels beyond the arc show the page, never the header: a square header paints over them.
				assert.deepEqual(samples.outside.filter(isHeader), [], `a pixel outside the rounded frame has the header color ${header}: a square header paints over it`);
				// The border arc stays visible: some pixel on the diagonal differs from the header background.
				assert.ok(samples.arc.length > 0 && samples.arc.some((pixel) => !isHeader(pixel)), `no border pixel on the corner diagonal differs from the header background ${header}: the arc is overpainted (arc ${JSON.stringify(samples.arc)}, radius ${corner.radius})`);
			} finally {
				await context.close();
			}
		});
	}
}
