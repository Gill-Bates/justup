//
// tests/layout/notifications.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser contract for the notification list rows (DESIGN.md, "Notification List Visual &
// Interaction Contract"). It seeds the real app with report, target and alert notifications in the
// shapes that matter (available, expired and removed resources, short and long text) and measures
// computed styles and bounding rectangles on the real /ui/notifications page.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { waitForVisualStability } from "./harness.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
// Absolute per-edge tolerance in CSS px, as in the dashboard geometry contract. Do not widen it.
const NOTIF_GEOMETRY_TOL_PX = 1;
const MIN_TEXT_CONTRAST = 4.5;
const LONG_FILE = `20261102-${"a-very-long-site-name-without-break-opportunity-".repeat(3)}report.pdf`;
const LONG_TEXT = "Unbreakable-".repeat(18) + "end";

// `expect` is the action kind the server must resolve; rows are listed newest first.
const ROWS = [
	{ expect: "unavailable", read: false, event: "report.completed", category: "reports", severity: "success", title: "PDF ready", body: "20261102-wiedenest_de-report.pdf", resource: "report_job" },
	{ expect: "unavailable", read: false, event: "report.completed", category: "reports", severity: "success", title: `PDF ready: ${"efg-luettringhausen-".repeat(5)}`, body: LONG_FILE, resource: "report_job" },
	{ expect: "download", read: true, event: "report.completed", category: "reports", severity: "success", title: "PDF ready", body: "20261102-example_com-report.pdf", resource: "report_job", job: { status: "completed", hours: 24 } },
	{ expect: "download", read: false, event: "report.completed", category: "reports", severity: "success", title: "PDF ready", body: LONG_FILE, resource: "report_job", job: { status: "completed", hours: 24 } },
	{ expect: "unavailable", read: true, event: "report.completed", category: "reports", severity: "success", title: "PDF ready", body: "20261102-expired-report.pdf", resource: "report_job", job: { status: "completed", hours: -2 } },
	{ expect: "link", read: false, event: "report.failed", category: "reports", severity: "error", title: "PDF failed", body: "Example Web", resource: "report_job", job: { status: "failed", hours: 24 }, targetLink: true },
	{ expect: "unavailable", read: true, event: "report.failed", category: "reports", severity: "error", title: "PDF failed", body: "Removed report target", resource: "report_job" },
	{ expect: "unavailable", read: false, event: "activity.target.updated", category: "activity", severity: "info", title: "Target updated", body: "Example Web", resource: "target", resourceId: "987654" },
	{ expect: "unavailable", read: false, event: "activity.target.deleted", category: "activity", severity: "warning", title: "Target removed", body: "", resource: "target", resourceId: "987655" },
	{ expect: "unavailable", read: false, event: "monitoring.alert.opened", category: "monitoring", severity: "warning", title: "Alert opened", body: `HTTP check failed for ${"efg-luettringhausen-and-a-long-name ".repeat(6)}`, resource: "alert", resourceId: "layout-missing-alert" },
	{ expect: "none", read: true, event: "activity.note", category: "activity", severity: "info", title: "Long unbreakable description", body: LONG_TEXT, resource: null },
	{ expect: "none", read: false, event: "activity.note.empty", category: "activity", severity: "info", title: "No description", body: "", resource: null },
];
const VIEWPORTS = [
	{ name: "1280x720", w: 1280, h: 720, dsf: 1 },
	{ name: "390x844", w: 390, h: 844, dsf: 1 },
	{ name: "320x640", w: 320, h: 640, dsf: 1 },
	{ name: "1280x720-effective-zoom-200pct", w: 640, h: 360, dsf: 2 }, // viewport proxy for zoom: 640x360 CSS px at DPR 2
];
const THEMES = ["light", "dark"];
// Rows whose hover surface is measured: unread button row, download link and (error) link row.
const HOVER_ROWS = [0, 3, 5];

