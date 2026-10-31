#!/usr/bin/env node
//
// tools/viewport-matrix.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Local browser check of the layout contract (docs/development/layout.md): loads the running app in
// a real browser engine across a viewport matrix and measures the invariants instead of looking at
// screenshots. It is a local audit, not a CI gate (.github/AGENTS.md); it installs nothing.
//
//   JUSTUP_UX_BASE_URL     running app, default http://127.0.0.1:8000
//   JUSTUP_UX_USER         login user, default admin
//   JUSTUP_UX_PASSWORD     login password (required)
//   JUSTUP_UX_ENGINES      comma list of chromium,webkit,firefox; default chromium
//   JUSTUP_UX_PLAYWRIGHT   directory holding node_modules/playwright-core (default: repo, then global)
//   JUSTUP_UX_OUT          JSON report path; default stdout summary only
//   JUSTUP_UX_SEED         number of targets to create when fewer exist, default 24
//   JUSTUP_UX_SET          matrix | quick, default matrix
//
// Exit code 1 when an invariant is violated, 2 when the tool cannot run.

import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ENV = process.env;
const BASE = (ENV.JUSTUP_UX_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const USER = ENV.JUSTUP_UX_USER || 'admin';
const PASSWORD = ENV.JUSTUP_UX_PASSWORD || '';
const ENGINES = (ENV.JUSTUP_UX_ENGINES || 'chromium').split(',').map((s) => s.trim()).filter(Boolean);
const SEED = Number(ENV.JUSTUP_UX_SEED || 24);
const QUICK = ENV.JUSTUP_UX_SET === 'quick';

// Zoom is emulated by the CSS-px viewport it leaves: 200 % on 1280x720 is 640x360, 400 % is 320x180.
const VIEWPORTS = [
	{ name: '320x568', width: 320, height: 568 },
	{ name: '390x844', width: 390, height: 844 },
	{ name: '768x1024', width: 768, height: 1024 },
	{ name: '1280x720', width: 1280, height: 720 },
	{ name: '1440x900', width: 1440, height: 900 },
	{ name: '1920x1080', width: 1920, height: 1080 },
	{ name: 'landscape-844x390', width: 844, height: 390 },
	{ name: 'landscape-1024x768', width: 1024, height: 768 },
	{ name: 'short-1280x400', width: 1280, height: 400 },
	{ name: 'short-390x320', width: 390, height: 320 },
	{ name: 'zoom200-640x360', width: 640, height: 360 },
	{ name: 'zoom400-320x180', width: 320, height: 180 },
];
const QUICK_VIEWPORTS = ['320x568', '1280x720', 'short-1280x400', 'zoom200-640x360'];

function playwrightCore() {
	const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
	const candidates = [ENV.JUSTUP_UX_PLAYWRIGHT, repo];
	try {
		candidates.push(path.dirname(execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim()));
	} catch { /* npm missing: remaining candidates only */ }
	for (const dir of candidates.filter(Boolean)) {
		try {
			return createRequire(path.join(dir, 'noop.js'))('playwright-core');
		} catch { /* try next */ }
	}
	console.error('playwright-core not found. Set JUSTUP_UX_PLAYWRIGHT; this tool never installs it.');
	process.exit(2);
}

// Runs inside the page. Returns { info, violations }. Selectors in `actions` must be reachable.
function measureLayout({ actions, scrollEnd }) {
	const vw = window.innerWidth;
	const vh = window.innerHeight;
	const de = document.documentElement;
	const shell = document.getElementById('app-shell');
	const main = document.querySelector('main');
	const topbar = document.querySelector('.ju-topbar');
	const footer = document.querySelector('.ju-footer');
	const violations = [];
	// The app sets smooth scrolling; measurements need the final position at once. A scroll animation
	// still running from the previous step would retarget WebKit's later focus scroll (false
	// focus-obscured), so it is cancelled by an instant scroll to the current position.
	de.style.scrollBehavior = 'auto';
	window.scrollTo(window.scrollX, window.scrollY);
	if (document.querySelector('main')) { const m = document.querySelector('main'); m.scrollTo(m.scrollLeft, m.scrollTop); }
	const fail = (rule, detail) => violations.push({ rule, detail });
	const name = (el) => {
		if (!el || el === document) return 'document';
		const cls = [...el.classList].slice(0, 2).join('.');
		return el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') + (cls ? `.${cls}` : '');
	};
	const rectOf = (el) => el.getBoundingClientRect();
	const shown = (el) => {
		const r = rectOf(el);
		if (r.width <= 1 || r.height <= 1) return false;
		const cs = getComputedStyle(el);
		return cs.visibility !== 'hidden' && cs.display !== 'none';
	};
	// Content of a closed <details> is neither rendered nor focusable, though it keeps a rect.
	const inClosedDetails = (el) => {
		for (let a = el.parentElement; a; a = a.parentElement) {
			if (a.tagName === 'DETAILS' && !a.open) {
				const summary = el.closest('summary');
				if (!(summary && summary.parentElement === a)) return true;
			}
		}
		return false;
	};
	const skip = (el) => inClosedDetails(el) || el.closest('.visually-hidden, .visually-hidden-focusable, .ju-hp, [hidden], .modal:not(.show), .toast:not(.show), .d-none, #server-offline-overlay');
	const clipAxes = (cs) => ({ x: ['hidden', 'clip'].includes(cs.overflowX), y: ['hidden', 'clip'].includes(cs.overflowY) });

	// Only sticky or fixed bars can cover content; their rects are read fresh because scrolling moves them.
	const fixedBars = [topbar, footer].filter((b) => b && ['sticky', 'fixed'].includes(getComputedStyle(b).position));
	const barOverlap = (r) => fixedBars.some((bar) => {
		const b = bar.getBoundingClientRect();
		return b.height > 0 && r.bottom > b.top + 1 && r.top < b.bottom - 1 && r.right > b.left && r.left < b.right;
	});

	const fit = !!shell && /ju-shell--viewport/.test(shell.className) && rectOf(shell).height <= vh + 1;
	const mainCs = main ? getComputedStyle(main) : null;

	// Page-level horizontal overflow and the elements that cause it.
	if (de.scrollWidth > de.clientWidth + 1 || document.body.scrollWidth > de.clientWidth + 1) {
		fail('page-horizontal-overflow', `scrollWidth ${de.scrollWidth} > clientWidth ${de.clientWidth}`);
	}
	const outside = [];
	for (const el of document.body.querySelectorAll('*')) {
		if (!shown(el) || skip(el)) continue;
		const r = rectOf(el);
		if (r.right <= vw + 1 && r.left >= -1) continue;
		let clipped = false;
		for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
			const cs = getComputedStyle(a);
			if (cs.position === 'fixed' || cs.overflowX !== 'visible') { clipped = true; break; }
		}
		if (!clipped) outside.push(`${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}]`);
	}
	if (outside.length) fail('content-outside-container', outside.slice(0, 4).join('; '));

	// Interactive elements cut off by a non-scrollable clipping ancestor.
	const clippedControls = [];
	for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [tabindex]:not([tabindex="-1"])')) {
		if (!shown(el) || skip(el)) continue;
		const r = rectOf(el);
		for (let a = el.parentElement; a; a = a.parentElement) {
			const ax = clipAxes(getComputedStyle(a));
			if (!ax.x && !ax.y) continue;
			const ar = rectOf(a);
			const cutX = ax.x && (r.left < ar.left - 1 || r.right > ar.right + 1);
			const cutY = ax.y && (r.top < ar.top - 1 || r.bottom > ar.bottom + 1);
			if (cutX || cutY) { clippedControls.push(`${name(el)} in ${name(a)}`); break; }
		}
	}
	if (clippedControls.length) fail('clipped-content', clippedControls.slice(0, 4).join('; '));

	// Scroll ownership.
	const docScrolls = de.scrollHeight > de.clientHeight + 1;
	const regions = [];
	for (const el of document.body.querySelectorAll('*')) {
		const cs = getComputedStyle(el);
		if (!['auto', 'scroll'].includes(cs.overflowY) || el.scrollHeight <= el.clientHeight + 1 || !shown(el)) continue;
		if (el.closest('.modal, #server-offline-overlay, .ju-sidebar, .ju-notif-popover')) continue;
		regions.push(name(el));
	}
	const mainScrolls = !!main && ['auto', 'scroll'].includes(mainCs.overflowY) && main.scrollHeight > main.clientHeight + 1;
	if (fit && docScrolls) fail('unexpected-scroll-region', `document scrolls in viewport mode (${de.scrollHeight} > ${de.clientHeight})`);
	if (!fit && main && ['auto', 'scroll'].includes(mainCs.overflowY)) fail('nested-scroll-trap', 'main is a scroll container in flow mode');

	// Action reachability and focus visibility under sticky bars.
	const hiddenByBar = (el) => barOverlap(rectOf(el));
	for (const sel of actions || []) {
		const el = document.querySelector(sel);
		if (!el || !shown(el)) continue;
		el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
		const r = rectOf(el);
		if (r.left < -1 || r.right > vw + 1 || r.top < -1 || r.bottom > vh + 1 || hiddenByBar(el)) {
			fail('unreachable-action', `${sel} at [${Math.round(r.top)}..${Math.round(r.bottom)}] of ${vh}`);
		}
	}
	const focusables = main ? [...main.querySelectorAll('a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled]), summary')] : [];
	const obscured = [];
	for (const el of focusables.filter((e) => shown(e) && !skip(e)).slice(0, 40)) {
		el.focus({ preventScroll: false });
		if (document.activeElement !== el) continue; // disabled by a fieldset or otherwise unfocusable
		const r = rectOf(el);
		if (barOverlap(r)) obscured.push(name(el));
		else if (r.right < 0 || r.left > vw) obscured.push(`${name(el)} (off-screen x)`);
	}
	if (obscured.length) fail('focus-obscured', obscured.slice(0, 4).join('; '));

	// End of the content must not sit behind the footer.
	if (scrollEnd) {
		window.scrollTo(0, 1e6);
		if (main) main.scrollTop = 1e6;
		let bottom = 0;
		if (main) {
			for (const el of main.querySelectorAll('*')) {
				if (!shown(el) || skip(el) || getComputedStyle(el).position === 'fixed') continue;
				let inRegion = false;
				for (let a = el.parentElement; a && a !== main; a = a.parentElement) {
					if (['auto', 'scroll', 'hidden', 'clip'].includes(getComputedStyle(a).overflowY)) { inRegion = true; break; }
				}
				if (!inRegion) bottom = Math.max(bottom, rectOf(el).bottom);
			}
		}
		const f = footer && rectOf(footer);
		if (f && fixedBars.includes(footer) && bottom > f.top + 1) fail('fixed-overlap', `content ends at ${Math.round(bottom)}, footer starts at ${Math.round(f.top)}`);
		if (f && f.bottom > vh + 1 && !docScrolls) fail('clipped-content', `footer bottom ${Math.round(f.bottom)} > viewport ${vh}`);
		window.scrollTo(0, 0);
		if (main) main.scrollTop = 0;
	}
	de.style.scrollBehavior = '';

	return {
		info: {
			mode: shell ? (shell.className.match(/ju-shell--[\w-]+/g) || ['flow']).join(' ') : 'standalone',
			fit,
			scrollOwner: docScrolls ? 'document' : mainScrolls ? 'main' : 'none',
			regions: regions.slice(0, 5),
		},
		violations,
	};
}

