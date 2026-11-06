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
	const queueBody = content.querySelector('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
	if (queueBody && visible(queueBody) && queueBody.scrollHeight > queueBody.clientHeight + TOL) {
		issue("delivery-queue-ghost-scroll", queueBody, { scrollHeight: queueBody.scrollHeight, clientHeight: queueBody.clientHeight });
	}
	out.info = {
		viewport: [vw, vh],
		docScroll: [doc.scrollWidth, doc.scrollHeight],
		content: r2(cr),
		main: r2(main.getBoundingClientRect()),
		elements: all.length,
		queueScroll: queueBody && visible(queueBody) ? [queueBody.scrollHeight, queueBody.clientHeight] : null,
	};
	if (shell && topbar && footer) {
		const sr = shell.getBoundingClientRect();
		const tr = topbar.getBoundingClientRect();
		const fr = footer.getBoundingClientRect();
		const mr = main.getBoundingClientRect();
		const mainOverflow = getComputedStyle(main).overflowY;
		const mainExcess = main.scrollHeight - main.clientHeight;
		// Bottom padding of shell or content column (safe-area inset) lies outside the footer box.
		const safeBottom = [shell, content].reduce((sum, el) => sum + (parseFloat(getComputedStyle(el).paddingBottom) || 0), 0);
		out.info.mainScroll = [main.scrollTop, main.scrollHeight, main.clientHeight, mainOverflow];
		if (sr.top < -TOL || sr.bottom > vh + TOL || sr.height > vh + TOL) issue("shell-viewport-overflow", shell, { shell: r2(sr), vh });
		if (doc.scrollHeight > doc.clientHeight + TOL || Math.abs(window.scrollY) > TOL) issue("document-scrolls", doc, { scrollHeight: doc.scrollHeight, clientHeight: doc.clientHeight, scrollY: window.scrollY });
		if (tr.top < -TOL || tr.bottom > vh + TOL) issue("topbar-outside-viewport", topbar, { rect: r2(tr), vh });
		if (fr.top < -TOL || fr.bottom > vh + TOL) issue("footer-outside-viewport", footer, { rect: r2(fr), vh });
		else if (fr.bottom < vh - safeBottom - TOL) issue("footer-above-viewport-bottom", footer, { rect: r2(fr), vh, safeBottom });
		if (mainExcess > TOL && (!(mainOverflow === "auto" || mainOverflow === "scroll") || cfg.scrollPos === "bottom" && main.scrollTop < mainExcess - TOL)) {
			issue("main-not-scrollable", main, { scrollTop: main.scrollTop, scrollHeight: main.scrollHeight, clientHeight: main.clientHeight, overflowY: mainOverflow });
		}
		// Form actions (shared `.ui-form-actions` bars with Save and Cancel; any other visible submit
		// control is checked as well) must be reachable: a persistent (sticky/fixed) one fully visible
		// between main and footer at every scroll position, a scrolling one at the scroll end.
		const barSel = ".ui-form-actions";
		const bars = Array.from(main.querySelectorAll(barSel)).filter(visible);
		const loose = Array.from(main.querySelectorAll("form button[type=submit], form input[type=submit]")).filter((el) => visible(el) && !el.closest(barSel));
		const controls = bars.flatMap((bar) => Array.from(bar.querySelectorAll("a[href], button, input:not([type=hidden]), select, textarea")).filter(visible));
		const isPersistent = (el) => {
			for (let p = el; p && p !== main; p = p.parentElement) if (/^(sticky|fixed)$/.test(getComputedStyle(p).position)) return true;
			return false;
		};
		for (const el of [...bars, ...controls, ...loose]) {
			const ar = el.getBoundingClientRect();
			const persistent = isPersistent(el);
			const outside = ar.bottom > mr.bottom + TOL || ar.bottom > fr.top + TOL;
			if (persistent ? outside || ar.top < mr.top - TOL : cfg.scrollPos === "bottom" && ar.top >= mr.top - TOL && outside) {
				issue("form-actions-unreachable", el, { persistent, rect: r2(ar), main: r2(mr) });
			}
			// A control that is in view must not lie under the footer or any other element.
			const inView = ar.top >= mr.top - TOL && ar.bottom <= mr.bottom + TOL;
			if (el.matches(barSel) || !inView) continue;
			const hit = document.elementFromPoint(ar.left + ar.width / 2, ar.top + ar.height / 2);
			if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) issue("form-actions-covered", el, { rect: r2(ar), coveredBy: hit.className || hit.tagName });
		}
	}
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

	// 3b. A dashboard widget shows no scrollbar except inside a declared overflow area
	//     (cfg.widgetScroll). Compact KPI tiles in particular must fit their content.
	for (const el of all) {
		if (!el.closest(".grid-stack-item") || matchesAny(el, cfg.widgetScroll)) continue;
		const cs = getComputedStyle(el);
		const overflowY = (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight - el.clientHeight > 1;
		const overflowX = (cs.overflowX === "auto" || cs.overflowX === "scroll") && el.scrollWidth - el.clientWidth > 1;
		if (overflowY || overflowX) {
			issue("widget-scrollbar", el, { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
		}
	}

	// 3c. Ghost scroll: a container the user can scroll vertically by a few pixels only (typically `overflow-x: auto`,
	//     which turns `overflow-y` into `auto` too). Real scrollers (main, the document) are measured separately.
	for (const el of all) {
		if (el === main || el.matches("input, textarea, select")) continue;
		const cs = getComputedStyle(el);
		const excess = el.scrollHeight - el.clientHeight;
		if ((cs.overflowY === "auto" || cs.overflowY === "scroll") && excess > 1 && excess <= 8) {
			issue("ghost-scroll-y", el, { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, overflowY: cs.overflowY });
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
				issue(name === "footer" ? "content-covered-by-footer" : "covered-by-" + name, el, { rect: r2(v), box: r2(b) });
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

	const textRects = (el) => {
		const rects = [];
		const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
		while (walker.nextNode()) {
			if (!walker.currentNode.nodeValue.trim()) continue;
			if (!visible(walker.currentNode.parentElement)) continue;
			const range = document.createRange();
			range.selectNodeContents(walker.currentNode);
			for (const rect of range.getClientRects()) {
				let visible = {
					left: rect.left,
					top: rect.top,
					right: rect.right,
					bottom: rect.bottom,
					text: walker.currentNode.nodeValue.trim(),
					source: desc(walker.currentNode.parentElement),
				};
				for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
					const cs = getComputedStyle(p);
					if (cs.position === "fixed") break;
					if (!clipsX(cs) && !clipsY(cs)) continue;
					const pr = p.getBoundingClientRect();
					if (clipsX(cs)) { visible.left = Math.max(visible.left, pr.left); visible.right = Math.min(visible.right, pr.right); }
					if (clipsY(cs)) { visible.top = Math.max(visible.top, pr.top); visible.bottom = Math.min(visible.bottom, pr.bottom); }
				}
				if (visible.right - visible.left > 1 && visible.bottom - visible.top > 1) rects.push(visible);
			}
		}
		return rects;
	};
	const compareText = (elements, group) => {
		const measured = elements.filter(visible).map((el) => ({ el, rects: textRects(el) }));
		for (let i = 0; i < measured.length; i++) {
			for (let j = i + 1; j < measured.length; j++) {
				for (const a of measured[i].rects) {
					for (const b of measured[j].rects) {
						if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > TOL && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > TOL) {
							issue("text-overlap", measured[i].el, {
								group,
								with: desc(measured[j].el),
								text: a.text,
								source: a.source,
								withText: b.text,
								withSource: b.source,
								rect: r2(a),
								withRect: r2(b),
							});
							j = measured.length;
							break;
						}
					}
					if (j === measured.length) break;
				}
			}
		}
	};
	for (const pair of content.querySelectorAll(".target-kpi-pair")) {
		compareText(Array.from(pair.querySelectorAll(".target-kpi-label, .target-kpi-value")), "target-kpi-pair");
	}
	for (const toolbar of content.querySelectorAll(".target-toolbar")) {
		compareText(Array.from(toolbar.querySelectorAll(":scope > .target-toolbar-group")), "target-toolbar");
		if (!visible(toolbar)) continue;
		// Information architecture: the controls stay between the KPI cards and the charts.
		const kpi = document.querySelector("#target-info-cards > .target-kpi-row");
		const chart = document.querySelector("#target-dashboard-pane .target-combined-card");
		const box = toolbar.getBoundingClientRect();
		if (kpi && chart && visible(kpi) && visible(chart)
			&& (kpi.getBoundingClientRect().bottom > box.top + TOL || box.bottom > chart.getBoundingClientRect().top + TOL)) {
			issue("target-toolbar-order", toolbar, { toolbar: r2(box), kpi: r2(kpi.getBoundingClientRect()), chart: r2(chart.getBoundingClientRect()) });
		}
		// Controls that share a row must share a centre line, whatever their own height.
		const controls = Array.from(toolbar.querySelectorAll(
			"#quick-range, .target-custom-range > summary, #reset-zoom-btn",
		)).filter(visible).map((el) => ({ el, rect: el.getBoundingClientRect() }));
		for (let i = 0; i < controls.length; i++) {
			for (let j = i + 1; j < controls.length; j++) {
				const a = controls[i].rect;
				const b = controls[j].rect;
				const sameRow = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > Math.min(a.height, b.height) / 2;
				if (sameRow && Math.abs((a.top + a.bottom) / 2 - (b.top + b.bottom) / 2) > 2) {
					issue("target-toolbar-misaligned", controls[i].el, { with: desc(controls[j].el), rect: r2(a), withRect: r2(b) });
				}
			}
		}
	}
	for (const heading of content.querySelectorAll(".target-panel-heading")) {
		compareText(Array.from(heading.querySelectorAll(":scope > .target-panel-title, :scope > .target-panel-sub")), "target-panel-heading");
	}
	const dashboardPane = document.getElementById("target-dashboard-pane");
	const kpiRow = document.querySelector("#target-info-cards > .target-kpi-row");
	if (dashboardPane && kpiRow && visible(kpiRow)) {
		const paneRect = dashboardPane.getBoundingClientRect();
		const rowRect = kpiRow.getBoundingClientRect();
		if (rowRect.left > paneRect.left + TOL || rowRect.right < paneRect.right - TOL) {
			issue("dashboard-kpi-row-does-not-fill-grid", kpiRow, { pane: r2(paneRect), row: r2(rowRect) });
		}
	}
	for (const legend of content.querySelectorAll(".combined-legend")) {
		compareText(Array.from(legend.querySelectorAll(".combined-legend-btn")), "combined-legend");
	}
	for (const item of document.querySelectorAll(".ju-notif-swipe-item.is-dismiss-revealed")) {
		const swipe = item.querySelector(":scope > .ju-notif-swipe-content");
		const action = item.querySelector(":scope > .ju-notif-dismiss-action");
		if (!visible(item) || !swipe || !action || !visible(action)) continue;
		const swipeRect = swipe.getBoundingClientRect();
		const actionRect = action.getBoundingClientRect();
		if (swipeRect.right > actionRect.left + TOL) {
			issue("notification-delete-overlaps-content", item, { content: r2(swipeRect), action: r2(actionRect) });
		}
	}

	const combined = document.getElementById("combined-card");
	if (combined) {
		const card = combined.getBoundingClientRect();
		for (const selector of [".combined-legend", ".combined-donut-legend", ".target-combined-plot", "#combined-status-note", "#combined-summary"]) {
			for (const el of combined.querySelectorAll(selector)) {
				if (!visible(el)) continue;
				const r = el.getBoundingClientRect();
				if (r.top < card.top - TOL || r.bottom > card.bottom + TOL) {
					issue("dashboard-content-outside-card", el, { card: r2(card), rect: r2(r) });
				}
			}
		}
		const footerParts = Array.from(combined.querySelectorAll("#combined-status-note, #combined-summary"));
		compareText(footerParts, "combined-footer");
	}

	for (const selector of [".target-combined-plot", ".target-availability-plot"]) {
		for (const plot of content.querySelectorAll(selector)) {
			if (!visible(plot)) continue;
			const r = plot.getBoundingClientRect();
			if (r.width < (cfg.minPlotWidth || 120) || r.height < (cfg.minPlotHeight || 96)) {
				issue("chart-plot-too-small", plot, { rect: r2(r), min: [cfg.minPlotWidth || 120, cfg.minPlotHeight || 96] });
			}
			const body = plot.closest(".card")?.querySelector(":scope > .card-body");
			if (body) {
				const bodyRect = body.getBoundingClientRect();
				if (r.top < bodyRect.top - TOL || r.bottom > bodyRect.bottom + TOL) {
					issue("chart-plot-outside-card", plot, { body: r2(bodyRect), plot: r2(r) });
				}
			}
			const canvas = plot.querySelector("canvas");
			const chart = canvas && window.Chart?.getChart(canvas);
			const legend = chart?.legend;
			const area = chart?.chartArea;
			if (!legend || legend.options?.display === false || !area) continue;
			const width = chart.width || canvas.clientWidth;
			const height = chart.height || canvas.clientHeight;
			if (legend.left < -TOL || legend.top < -TOL || legend.right > width + TOL || legend.bottom > height + TOL) {
				issue("chart-legend-outside-canvas", canvas, { legend: [legend.left, legend.top, legend.right, legend.bottom], canvas: [width, height] });
			}
			if (Math.min(legend.right, area.right) - Math.max(legend.left, area.left) > TOL &&
				Math.min(legend.bottom, area.bottom) - Math.max(legend.top, area.top) > TOL) {
				issue("chart-legend-overlaps-plot", canvas, { legend: [legend.left, legend.top, legend.right, legend.bottom], chartArea: [area.left, area.top, area.right, area.bottom] });
			}
			const areaWidth = area.width || area.right - area.left;
			const areaHeight = area.height || area.bottom - area.top;
			if (areaWidth < (cfg.minChartAreaWidth || 120) || areaHeight < (cfg.minChartAreaHeight || 40)) {
				issue("chart-area-too-small", canvas, { chartArea: [areaWidth, areaHeight], min: [cfg.minChartAreaWidth || 120, cfg.minChartAreaHeight || 40] });
			}
			const fitDashboard = innerWidth >= 1200 && innerHeight >= 800;
			const expectedPosition = fitDashboard || width >= 768 ? "right" : "bottom";
			if (legend.options.position !== expectedPosition) {
				issue("chart-legend-position", canvas, { expected: expectedPosition, actual: legend.options.position, width, fitDashboard });
			}
		}
	}

	const downtime = document.getElementById("downtime-history-card");
	if (downtime) {
		const empty = downtime.querySelector(".target-empty-state");
		const body = downtime.querySelector(":scope > .card-body");
		if (empty && visible(empty) && body) {
			const cs = getComputedStyle(body);
			if (["auto", "scroll"].includes(cs.overflowY) && body.scrollHeight > body.clientHeight + TOL) {
				issue("downtime-empty-ghost-scroll", body, { scrollHeight: body.scrollHeight, clientHeight: body.clientHeight, overflowY: cs.overflowY });
			}
		}
		const list = downtime.querySelector(".downtime-scroll");
		if (list && list.scrollHeight > list.clientHeight + TOL) {
			const cs = getComputedStyle(list);
			if (!["auto", "scroll"].includes(cs.overflowY)) issue("downtime-list-not-scrollable", list, { overflowY: cs.overflowY });
			if (list.tabIndex < 0 || list.getAttribute("role") !== "region" || !list.getAttribute("aria-label")) {
				issue("downtime-list-not-keyboard-operable", list, { tabIndex: list.tabIndex, role: list.getAttribute("role") || "" });
			}
			if (body && ["auto", "scroll"].includes(getComputedStyle(body).overflowY) && body.scrollHeight > body.clientHeight + TOL) {
				issue("nested-downtime-scroll", body, { inner: desc(list) });
			}
		}
	}

	// 9. Table contract: cells never truncate (no ellipsis, no clipped cell), nothing is hidden, the
	//    card/table mode follows the container width and a scrolling viewport is operable by keyboard.
	const rem = parseFloat(getComputedStyle(doc).fontSize) || 16;
	for (const cell of content.querySelectorAll("td, th")) {
		if (!visible(cell) && getComputedStyle(cell).display !== "none") continue;
		const cs = getComputedStyle(cell);
		const row = cell.closest("tr");
		if (cs.display === "none") {
			if (cell.tagName === "TD" && row && visible(row) && !cell.hidden) issue("table-cell-hidden", cell, { label: cell.dataset.label || "" });
			continue;
		}
		for (const el of [cell, ...cell.querySelectorAll("*")]) {
			if (el.matches(".visually-hidden")) continue;
			const ecs = getComputedStyle(el);
			if (ecs.textOverflow === "ellipsis" && ecs.overflowX !== "visible") issue("table-ellipsis", el, { text: (el.textContent || "").trim().slice(0, 30) });
			else if (ecs.overflowX !== "visible" && el.scrollWidth > el.clientWidth + TOL && el.clientWidth > 0) issue("table-cell-clipped", el, { text: (el.textContent || "").trim().slice(0, 30), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
		}
	}
	for (const vp of content.querySelectorAll(".ju-table-viewport")) {
		if (getComputedStyle(vp).display === "none" || vp.clientWidth === 0) continue;
		const operable = vp.getAttribute("role") === "region" && vp.tabIndex >= 0 && vp.hasAttribute("aria-label");
		if (!operable && vp.scrollWidth > vp.clientWidth + TOL) issue("table-scroll-not-operable", vp, { scrollWidth: vp.scrollWidth, clientWidth: vp.clientWidth });
		if (!operable) issue("table-viewport-unlabelled", vp, {});
		for (const t of vp.querySelectorAll("table.rtable")) {
			const cards = getComputedStyle(t.tBodies[0]?.rows[0] || t).display === "block" || getComputedStyle(t.tBodies[0]?.rows[0] || t).display === "grid";
			const narrow = vp.clientWidth < 40 * rem;
			if (t.tBodies[0]?.rows.length && cards !== narrow) issue("table-mode-mismatch", t, { viewport: vp.clientWidth, cards });
		}
	}
	// 10. Badge groups (DESIGN.md, "Badges"): every `.ju-badges` / `[data-ju-badges]` container is measured. The
	//     rows of badges sit vertically centred in the space the group offers, the label is centred in
	//     each badge box, and no badge overlaps a sibling or leaves (is clipped by) its group.
	const px = (value) => parseFloat(value) || 0;
	for (const group of content.querySelectorAll(".ju-badges, [data-ju-badges]")) {
		if (!visible(group)) continue;
		const badges = [...group.children].filter((el) => el.matches(".badge, [data-ju-badge]") && visible(el));
		if (!badges.length) continue;
		const gcs = getComputedStyle(group);
		const box = group.getBoundingClientRect();
		const rects = badges.map((el) => el.getBoundingClientRect());
		const inner = {
			top: box.top + px(gcs.paddingTop) + px(gcs.borderTopWidth), bottom: box.bottom - px(gcs.paddingBottom) - px(gcs.borderBottomWidth),
			left: box.left + px(gcs.paddingLeft) + px(gcs.borderLeftWidth), right: box.right - px(gcs.paddingRight) - px(gcs.borderRightWidth),
		};
		const rowTop = Math.min(...rects.map((r) => r.top));
		const rowBottom = Math.max(...rects.map((r) => r.bottom));
		const above = rowTop - inner.top;
		const below = inner.bottom - rowBottom;
		if (rects.some((r) => r.left < inner.left - TOL || r.right > inner.right + TOL) || above < -TOL || below < -TOL) {
			issue("badge-clipped-by-group", group, { group: r2(inner), badges: rects.map(r2) });
		} else if (Math.abs(above - below) > TOL) {
			issue("badge-group-not-centered", group, { above: Math.round(above * 10) / 10, below: Math.round(below * 10) / 10 });
		}
		badges.forEach((el, i) => {
			// Visible label extent; screen-reader-only children are positioned out of the flow and skipped.
			const parts = [...el.childNodes].filter((node) => (node.nodeType === 3 && node.textContent.trim()) || (node.nodeType === 1 && !node.matches(".visually-hidden")))
				.map((node) => { const range = document.createRange(); range.selectNode(node); return range.getBoundingClientRect(); }).filter((r) => r.height);
			const labelTop = Math.min(...parts.map((r) => r.top));
			const labelBottom = Math.max(...parts.map((r) => r.bottom));
			if (parts.length && Math.abs((labelTop - rects[i].top) - (rects[i].bottom - labelBottom)) > TOL) {
				issue("badge-text-not-centered", el, { top: Math.round(labelTop - rects[i].top), bottom: Math.round(rects[i].bottom - labelBottom) });
			}
			if (el.scrollWidth > el.clientWidth + TOL) issue("badge-text-clipped", el, { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth });
			for (let j = i + 1; j < badges.length; j++) {
				const overlapX = Math.min(rects[i].right, rects[j].right) - Math.max(rects[i].left, rects[j].left);
				const overlapY = Math.min(rects[i].bottom, rects[j].bottom) - Math.max(rects[i].top, rects[j].top);
				if (overlapX > 1 && overlapY > 1) issue("badge-overlap", el, { other: desc(badges[j]) });
			}
		});
	}
	return out;
}