function seedNotifications(app) {
	const script = `import json, sys
from datetime import timedelta
from app.db import notifications as inbox
from app.db import sqlite as sqlite_db
from app.utils.config import load_config
from app.utils.time import utcnow
rows = json.loads(sys.argv[1])
conn = sqlite_db.connect(load_config().db_path)
admin = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()["id"]
target = conn.execute("SELECT id FROM targets ORDER BY id LIMIT 1").fetchone()["id"]
now = utcnow()
# The list is ordered by id descending: insert in reverse so list position equals fixture position.
conn.execute("DELETE FROM notification_reads")
conn.execute("DELETE FROM notifications")
conn.execute("DELETE FROM report_jobs WHERE job_id LIKE 'layout-job-%'")
for index, row in reversed(list(enumerate(rows))):
    resource_id = row.get("resourceId")
    job = row.get("job")
    if row["resource"] == "report_job":
        resource_id = f"layout-job-{index}"
    if job:
        conn.execute(
            "INSERT INTO report_jobs(job_id, target_id, status, progress, filename, created_at, completed_at, expires_at, user_id)"
            " VALUES(?, ?, ?, 100, ?, ?, ?, ?, ?)",
            (resource_id, target, job["status"], row["body"], now, now, now + timedelta(hours=job["hours"]), admin),
        )
    notification_id = inbox.emit(
        conn,
        event_type=row["event"], category=row["category"], severity=row["severity"],
        title=row["title"], body=row["body"], idempotency_key=f"layout-notification-{index}",
        audience="user", recipient_user_id=admin, target_id=target if row.get("targetLink") else None,
        resource_type=row["resource"], resource_id=resource_id, created_at=now - timedelta(minutes=index),
    )
    assert notification_id, index
    if row["read"]:
        conn.execute("INSERT INTO notification_reads(notification_id, user_id, read_at) VALUES(?, ?, ?)", (notification_id, admin, now))
conn.commit()
`;
	const result = spawnSync(app.python, ["-c", script, JSON.stringify(ROWS)], {
		cwd: repo,
		env: { ...process.env, JUSTUP_DATA_DIR: app.data },
		encoding: "utf8",
	});
	if (result.error || result.status !== 0) {
		throw new Error(`notification fixture failed: ${result.error?.message || result.stderr || result.status}`);
	}
}