// Print layout: viewport limits must not cut the page.
function measurePrint() {
	const main = document.querySelector('main');
	const shell = document.getElementById('app-shell');
	const out = [];
	if (!main || !shell) return out;
	const cs = getComputedStyle(main);
	if (cs.overflowY !== 'visible' && main.scrollHeight > main.clientHeight + 1) out.push(`main clips ${main.scrollHeight - main.clientHeight}px`);
	if (shell.getBoundingClientRect().height + 1 < main.scrollHeight) out.push('shell shorter than content');
	for (const el of main.querySelectorAll('*')) {
		const s = getComputedStyle(el);
		if (['auto', 'scroll'].includes(s.overflowY) && el.scrollHeight > el.clientHeight + 1 && el.clientHeight > 0) {
			out.push(`${el.tagName.toLowerCase()}.${[...el.classList][0] || ''} clips ${el.scrollHeight - el.clientHeight}px`);
			if (out.length > 4) break;
		}
	}
	return out;
}

const settle = (page) => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 250)))));

async function login(context) {
	const page = await context.newPage();
	await page.goto(`${BASE}/ui/login`);
	await page.fill('#username', USER);
	await page.fill('input[type=password]', PASSWORD);
	await Promise.all([page.waitForURL((u) => !/login/.test(u.pathname), { timeout: 20000 }), page.click('button[type=submit]')]);
	return page;
}

