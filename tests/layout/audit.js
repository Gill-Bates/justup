//
// tests/layout/audit.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Layout audit that runs inside the page (Playwright page.evaluate). It measures instead of
// asserting on CSS text: every finding names the DOM node, its rect and the clip/scroll ancestor.
// Contract under test: docs/development.md, "Layout contract".
// Must stay self-contained: the function is serialised and executed in the browser.
(cfg) => {
	const TOL = 2;
	const vw = document.documentElement.clientWidth;
	const vh = window.innerHeight;
	const shell = document.getElementById("app-shell");
	const content = document.querySelector(".ju-content") || document.body;
	const main = document.getElementById("main-content") || content;
	const footer = document.querySelector(".ju-footer");
	const topbar = document.querySelector(".ju-topbar");
	const sidebar = document.querySelector(".ju-sidebar");
	const out = { issues: [], scrollRegions: [], info: {} };

	const desc = (el) => {
		if (!el || el === document) return "document";
		const cls = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 3).join(".") : "";
		return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (cls ? "." + cls : "");
	};
	const r2 = (r) => [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)];
	const issue = (type, el, extra) => out.issues.push({ type, node: desc(el), ...extra });
	const matchesAny = (el, list) => (list || []).some((s) => el.closest(s));

	const visible = (el) => {
		const cs = getComputedStyle(el);
		if (cs.display === "none" || cs.visibility === "hidden" || cs.position === "fixed") return false;
		const r = el.getBoundingClientRect();
		if (r.width < 2 || r.height < 2) return false;
		if (el.closest(".visually-hidden, .visually-hidden-focusable, [hidden], .modal:not(.show), .tooltip, .popover, .ju-hp")) return false;
		// Content of a closed <details> is laid out but neither painted nor reachable.
		const closed = el.closest("details:not([open])");
		if (closed && !el.closest("summary")) return false;
		return true;
	};
	const clipsX = (cs) => cs.overflowX !== "visible";
	const clipsY = (cs) => cs.overflowY !== "visible";

	// Rect of el after clipping by every ancestor that clips (hidden, clip, auto, scroll).
	const visibleRect = (el) => {
		let { left, top, right, bottom } = el.getBoundingClientRect();
		for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
			const cs = getComputedStyle(p);
			if (cs.position === "fixed") break;
			if (!clipsX(cs) && !clipsY(cs)) continue;
			const pr = p.getBoundingClientRect();
			if (clipsX(cs)) { left = Math.max(left, pr.left); right = Math.min(right, pr.right); }
			if (clipsY(cs)) { top = Math.max(top, pr.top); bottom = Math.min(bottom, pr.bottom); }
		}
		return { left, top, right, bottom };
	};

	const all = Array.from(content.querySelectorAll("*")).filter((el) => !el.closest(".ju-sidebar") && visible(el));
	const cr = content.getBoundingClientRect();
	const doc = document.documentElement;
	out.info = {
		viewport: [vw, vh],
		docScroll: [doc.scrollWidth, doc.scrollHeight],
		content: r2(cr),
		main: r2(main.getBoundingClientRect()),
		fit: shell ? getComputedStyle(shell).getPropertyValue("--ju-fit-height").trim() : "",
		elements: all.length,
	};

	// 1. Document-level horizontal overflow.
	if (doc.scrollWidth > doc.clientWidth + TOL) {
		issue("doc-overflow-x", doc, { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth });
	}

	// 2. Content column must start right of a fixed desktop sidebar and fit the viewport.
	if (sidebar && getComputedStyle(sidebar).visibility !== "hidden") {
		const sr = sidebar.getBoundingClientRect();
		if (sr.right > 1 && cr.left < sr.right - TOL) issue("content-under-sidebar", content, { sidebar: r2(sr), content: r2(cr) });
	}
	if (cr.right > vw + TOL) issue("content-exceeds-viewport", content, { content: r2(cr), vw });

	// 3. Clipping containers: a clip/hidden container whose content is wider than its box hides
	//    a size error; scroll containers are listed and must be deliberate (allow-list).
	for (const el of [content, ...all]) {
		// Text fields scroll or clip a long value by design; only fixed-format fields can be squeezed.
		if (el.matches("input, textarea, select") && !el.matches("input[type=number], input[type=date], input[type=time], input[type=datetime-local]")) continue;
		const cs = getComputedStyle(el);
		const sx = el.scrollWidth - el.clientWidth;
		if (clipsX(cs) && sx > TOL && el.clientWidth > 0) {
			const scrolls = cs.overflowX === "auto" || cs.overflowX === "scroll";
			if (scrolls) {
				out.scrollRegions.push({ node: desc(el), axis: "x", scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
				if (!matchesAny(el, cfg.scrollX)) issue("unexpected-scroll-x", el, { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
			} else if (!(cs.textOverflow === "ellipsis" || cs.textOverflow === "clip" && cs.whiteSpace === "nowrap") && !matchesAny(el, cfg.clipX)) {
				issue("clipped-x", el, { overflowX: cs.overflowX, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
			}
		}
	}

	// 4. Elements wider than the layout box that owns them (content column, card, modal) and that
	//    no scroll container accounts for. A clipping box hides such a node, so it is measured here.
	const BOX = ".card, .modal-content, .ui-page, main";
	for (const el of all) {
		if (matchesAny(el, cfg.scrollX)) continue;
		const box = el.parentElement && el.parentElement.closest(BOX);
		if (!box) continue;
		const r = el.getBoundingClientRect();
		const b = box.getBoundingClientRect();
		const scrollParent = (() => {
			for (let p = el.parentElement; p && p !== box; p = p.parentElement) {
				const cs = getComputedStyle(p);
				if (cs.overflowX === "auto" || cs.overflowX === "scroll") return p;
			}
			return null;
		})();
		if (scrollParent) continue;
		if (r.right > b.right + TOL || r.left < b.left - TOL || r.right > cr.right + TOL) {
			issue("exceeds-container-x", el, { rect: r2(r), box: desc(box), boxRect: r2(b) });
		}
	}

	// 5. Controls must be reachable inside every clipping ancestor: a hidden/clip ancestor must
	//    contain them; a scrolling ancestor makes them reachable, so its own box is the limit.
	const controls = Array.from(content.querySelectorAll("a[href], button, input:not([type=hidden]), select, textarea, summary, [tabindex]:not([tabindex='-1'])")).filter((el) => !el.closest(".ju-sidebar") && visible(el));
	for (const el of controls) {
		if (el.closest(".ju-topbar, .ju-footer")) continue;
		const r = el.getBoundingClientRect();
		let { left, right, top, bottom } = r;
		for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
			const cs = getComputedStyle(p);
			if (cs.position === "fixed") break;
			const pr = p.getBoundingClientRect();
			const hid = (o) => o === "hidden" || o === "clip";
			const scr = (o) => o === "auto" || o === "scroll";
			if (hid(cs.overflowX) && (right > pr.right + TOL || left < pr.left - TOL)) { issue("control-clipped", el, { by: desc(p), rect: r2(r), clip: r2(pr) }); break; }
			if (hid(cs.overflowY) && (bottom > pr.bottom + TOL || top < pr.top - TOL)) { issue("control-clipped", el, { by: desc(p), rect: r2(r), clip: r2(pr) }); break; }
			if (scr(cs.overflowX)) { left = Math.max(left, pr.left); right = Math.min(right, pr.right); }
			if (scr(cs.overflowY)) { top = Math.max(top, pr.top); bottom = Math.min(bottom, pr.bottom); }
		}
	}

	// 6. Nothing may be painted under the footer, a fixed sidebar or (at the top) the topbar.
	const cover = [["footer", footer], ["sidebar", sidebar && getComputedStyle(sidebar).visibility !== "hidden" ? sidebar : null]];
	if (cfg.scrollPos === "top") cover.push(["topbar", topbar]);
	for (const [name, box] of cover) {
		if (!box) continue;
		const b = box.getBoundingClientRect();
		for (const el of all) {
			if (box.contains(el) || el.contains(box) || el.closest(".modal")) continue;
			const v = visibleRect(el);
			const w = Math.min(v.right, b.right) - Math.max(v.left, b.left);
			const h = Math.min(v.bottom, b.bottom) - Math.max(v.top, b.top);
			if (w > TOL && h > TOL && v.right - v.left > TOL && v.bottom - v.top > TOL) {
				issue("covered-by-" + name, el, { rect: r2(v), box: r2(b) });
				break;
			}
		}
	}

	// 7. Controls in the viewport must be hit-testable. Swipe-to-dismiss rows reveal their action
	//    from underneath on hover/swipe; they are exempt (cfg.revealed). (not covered by another component).
	for (const el of controls) {
		const v = visibleRect(el);
		const x = (v.left + v.right) / 2, y = (v.top + v.bottom) / 2;
		if (v.right - v.left < 2 || v.bottom - v.top < 2 || x < 0 || y < 0 || x > vw || y > vh) continue;
		if (matchesAny(el, cfg.revealed)) continue;
		const hit = document.elementFromPoint(x, y);
		if (hit && !(el === hit || el.contains(hit) || hit.contains(el) || (el.labels && Array.from(el.labels).some((l) => l.contains(hit))))) {
			if (cfg.scrollPos !== "top" && hit.closest(".ju-topbar")) continue;
			issue("control-covered", el, { by: desc(hit), at: [Math.round(x), Math.round(y)] });
		}
	}

	// 8. Fit shells must not scroll the document.
	if (out.info.fit && out.info.fit !== "auto" && doc.scrollHeight > vh + TOL) {
		issue("fit-document-scrolls", doc, { scrollHeight: doc.scrollHeight, vh });
	}
	return out;
}