// In-page measurement: computed styles and rectangles of every row part (no assertions here).
function measureRows(hoverBackgroundProbe) {
	const rect = (el) => {
		if (!el) return null;
		const { left, right, top, bottom, width, height } = el.getBoundingClientRect();
		return { left, right, top, bottom, width, height };
	};
	const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
	const rgba = (css) => {
		canvas.clearRect(0, 0, 1, 1);
		canvas.fillStyle = "#000";
		canvas.fillStyle = css;
		canvas.fillRect(0, 0, 1, 1);
		const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
		return { r, g, b, a: a / 255 };
	};
	const luminance = ({ r, g, b }) => {
		const lin = [r, g, b].map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
		return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
	};
	const over = (fg, bg) => ({ r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 });
	const contrast = (fg, bg) => {
		const l1 = luminance(over(fg, bg)) + 0.05;
		const l2 = luminance(bg) + 0.05;
		return Math.max(l1, l2) / Math.min(l1, l2);
	};
	const hue = ({ r, g, b }) => {
		const max = Math.max(r, g, b) / 255;
		const min = Math.min(r, g, b) / 255;
		const d = max - min;
		if (d === 0) return { h: 0, s: 0 };
		const [rn, gn, bn] = [r, g, b].map((v) => v / 255);
		const h = max === rn ? ((gn - bn) / d) % 6 : max === gn ? (bn - rn) / d + 2 : (rn - gn) / d + 4;
		return { h: (h * 60 + 360) % 360, s: d / (1 - Math.abs(max + min - 1)) };
	};
	const probe = document.createElement("div");
	probe.style.cssText = `position:absolute;visibility:hidden;background-color:${hoverBackgroundProbe}`;
	document.body.append(probe);
	const hoverToken = rgba(getComputedStyle(probe).backgroundColor);
	probe.remove();
	const doc = document.documentElement;
	return {
		docOverflowX: doc.scrollWidth - doc.clientWidth,
		hoverToken,
		rows: Array.from(document.querySelectorAll("#np-list > .ju-notif-item")).map((item) => {
			const q = (selector) => item.querySelector(selector);
			const content = q(":scope > .ju-notif-swipe-content");
			const title = q(".ju-notif-title");
			const titleText = q(".ju-notif-title-text");
			const context = q(".ju-notif-context");
			const unavailable = q(".ju-notif-unavailable");
			const label = unavailable && Array.from(unavailable.childNodes).find((node) => node.nodeType === 3 && node.textContent.trim());
			const labelRange = label && document.createRange();
			if (labelRange) labelRange.selectNodeContents(label);
			const style = (el, prop) => getComputedStyle(el)[prop];
			const bg = rgba(style(content, "backgroundColor"));
			const textColor = (el) => (el ? contrast(rgba(style(el, "color")), over(bg, rgba(style(item, "backgroundColor")))) : null);
			return {
				kind: content.dataset.action || (content.classList.contains("is-actionable") ? "button" : "static"),
				unread: item.classList.contains("is-unread"),
				actionable: content.classList.contains("is-actionable"),
				item: rect(item), content: rect(content), title: rect(title), titleText: rect(titleText),
				badge: rect(q(".ju-notif-title .ju-notif-status")), body: rect(q(".ju-notif-body")), context: rect(context),
				unavailable: rect(unavailable), icon: rect(unavailable && unavailable.querySelector(".material-icons")),
				label: rect(labelRange), time: rect(q(".ju-notif-time")), dismiss: rect(q(".ju-notif-dismiss-action")),
				dismissButtonVisible: (() => {
					const button = q(".ju-notif-dismiss-button");
					const r = button.getBoundingClientRect();
					const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
					return button.contains(hit);
				})(),
				itemScrollOverflow: item.scrollWidth - item.clientWidth,
				contentBg: bg, backgroundImage: style(content, "backgroundImage"),
				severityBadge: Boolean(q(".ju-notif-title .badge")),
				severityDot: Boolean(q(".ju-notif-title .ju-status-item .ju-status-dot")),
				severityDotBg: q(".ju-notif-title .ju-status-dot") ? rgba(style(q(".ju-notif-title .ju-status-dot"), "backgroundColor")) : null,
				borderLeft: style(item, "borderLeftColor"), outlineStyle: style(content, "outlineStyle"), outlineWidth: style(content, "outlineWidth"),
				outlineColor: style(content, "outlineColor"), outlineOffset: style(content, "outlineOffset"),
				focusRing: (() => { const probe = document.createElement("i"); probe.style.color = "var(--ju-focus-ring)"; document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; })(),
				titleContrast: textColor(titleText), contextContrast: textColor(context || unavailable),
				contextTitleAttr: context ? context.getAttribute("title") === context.textContent : null,
				contextClipped: context ? context.scrollWidth > context.clientWidth + 1 : null,
				titleTextTitleAttr: titleText.getAttribute("title") === titleText.textContent,
				titleTextClipped: titleText.scrollWidth > titleText.clientWidth + 1,
				unavailableText: unavailable ? unavailable.textContent.replace("link_off", "").trim() : null,
				unavailableInLink: unavailable ? !!unavailable.closest("a") : null,
				unavailablePseudo: unavailable ? [getComputedStyle(unavailable, "::before").content, getComputedStyle(unavailable, "::after").content] : null,
				unavailableColor: unavailable ? style(unavailable, "color") : null, unavailableBg: unavailable ? style(unavailable, "backgroundColor") : null,
				unavailableFlexWrap: unavailable ? style(unavailable, "flexWrap") : null,
				hue: hue(bg),
			};
		}),
	};
}

