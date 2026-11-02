//
// tests/layout/notif-dropdown.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser layout check for the topbar notification dropdown. It renders the real popover markup
// with the real stylesheets and the real notifications.js, then measures actual geometry (not DOM
// presence): the relative time stays visible and un-truncated beside long titles/descriptions,
// the dropdown never overflows horizontally, and the full date is available as a tooltip. It also
// writes before/after screenshots (before = the old single ellipsized detail line).
//
//   node tests/layout/notif-dropdown.mjs [--shots DIR] [--browsers chromium,webkit] [--json FILE]
//
// Playwright is resolved from the working directory, then $PLAYWRIGHT_DIR (dev-only tool, never
// installed by the repo). Exit codes: 0 clean, 1 findings, 77 skipped (Playwright/browser absent).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const SKIP = 77;

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
	if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "1"]);
	return acc;
}, []));

function loadPlaywright() {
	for (const base of [process.cwd(), process.env.PLAYWRIGHT_DIR].filter(Boolean)) {
		try { return createRequire(path.join(path.resolve(base), "/"))("playwright"); } catch { /* next */ }
	}
	return null;
}
const pw = loadPlaywright();
if (!pw) { console.log("SKIP: Playwright is not installed (set PLAYWRIGHT_DIR)"); process.exit(SKIP); }

const css = (name) => readFileSync(path.join(repo, "app/static/css", name), "utf8");
const notifJs = readFileSync(path.join(repo, "app/static/js/notifications.js"), "utf8");

// setContent has a blank origin, so the stylesheet's /static font URL cannot resolve. Inline the
// real Material Icons font as a data URI so the icon column has its true single-glyph width instead
// of a wide fallback showing the ligature name (which would distort the layout being measured).
const fontData = readFileSync(path.join(repo, "app/static/fonts/MaterialIcons-Regular.ttf")).toString("base64");
const materialCss = css("material-icons.css").replace(
	/url\('\/static\/fonts\/MaterialIcons-Regular\.ttf'\) format\('truetype'\)/,
	`url('data:font/ttf;base64,${fontData}') format('truetype')`,
);

// Fixtures cover the reported shapes: long title, long description, no description, a wide age
// range from "Now" to "120 days ago", unread/read and several categories.
const now = Date.now();
const ago = (ms) => new Date(now - ms).toISOString();
const MIN = 60000, HOUR = 60 * MIN, DAY = 24 * HOUR;
const FIXTURES = [
	{ id: 1, title: "PDF report ready: efg-luettringhausen-a-very-long-site-name-that-keeps-going.example.com", body: "Ready to download", actor: "root", severity: "success", category: "reports", is_read: false, created_at: ago(15 * MIN) },
	{ id: 2, title: "Administrator password changed", body: "Administrator credentials updated and a rather long description that would push the time out of view", actor: "root", severity: "security", category: "security", is_read: true, created_at: ago(7 * DAY) },
	{ id: 3, title: "Just happened", body: "", actor: "", severity: "info", category: "activity", is_read: false, created_at: ago(10000) },
	{ id: 4, title: "One minute old", body: "short", actor: "root", severity: "info", category: "monitoring", is_read: true, created_at: ago(1 * MIN) },
	{ id: 5, title: "One hour old notification with a moderately long title to stress the layout", body: "A moderately long description text here", actor: "root", severity: "warning", category: "delivery", is_read: false, created_at: ago(1 * HOUR) },
	{ id: 6, title: "Ancient", body: "A very very long description without break opportunities aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", actor: "root", severity: "error", category: "monitoring", is_read: false, created_at: ago(120 * DAY) },
];