async function seed(page) {
	const created = await page.evaluate(async (want) => {
		const list = await (await window.apiFetch('/targets')).json();
		let made = 0;
		for (let i = list.length; i < want; i += 1) {
			const host = `layout-${i}-with-a-rather-long-subdomain-name.example.com`;
			const pre = await (await window.apiFetch('/targets/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: 'website', address: `https://${host}`, group: i % 3 ? `Group ${i % 3}` : null }) })).json();
			const res = await window.apiFetch('/targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...pre.values, name: `Layout seed ${i} ${'long name '.repeat(i % 4)}`, group: i % 3 ? `Group ${i % 3}` : null, alert_recipient_ids: [] }) });
			if (res.ok) made += 1;
		}
		return { have: list.length, made };
	}, SEED);
	return created;
}

// A user that must change the password reaches the standalone change-password page after login.
const FORCED = { username: 'layout-forced', password: 'TempPassw0rd!2026' };
async function ensureForcedUser(page) {
	// An admin-set password is temporary, so the PUT below marks the user as must-change.
	await page.evaluate(async (u) => {
		const json = { 'Content-Type': 'application/json' };
		await window.apiFetch('/users', { method: 'POST', headers: json, body: JSON.stringify({ username: u.username, password: u.password }) });
		const user = (await (await window.apiFetch('/users')).json()).find((x) => x.username === u.username);
		await window.apiFetch(`/users/${user.id}`, { method: 'PUT', headers: json, body: JSON.stringify({ username: u.username, is_admin: false, can_close_alerts: false, new_password: u.password }) });
	}, FORCED);
}

// Scenarios: a path plus an optional state change, measured after every load and resize.
function scenarios(firstTargetId) {
	const tab = (id) => async (page) => {
		// Settings swaps its tab strip for a select below the md breakpoint.
		if (id.endsWith('-tab') && !(await page.isVisible(`#${id}`)) && (await page.isVisible('#settingsTabSelect'))) {
			await page.selectOption('#settingsTabSelect', `#${id.replace('-tab', '-pane')}`);
		} else {
			await page.click(`#${id}`);
		}
		await settle(page);
	};
	const wizardTo = (n) => async (page) => {
		for (let i = 0; i < n; i += 1) {
			if (i === 1) await page.fill('#wizard-address', 'https://example.com');
			await page.click('#wizard-next');
			await page.waitForFunction((s) => document.querySelector(`.target-wizard-step[data-step="${s}"]`)?.hidden === false, i + 1, { timeout: 15000 });
		}
		await settle(page);
	};
	const wizardActions = ['#wizard-back', '#wizard-next', '#wizard-save', '#wizard-cancel'];
	const list = [
		{ name: 'dashboard', path: '/' },
		{ name: 'targets', path: '/ui/targets' },
		{ name: 'wizard-step1-type', path: '/ui/targets/new', actions: wizardActions },
		{ name: 'wizard-step2-address', path: '/ui/targets/new', prepare: wizardTo(1), actions: wizardActions },
		{ name: 'wizard-step3-checks', path: '/ui/targets/new', prepare: wizardTo(2), actions: wizardActions },
		{ name: 'wizard-step4-review', path: '/ui/targets/new', prepare: wizardTo(3), actions: wizardActions },
		{ name: 'groups', path: '/ui/groups' },
		{ name: 'notifications', path: '/ui/notifications' },
		{ name: 'about', path: '/ui/about' },
		{ name: 'settings-general', path: '/ui/settings' },
		...['network', 'storage', 'backup', 'smtp', 'users', 'recipients', 'signal', 'spooler'].map((t) => ({ name: `settings-${t}`, path: '/ui/settings', prepare: tab(`${t}-tab`) })),
	];
	if (firstTargetId) {
		list.push(
			{ name: 'target-detail-dashboard', path: `/ui/targets/${firstTargetId}`, ready: '#target-dashboard-pane.active' },
			{ name: 'target-detail-network', path: `/ui/targets/${firstTargetId}`, prepare: tab('target-network-diagnostics-tab') },
			{ name: 'target-editor', path: `/ui/targets/${firstTargetId}/edit` },
		);
	}
	return list;
}

async function overlayChecks(page, vp) {
	const out = [];
	await page.goto(`${BASE}/ui/groups`);
	await settle(page);
	await page.click('#newGroupButton');
	await page.waitForSelector('#groupModal.show');
	await settle(page);
	const modal = await page.evaluate(() => {
		const d = document.querySelector('#groupModal .modal-dialog');
		const c = document.querySelector('#groupModal .modal-content');
		const close = document.querySelector('#groupModal .btn-close, #groupModal [data-bs-dismiss]');
		const r = c.getBoundingClientRect();
		const cr = close.getBoundingClientRect();
		return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, vw: innerWidth, vh: innerHeight, closeIn: cr.top >= 0 && cr.bottom <= innerHeight && cr.left >= 0 && cr.right <= innerWidth, centeredBox: !!d };
	});
	if (modal.top < -1 || modal.bottom > modal.vh + 1 || modal.left < -1 || modal.right > modal.vw + 1 || !modal.closeIn) out.push({ rule: 'modal-placement', detail: `modal [${Math.round(modal.top)}..${Math.round(modal.bottom)}] of ${modal.vh}, close reachable ${modal.closeIn}` });
	await page.waitForTimeout(500); // Bootstrap ignores hide() while the fade is still running
	await page.keyboard.press('Escape');
	await page.waitForSelector('#groupModal', { state: 'hidden' });
	await settle(page);
	const lock = await page.evaluate(() => ({ cls: document.body.classList.contains('modal-open'), ov: getComputedStyle(document.body).overflow, mainOv: getComputedStyle(document.querySelector('main')).overflowY }));
	if (lock.cls || lock.ov === 'hidden' || lock.mainOv === 'hidden') out.push({ rule: 'scroll-state-regression', detail: `after modal close: ${JSON.stringify(lock)}` });

	await page.click('#notif-bell');
	await settle(page);
	const pop = await page.evaluate(() => {
		const p = document.getElementById('notif-popover');
		const r = p.getBoundingClientRect();
		return { shown: !p.hidden, top: r.top, bottom: r.bottom, left: r.left, right: r.right, vw: innerWidth, vh: innerHeight };
	});
	if (pop.shown && (pop.left < -1 || pop.right > pop.vw + 1 || pop.top < -1 || pop.bottom > pop.vh + 1)) out.push({ rule: 'popover-placement', detail: `popover [${Math.round(pop.left)}..${Math.round(pop.right)}]x[${Math.round(pop.top)}..${Math.round(pop.bottom)}] of ${pop.vw}x${pop.vh}` });
	await page.keyboard.press('Escape');
	if (vp.width < 992) {
		await page.click('#sidebar-toggle');
		await settle(page);
		const side = await page.evaluate(() => {
			const s = document.getElementById('app-sidebar');
			const r = s.getBoundingClientRect();
			const last = [...s.querySelectorAll('a[href]')].pop();
			const scrollable = s.scrollHeight > s.clientHeight + 1 && getComputedStyle(s).overflowY !== 'hidden';
			const lr = last.getBoundingClientRect();
			return { left: r.left, right: r.right, bottom: r.bottom, vh: innerHeight, reach: lr.bottom <= innerHeight + 1 || scrollable };
		});
		if (side.left < -1 || !side.reach) out.push({ rule: 'sidebar-reachability', detail: JSON.stringify(side) });
		await page.keyboard.press('Escape');
	}
	// Disconnect overlay: the card must be reachable from the top, also when it is taller than the view.
	await page.evaluate(() => document.body.classList.add('server-offline'));
	await settle(page);
	const off = await page.evaluate(() => {
		const o = document.getElementById('server-offline-overlay');
		const c = o.querySelector('.card').getBoundingClientRect();
		return { top: c.top, bottom: c.bottom, scrollable: o.scrollHeight > o.clientHeight + 1, vh: innerHeight, left: c.left, right: c.right, vw: innerWidth };
	});
	if (off.top < -1 || off.left < -1 || off.right > off.vw + 1 || (off.bottom > off.vh + 1 && !off.scrollable)) out.push({ rule: 'overlay-reachability', detail: `offline card [${Math.round(off.top)}..${Math.round(off.bottom)}] of ${off.vh}, scrollable ${off.scrollable}` });
	await page.evaluate(() => document.body.classList.remove('server-offline'));
	return out;
}

async function main() {
	if (!PASSWORD) { console.error('JUSTUP_UX_PASSWORD is required.'); process.exit(2); }
	const pw = playwrightCore();
	const viewports = QUICK ? VIEWPORTS.filter((v) => QUICK_VIEWPORTS.includes(v.name)) : VIEWPORTS;
	const results = [];
	let firstTargetId = null;
	for (const engineName of ENGINES) {
		const browser = await pw[engineName].launch();
		const authCtx = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 900 } });
		const authPage = await login(authCtx);
		const seeded = await seed(authPage);
		await ensureForcedUser(authPage);
		firstTargetId = await authPage.evaluate(async () => { const l = await (await window.apiFetch('/targets')).json(); return l[0]?.id ?? null; });
		console.log(`[${engineName}] targets: had ${seeded.have}, created ${seeded.made}, first id ${firstTargetId}`);
		const state = await authCtx.storageState();
		await authCtx.close();
		const list = scenarios(firstTargetId);
		const record = (entry) => { results.push({ engine: engineName, ...entry }); };

		for (const vp of viewports) {
			const ctx = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: vp.width, height: vp.height }, storageState: state });
			const page = await ctx.newPage();
			const errors = [];
			page.on('pageerror', (e) => errors.push(String(e).slice(0, 160)));
			for (const sc of list) {
				await page.goto(`${BASE}${sc.path}`);
				if (sc.ready) await page.waitForSelector(sc.ready);
				await settle(page);
				try {
					if (sc.prepare) await sc.prepare(page);
				} catch (err) {
					throw new Error(`${sc.name} @ ${vp.name}: ${String(err.message).split('\n')[0]}`);
				}
				const m = await page.evaluate(measureLayout, { actions: sc.actions, scrollEnd: true });
				record({ scenario: sc.name, viewport: vp.name, path: 'load', ...m.info, violations: m.violations });
			}
			// Standalone pages in a logged-out context.
			const anon = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: vp.width, height: vp.height } });
			const ap = await anon.newPage();
			await ap.goto(`${BASE}/ui/login`);
			await settle(ap);
			const lm = await ap.evaluate(measureLayout, { actions: ['button[type=submit]'], scrollEnd: false });
			record({ scenario: 'login', viewport: vp.name, path: 'load', ...lm.info, violations: lm.violations });
			await anon.close();
			const forced = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: vp.width, height: vp.height } });
			const fp = await forced.newPage();
			await fp.goto(`${BASE}/ui/login`);
			await fp.fill('#username', FORCED.username);
			await fp.fill('input[type=password]', FORCED.password);
			await Promise.all([fp.waitForURL(/change-password/, { timeout: 20000 }), fp.click('button[type=submit]')]);
			await settle(fp);
			const cm = await fp.evaluate(measureLayout, { actions: ['button[type=submit]'], scrollEnd: false });
			record({ scenario: 'change-password', viewport: vp.name, path: 'load', ...cm.info, violations: cm.violations });
			await forced.close();
			const ov = await overlayChecks(page, vp);
			record({ scenario: 'overlays', viewport: vp.name, path: 'load', mode: '-', violations: ov });
			if (errors.length) record({ scenario: 'page-errors', viewport: vp.name, path: 'load', mode: '-', violations: errors.slice(0, 3).map((detail) => ({ rule: 'page-error', detail })) });
			await ctx.close();
		}

		// Live resize: load large, shrink to each viewport, return to large; nothing may stay broken.
		const resizeScenarios = list.filter((s) => ['dashboard', 'wizard-step1-type', 'wizard-step4-review', 'about', 'target-detail-dashboard', 'target-detail-network', 'settings-smtp', 'targets'].includes(s.name));
		for (const sc of resizeScenarios) {
			const ctx = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 900 }, storageState: state });
			const page = await ctx.newPage();
			await page.goto(`${BASE}${sc.path}`);
			if (sc.ready) await page.waitForSelector(sc.ready);
			await settle(page);
			try {
				if (sc.prepare) await sc.prepare(page);
			} catch (err) {
				throw new Error(`${sc.name} @ resize start: ${String(err.message).split('\n')[0]}`);
			}
			for (const vp of [...viewports, { name: '1440x900(back)', width: 1440, height: 900 }]) {
				await page.setViewportSize({ width: vp.width, height: vp.height });
				await settle(page);
				const m = await page.evaluate(measureLayout, { actions: sc.actions, scrollEnd: true });
				record({ scenario: sc.name, viewport: vp.name, path: 'resize', ...m.info, violations: m.violations });
			}
			await ctx.close();
		}

		// Print: viewport limits must not clip the printed page.
		const pctx = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1280, height: 720 }, storageState: state });
		const pp = await pctx.newPage();
		await pp.emulateMedia({ media: 'print' });
		for (const sc of list.filter((s) => !s.prepare)) {
			await pp.goto(`${BASE}${sc.path}`);
			if (sc.ready) await pp.waitForSelector(sc.ready);
			await settle(pp);
			if (sc.prepare) await sc.prepare(pp);
			const clip = await pp.evaluate(measurePrint);
			record({ scenario: sc.name, viewport: 'print', path: 'print', mode: '-', violations: clip.map((detail) => ({ rule: 'print-clipped', detail })) });
		}
		await pctx.close();
		await browser.close();
	}

	const bad = results.filter((r) => r.violations.length);
	for (const r of bad) {
		for (const v of r.violations) console.log(`FAIL [${r.engine}] ${r.scenario} @ ${r.viewport} (${r.path}; ${r.mode}) ${v.rule}: ${v.detail}`);
	}
	console.log(`${results.length} measurements, ${bad.length} with violations, ${bad.reduce((n, r) => n + r.violations.length, 0)} violations`);
	if (ENV.JUSTUP_UX_OUT) writeFileSync(ENV.JUSTUP_UX_OUT, JSON.stringify(results, null, 1));
	process.exit(bad.length ? 1 : 0);
}

main().catch((err) => { console.error(err); process.exit(2); });