// Geometry findings for one rendered state; revealed = delete action opened (hover, focus or swipe).
function geometryFindings(snapshot, narrow, revealed) {
	const findings = [];
	const tol = NOTIF_GEOMETRY_TOL_PX;
	const add = (type, index, detail) => findings.push({ type, row: index, detail });
	if (snapshot.docOverflowX > tol) add("document-overflow-x", -1, snapshot.docOverflowX);
	const slotTops = [];
	const timeRights = [];
	const timeTops = [];
	snapshot.rows.forEach((row, index) => {
		const spec = ROWS[index];
		if (row.itemScrollOverflow > tol) add("row-overflow-x", index, row.itemScrollOverflow);
		const limit = revealed ? row.dismiss.left : row.content.right;
		for (const name of ["title", "titleText", "badge", "body", "context", "unavailable", "label", "time"]) {
			const r = row[name];
			// display: contents (narrow layout) gives the body wrapper no box of its own.
			if (!r || (name === "body" && r.width === 0)) continue;
			if (r.left < row.content.left - tol || r.right > limit + tol) add("part-outside-content", index, `${name} ${r.left.toFixed(1)}..${r.right.toFixed(1)} vs ${row.content.left.toFixed(1)}..${limit.toFixed(1)}`);
		}
		if (row.time && row.body && row.body.right > row.time.left + tol) add("body-overlaps-time", index, `${row.body.right} > ${row.time.left}`);
		if (row.time && row.title && !narrow && row.title.right > row.time.left + tol) add("title-overlaps-time", index, `${row.title.right} > ${row.time.left}`);
		if (row.time && narrow && row.title && row.title.right > row.content.right + tol) add("title-outside-row", index, row.title.right);
		if (row.context && !row.contextTitleAttr) add("context-without-full-title", index, "title attribute differs from text");
		if (row.titleTextClipped && !row.titleTextTitleAttr) add("title-clipped-without-title", index, "ellipsis without title");
		if (revealed && row.content.right > row.dismiss.left + tol) add("delete-overlaps-content", index, `${row.content.right} > ${row.dismiss.left}`);
		if (revealed && !row.dismissButtonVisible) add("delete-button-not-reachable", index, "covered");
		// Secondary text starts in the title column; the status line sits below the description.
		const first = row.context || row.unavailable;
		if (first) {
			if (Math.abs(first.left - row.title.left) > tol) add("secondary-misaligned", index, `${first.left} vs title ${row.title.left}`);
			// The narrow layout keeps a fixed description slot, so only wide rows share the first-line slot.
			if (!narrow || row.context) slotTops.push(first.top - row.title.bottom);
		}
		if (row.unavailable) {
			if (row.unavailable.left < (row.context ? row.context.left : row.title.left) - tol) add("unavailable-left-of-description", index, row.unavailable.left);
			if (Math.abs(row.unavailable.left - (row.context || row.title).left) > tol) add("unavailable-misaligned", index, `${row.unavailable.left} vs ${(row.context || row.title).left}`);
			if (row.context && row.unavailable.top < row.context.bottom - tol) add("unavailable-not-below-description", index, `${row.unavailable.top} < ${row.context.bottom}`);
			if (row.time && row.unavailable.top < row.time.bottom - tol && narrow) add("unavailable-overlaps-time", index, `${row.unavailable.top} < ${row.time.bottom}`);
			// A time that wraps to two lines (narrow, delete open) makes the description row taller.
			const above = narrow && row.time ? Math.max(row.context ? row.context.bottom : 0, row.time.bottom) : row.context?.bottom;
			if (row.context && row.unavailable.top - above > 8) add("unavailable-detached", index, row.unavailable.top - above);
			if (Math.abs((row.icon.top + row.icon.bottom) / 2 - (row.label.top + row.label.bottom) / 2) > 2) add("unavailable-icon-not-centred", index, "icon vs label");
			if (row.unavailable.height > 40) add("unavailable-too-tall", index, row.unavailable.height);
			if (row.unavailableInLink) add("unavailable-is-link", index, "inside anchor");
			if (row.unavailablePseudo.some((value) => value !== "none" && value !== "normal")) add("unavailable-generated-content", index, row.unavailablePseudo.join());
			if (row.unavailableBg !== "rgba(0, 0, 0, 0)") add("unavailable-filled", index, row.unavailableBg);
			if (!row.unavailableText) add("unavailable-text-missing", index, "empty");
		}
		if (spec.expect === "unavailable" && !row.unavailable) add("unavailable-missing", index, "no status line");
		if (spec.expect !== "unavailable" && row.unavailable) add("unavailable-unexpected", index, row.unavailableText);
		if (spec.expect === "link" && row.kind !== "link") add("target-link-missing", index, row.kind);
		if (spec.expect === "none" && (row.kind === "link" || row.kind === "download")) add("unexpected-link", index, row.kind);
		if (spec.expect === "download" && row.kind !== "download") add("download-link-missing", index, row.kind);
		if (spec.expect === "unavailable" && (row.kind === "download" || row.kind === "link")) add("dead-link", index, row.kind);
		if (row.time) { timeRights.push(row.time.right); timeTops.push(row.time.top - row.item.top); }
	});
	// Consistent slots across all rows: the secondary text starts at the same distance below the
	// title and the timestamp keeps one column and one vertical offset.
	if (!revealed && slotTops.length) {
		const min = Math.min(...slotTops);
		const max = Math.max(...slotTops);
		if (max - min > tol) add("secondary-slot-varies", -1, `${min}..${max}`);
	}
	if (!revealed && timeRights.length && Math.max(...timeRights) - Math.min(...timeRights) > tol) add("time-column-varies", -1, `${Math.min(...timeRights)}..${Math.max(...timeRights)}`);
	if (!revealed && timeTops.length && Math.max(...timeTops) - Math.min(...timeTops) > tol + 4) add("time-offset-varies", -1, `${Math.min(...timeTops)}..${Math.max(...timeTops)}`);
	return findings;
}