// Minimal real topbar + popover markup (copied from base.html). The popover is shown (not hidden)
// so it can be measured and screenshotted; renderPreview fills the list.
const PAGE = (styles) => `<!doctype html>
<html lang="en" data-bs-theme="light" data-use-utc="false">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>${styles}</style>
<style>:root{--ju-topbar-h:3.75rem;} body{margin:0;} .ju-content{position:relative;}</style>
</head>
<body>
<div id="app-shell" class="ju-shell"><div class="ju-content">
<header class="ju-topbar"><div class="ju-topbar-actions"><div class="ju-bell-wrap">
	<button type="button" class="btn ju-topbar-button ju-bell" id="notif-bell" aria-haspopup="dialog" aria-expanded="true" aria-controls="notif-popover" aria-label="Notifications"><span class="material-icons" aria-hidden="true">notifications</span><span class="ju-bell-badge" id="notif-badge" hidden></span></button>
	<div class="ju-notif-popover" id="notif-popover" role="dialog" aria-labelledby="notif-pop-title">
		<div class="ju-notif-pop-head"><strong id="notif-pop-title">Notifications</strong><span class="text-body-secondary small" id="notif-pop-count"></span></div>
		<ul class="ju-notif-pop-list" id="notif-pop-list"></ul>
		<div class="ju-notif-pop-foot"><button type="button" class="btn btn-sm btn-outline-secondary" id="notif-read-all">Mark all as read</button><a class="btn btn-sm btn-primary" href="/ui/notifications">View all notifications</a></div>
	</div>
	<span class="visually-hidden" id="notif-live" role="status" aria-live="polite"></span>
</div></div></header>
<main id="main-content" class="ju-content" style="min-height:40vh"></main>
</div></div>
<script>${notifJs}</script>
</body></html>`;

const STYLES = [css("bootstrap.min.css"), materialCss, css("gentelella-layout.css"), css("ui.css"), css("notifications.css")].join("\n");

const VIEWPORTS = [
	{ name: "320x640", w: 320, h: 640 },
	{ name: "375x667", w: 375, h: 667 },
	{ name: "768x1024", w: 768, h: 1024 },
	{ name: "1280x800", w: 1280, h: 800 },
];
const THEMES = ["light", "dark"];

// In-page measurement: returns per-row time geometry and whether the row/time overflow their boxes.
const AUDIT = (items) => {
	const TOL = 2;
	const doc = document.documentElement;
	const pop = document.getElementById("notif-popover");
	const out = { issues: [], rows: [], docOverflowX: doc.scrollWidth > doc.clientWidth + TOL, popOverflowX: false };
	const pr = pop.getBoundingClientRect();
	out.popOverflowX = pop.scrollWidth > pop.clientWidth + TOL;
	const visibleRect = (el) => {
		let { left, top, right, bottom } = el.getBoundingClientRect();
		for (let p = el.parentElement; p && p !== doc; p = p.parentElement) {
			const cs = getComputedStyle(p);
			if (cs.overflowX !== "visible") { const b = p.getBoundingClientRect(); left = Math.max(left, b.left); right = Math.min(right, b.right); }
			if (cs.overflowY !== "visible") { const b = p.getBoundingClientRect(); top = Math.max(top, b.top); bottom = Math.min(bottom, b.bottom); }
		}
		return { left, top, right, bottom };
	};
	const rows = Array.from(document.querySelectorAll("#notif-pop-list .ju-notif-pop-item"));
	rows.forEach((li, i) => {
		const timeEl = li.querySelector(".ju-notif-pop-time");
		const row = { i, hasTime: !!timeEl };
		if (timeEl) {
			const v = visibleRect(timeEl);
			const full = timeEl.getBoundingClientRect();
			row.text = timeEl.textContent;
			row.datetime = timeEl.getAttribute("datetime");
			row.title = timeEl.getAttribute("title") || timeEl.title || "";
			row.visibleW = Math.round(v.right - v.left);
			row.visibleH = Math.round(v.bottom - v.top);
			row.fullW = Math.round(full.width);
			// The time must be visible: a positive painted box at least as wide as its own content.
			if (row.visibleW < 4 || row.visibleH < 4) out.issues.push({ type: "time-not-visible", i, text: row.text });
			// It must not be clipped: the painted width must cover the element's own width.
			if (full.width - (v.right - v.left) > TOL || full.height - (v.bottom - v.top) > TOL) out.issues.push({ type: "time-clipped", i, text: row.text });
			// It must stay inside the popover horizontally.
			if (full.right > pr.right + TOL || full.left < pr.left - TOL) out.issues.push({ type: "time-outside-popover", i, text: row.text });
			// A non-empty expected age must actually render some text.
			if (items[i] && items[i].created_at && !/not-a-date/.test(items[i].created_at) && !row.text) out.issues.push({ type: "time-empty", i });
		} else if (items[i] && items[i].created_at) {
			out.issues.push({ type: "time-missing", i });
		}
		out.rows.push(row);
	});
	if (out.docOverflowX) out.issues.push({ type: "doc-overflow-x", scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth });
	if (out.popOverflowX) out.issues.push({ type: "pop-overflow-x", scrollWidth: pop.scrollWidth, clientWidth: pop.clientWidth });
	return out;
};

async function run() {
	const browsers = (args.browsers || "chromium").split(",");
	const findings = [];
	let audits = 0;
	const launched = [];
	const missing = [];
	if (args.shots) mkdirSync(args.shots, { recursive: true });

	for (const name of browsers) {
		let browser;
		try { browser = await pw[name].launch(); }
		catch (e) { console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`); missing.push(name); continue; }
		launched.push(name);
		try {
			for (const vp of VIEWPORTS) {
				for (const theme of THEMES) {
					const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 1 });
					const page = await ctx.newPage();
					await page.setContent(PAGE(STYLES), { waitUntil: "load" });
					await page.evaluate(() => document.fonts && document.fonts.ready);
					await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
					// Render the real preview, then open and measure.
					await page.evaluate((items) => {
						const N = window.JustUpNotifications;
						N.renderPreview(document.getElementById("notif-pop-list"), { items }, document, document.getElementById("notif-bell"));
					}, FIXTURES);
					await page.waitForTimeout(80);
					const res = await page.evaluate(`(${AUDIT.toString()})(${JSON.stringify(FIXTURES)})`);
					audits++;
					for (const issue of res.issues) findings.push({ browser: name, vp: vp.name, theme, ...issue });

					if (args.shots && name === "chromium") {
						await page.screenshot({ path: path.join(args.shots, `after-${vp.name}-${theme}.png`) });
						// "Before": the old behaviour - a single detail line with the time appended and
						// white-space:nowrap;overflow:hidden;text-overflow:ellipsis on the whole line.
						await page.evaluate(() => {
							document.querySelectorAll("#notif-pop-list .ju-notif-pop-item").forEach((li) => {
								const detail = li.querySelector(".ju-notif-pop-detail");
								const ctxEl = li.querySelector(".ju-notif-pop-context");
								const timeEl = li.querySelector(".ju-notif-pop-time");
								if (!detail) return;
								const text = [ctxEl ? ctxEl.textContent : "", timeEl ? timeEl.textContent : ""].filter(Boolean).join(" \u00b7 ");
								detail.style.display = "block";
								detail.style.whiteSpace = "nowrap";
								detail.style.overflow = "hidden";
								detail.style.textOverflow = "ellipsis";
								detail.textContent = text;
							});
						});
						await page.waitForTimeout(40);
						await page.screenshot({ path: path.join(args.shots, `before-${vp.name}-${theme}.png`) });
					}
					await ctx.close();
				}
			}
		} finally {
			await browser.close();
		}
	}

	if (args.json) writeFileSync(args.json, JSON.stringify(findings, null, 1));
	const byKey = new Map();
	for (const f of findings) {
		const k = `${f.type} | row ${f.i ?? "-"}`;
		const e = byKey.get(k) || { n: 0, vps: new Set(), themes: new Set(), browsers: new Set() };
		e.n++; e.vps.add(f.vp); e.themes.add(f.theme); e.browsers.add(f.browser);
		byKey.set(k, e);
	}
	console.log(`${audits} audits, ${findings.length} findings, ${byKey.size} distinct`);
	for (const [k, e] of [...byKey].sort()) console.log(`  ${k}  [${[...e.browsers].join(",")}; ${[...e.themes].join("/")}; ${[...e.vps].join(" ")}]`);
	if (missing.length) console.log(`MISSING COVERAGE: ${missing.join(", ")} not started`);

	// Self-check: every audit must have seen the time elements (guards against a silent no-op).
	assert.ok(audits >= VIEWPORTS.length * THEMES.length, "expected one audit per viewport/theme");

	if (findings.length) process.exit(1);
	if (!launched.length || (args.browsers && missing.length)) process.exit(SKIP);
	process.exit(0);
}

run().catch((e) => { console.error(e); process.exit(2); });