function stateFindings(snapshot, hovered) {
	const findings = [];
	const same = (a, b) => a === b || (a !== null && b !== null && a.r === b.r && a.g === b.g && a.b === b.b);
	snapshot.rows.forEach((row, index) => {
		// DC-STATE-1 / DC-NOTIF-3: event severity (S4) is a status dot with its word, never a badge.
		if (row.severityBadge || !row.severityDot) findings.push({ type: "severity-not-a-dot", row: index, detail: `badge=${row.severityBadge} dot=${row.severityDot}` });
		for (const [name, value] of [["title", row.titleContrast], ["secondary", row.contextContrast]]) {
			if (value !== null && value < MIN_TEXT_CONTRAST) findings.push({ type: "text-contrast", row: index, detail: `${name} ${value.toFixed(2)}` });
		}
	});
	if (hovered) {
		const { index, snapshot: hoverSnapshot, rest } = hovered;
		const row = hoverSnapshot.rows[index];
		if (!same(row.contentBg, hoverSnapshot.hoverToken)) findings.push({ type: "hover-not-shared-token", row: index, detail: JSON.stringify(row.contentBg) });
		if (same(row.contentBg, rest.rows[index].contentBg)) findings.push({ type: "hover-unchanged", row: index, detail: "no visible hover surface" });
		if (row.hue.s > 0.1 && row.hue.h >= 15 && row.hue.h <= 75) findings.push({ type: "hover-warm-colour", row: index, detail: JSON.stringify(row.hue) });
		const before = rest.rows[index];
		if (!same(row.severityDotBg, before.severityDotBg)) findings.push({ type: "hover-changed-severity-dot", row: index, detail: "severity dot colour" });
		if (row.borderLeft !== before.borderLeft) findings.push({ type: "hover-changed-indicator", row: index, detail: `${before.borderLeft} -> ${row.borderLeft}` });
		if (row.backgroundImage !== before.backgroundImage) findings.push({ type: "hover-changed-severity-tint", row: index, detail: "background-image" });
		if (before.unread !== row.unread) findings.push({ type: "hover-changed-unread", row: index, detail: "unread" });
	}
	return findings;
}

async function snapshotRows(page) {
	return page.evaluate(measureRows, "var(--ju-panel-hover)");
}

async function setTheme(page, theme) {
	await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
	await waitForVisualStability(page);
}

async function auditNotificationRows(page, app, browserName, viewport) {
	await page.setViewportSize({ width: viewport.w, height: viewport.h });
	assert.equal(await page.evaluate(() => window.devicePixelRatio), viewport.dsf, `${browserName}/${viewport.name} device scale factor`);
	const findings = [];
	const tag = (items, variant) => items.map((item) => ({ browser: browserName, vp: viewport.name, variant, ...item }));
	const errors = [];
	page.on("pageerror", (error) => errors.push(String(error)));
	await page.goto(`${app.base}/ui/notifications?category=all`, { waitUntil: "load" });
	await page.waitForSelector("#np-list .ju-notif-item");
	const narrow = await page.evaluate(() => window.matchMedia("(width <= 991.98px)").matches);
	assert.equal(await page.locator("#np-list > .ju-notif-item").count(), ROWS.length, "all seeded notifications are listed");
	for (const theme of THEMES) {
			await setTheme(page, theme);
			await page.mouse.move(0, 0);
			const rest = await snapshotRows(page);
			findings.push(...tag(geometryFindings(rest, narrow, false), `${theme}/rest`), ...tag(stateFindings(rest, null), `${theme}/rest`));
			// Hover on a normal-sized pointer only; touch layouts have no hover surface.
			for (const index of HOVER_ROWS) {
				const row = page.locator("#np-list > .ju-notif-item").nth(index);
				await row.evaluate((el) => el.scrollIntoView({ block: "center" }));
				await row.locator(":scope > .ju-notif-swipe-content").hover({ position: { x: 24, y: 8 } });
				await waitForVisualStability(page);
				const hovered = await snapshotRows(page);
				findings.push(...tag(stateFindings(hovered, { index, snapshot: hovered, rest }), `${theme}/hover`));
				findings.push(...tag(geometryFindings(hovered, narrow, true).filter((f) => f.row === index || f.row === -1), `${theme}/hover-reveal`));
			}
			await page.mouse.move(0, 0);
			// Keyboard focus on a row stays visible without hover and keeps the shared surface.
			const first = page.locator("#np-list > .ju-notif-item").nth(0).locator(":scope > .ju-notif-swipe-content");
			await first.focus();
			await page.keyboard.press("Shift+Tab");
			await page.keyboard.press("Tab");
			await waitForVisualStability(page);
			const focused = await snapshotRows(page);
			const focusedRow = focused.rows[0];
			if (focusedRow.outlineStyle === "none" || parseFloat(focusedRow.outlineWidth) < 2) findings.push(...tag([{ type: "focus-indicator-missing", row: 0, detail: `${focusedRow.outlineStyle} ${focusedRow.outlineWidth}` }], `${theme}/focus`));
			// DC-FOCUS-6 variant V3: the shared neutral ring, drawn inside the clipping row (negative offset).
			if (focusedRow.outlineStyle !== "none" && (focusedRow.outlineColor !== focusedRow.focusRing || parseFloat(focusedRow.outlineOffset) >= 0)) findings.push(...tag([{ type: "focus-indicator-variant", row: 0, detail: `colour ${focusedRow.outlineColor} (ring ${focusedRow.focusRing}), offset ${focusedRow.outlineOffset}` }], `${theme}/focus`));
			// Swipe reveal (touch pointer events) on a row; the content narrows and delete stays reachable.
			await page.mouse.move(0, 0);
			await page.evaluate(() => {
				const item = document.querySelector("#np-list > .ju-notif-item:nth-child(10)");
				item.scrollIntoView({ block: "center" });
				const content = item.querySelector(".ju-notif-swipe-content");
				const r = content.getBoundingClientRect();
				const fire = (type, x) => content.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerType: "touch", pointerId: 7, clientX: x, clientY: r.top + 10 }));
				fire("pointerdown", r.right - 20);
				fire("pointerup", r.right - 90);
			});
			await waitForVisualStability(page);
			const swiped = await snapshotRows(page);
			const revealedRow = await page.evaluate(() => document.querySelector("#np-list > .ju-notif-item:nth-child(10)").classList.contains("is-dismiss-revealed"));
			if (!revealedRow) findings.push(...tag([{ type: "swipe-did-not-reveal-delete", row: 9, detail: "no is-dismiss-revealed" }], `${theme}/swipe`));
			findings.push(...tag(geometryFindings(swiped, narrow, true).filter((f) => f.row === 9 || f.row === -1), `${theme}/swipe`));
			await page.reload({ waitUntil: "load" });
			await setTheme(page, theme);
	}
	const shots = process.env.JUSTUP_LAYOUT_SHOTS;
	if (shots) {
		mkdirSync(shots, { recursive: true });
		for (const theme of THEMES) {
			await setTheme(page, theme);
			await page.mouse.move(0, 0);
			await page.locator("#np-list > .ju-notif-item").nth(0).locator(":scope > .ju-notif-swipe-content").hover({ position: { x: 24, y: 8 } });
			await waitForVisualStability(page);
			await page.screenshot({ path: path.join(shots, `${browserName}-notifications-${theme}-${viewport.name.replace("@", "_")}.png`) });
		}
	}
	for (const error of errors) findings.push({ browser: browserName, vp: viewport.name, variant: "js", type: "page-error", row: -1, detail: error.slice(0, 160) });
	return findings;
}

// Behaviour that must survive the layout change: native links, read state and keyboard delete.

async function auditNotificationBehaviour(page, app, browserName) {
	await page.setViewportSize({ width: 1280, height: 720 });
	await page.goto(`${app.base}/ui/notifications?category=all`, { waitUntil: "load" });
		const rows = page.locator("#np-list > .ju-notif-item");
		// Report no longer available: plain text, no link, no download attribute.
		const gone = rows.nth(0);
		assert.equal(await gone.locator("a").count(), 0, `${browserName}: an unavailable report must not render a link`);
		assert.match(await gone.locator(".ju-notif-unavailable").innerText(), /Report no longer available/);
		// Available report: a native attachment link with the authorised download URL.
		const link = rows.nth(3).locator("a.ju-notif-swipe-content");
		assert.match(await link.getAttribute("href"), /^\/ui\/reports\/jobs\/layout-job-3\/download$/);
		assert.notEqual(await link.getAttribute("download"), null, `${browserName}: download attribute`);
		// Mark as read: clicking an unread static row clears its unread marker.
		assert.ok(await gone.evaluate((el) => el.classList.contains("is-unread")));
		await gone.locator(":scope > .ju-notif-swipe-content").click({ position: { x: 24, y: 8 } });
		await page.waitForFunction(() => !document.querySelector("#np-list > .ju-notif-item:nth-child(1)").classList.contains("is-unread"));
		assert.equal(await gone.locator(".ju-unread-dot").count(), 0);
		// Keyboard: the delete button is reachable by Tab, reveals itself and is operable.
		const second = rows.nth(1);
		await second.locator(":scope > .ju-notif-swipe-content").focus();
		await page.keyboard.press("Tab");
		const button = second.locator(".ju-notif-dismiss-button");
		assert.ok(await button.evaluate((el) => el === document.activeElement), `${browserName}: Tab reaches the delete button`);
		assert.ok(await second.evaluate((el) => el.classList.contains("is-dismiss-revealed")), `${browserName}: focus reveals the delete action`);
		// Same property before and after the reload: innerText vs textContent would never be equal.
		const title = (await second.locator(".ju-notif-title-text").textContent()).trim();
		assert.ok(title, `${browserName}: the dismissed row has a title`);
		assert.match(await button.getAttribute("aria-label"), /^Delete notification: /);
		const dismissal = page.waitForResponse((response) =>
			response.url().includes("/ui/notifications/")
				&& response.url().endsWith("/dismiss")
				&& response.request().method() === "POST", { timeout: 10_000 });
		const reload = page.waitForNavigation({ waitUntil: "load" });
		await page.keyboard.press("Enter");
		assert.equal((await dismissal).status(), 200, `${browserName}: keyboard dismissal response`);
		await page.waitForSelector("#np-list > .ju-notif-item");
		await reload;
		const titles = (await rows.locator(".ju-notif-title-text").allTextContents()).map((text) => text.trim());
		assert.ok(!titles.includes(title), `${browserName}: keyboard dismissal must persist after reload`);
}

export {
	auditNotificationBehaviour,
	auditNotificationRows,
	geometryFindings,
	NOTIF_GEOMETRY_TOL_PX,
	ROWS as NOTIFICATION_ROWS,
	seedNotifications,
	snapshotRows,
	stateFindings,
	VIEWPORTS as NOTIFICATION_VIEWPORTS,
};
