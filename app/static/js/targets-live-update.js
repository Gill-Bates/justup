//
// app/static/js/targets-live-update.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Live refresh of the Targets list without replacing it (DESIGN.md, "Load stability"). The poll answer
// is compared with the page: when groups and rows are the same, only changed cells are rewritten, so
// rows, self-updating status icons, focus, open groups and the search state stay untouched. Any
// structural change (target added or removed, other group, page or scope) falls back to the normal swap.
// This module also owns the transaction around a swap (DC-LIVE-2, DC-LIVE-4 to DC-LIVE-6): scroll anchor and
// focus survive a structural swap, a superseded response is dropped, and refresh events coalesce.
(function () {
	'use strict';
	const container = document.getElementById('targets-table');
	if (!container || !window.htmx) return;

	// The status icon refreshes itself through its own request; the server answer never carries its state.
	const SELF_UPDATING_CELL = 'Status';
	const expandedChecks = new Set();

	function applyCheckState(row) {
		const cell = row.querySelector('td[data-label="Checks"]');
		const button = cell && cell.querySelector('.targets-check-toggle');
		if (!cell) return;
		if (!button) expandedChecks.delete(row.dataset.targetId);
		const expanded = !!button && expandedChecks.has(row.dataset.targetId);
		cell.classList.toggle('targets-checks-expanded', expanded);
		if (button) {
			button.setAttribute('aria-expanded', String(expanded));
			button.textContent = expanded ? 'Show fewer' : '+' + button.dataset.more + ' more';
		}
	}

	function restoreCheckStates() {
		const visibleIds = new Set();
		container.querySelectorAll('tr[data-target-id]').forEach(function (row) {
			visibleIds.add(row.dataset.targetId);
			applyCheckState(row);
		});
		for (const id of expandedChecks) if (!visibleIds.has(id)) expandedChecks.delete(id);
	}

	container.addEventListener('click', function (event) {
		const button = event.target.closest('.targets-check-toggle');
		if (!button || !container.contains(button)) return;
		const row = button.closest('tr[data-target-id]');
		if (!row) return;
		if (expandedChecks.has(row.dataset.targetId)) expandedChecks.delete(row.dataset.targetId);
		else expandedChecks.add(row.dataset.targetId);
		applyCheckState(row);
	});

	container.addEventListener('htmx:afterSwap', function (event) {
		if (event.target === container) restoreCheckStates();
	});

	function parse(html) {
		const template = document.createElement('template');
		template.innerHTML = html;
		return template.content;
	}

	function groups(list) {
		return Array.from(list.querySelectorAll(':scope > .accordion-item'));
	}

	function groupKey(item) {
		const header = item.querySelector('.targets-group-header');
		return header ? header.dataset.groupKey : '';
	}

	function rows(item) {
		return Array.from(item.querySelectorAll('tbody > tr[data-target-id]'));
	}

	// True when both lists have the same groups, in the same order, with the same rows in the same order.
	function sameStructure(current, next) {
		if (current.dataset.totalPages !== next.dataset.totalPages || current.dataset.scope !== next.dataset.scope) return false;
		const a = groups(current);
		const b = groups(next);
		if (a.length !== b.length) return false;
		return a.every(function (item, index) {
			if (groupKey(item) !== groupKey(b[index])) return false;
			const ids = rows(item).map(function (row) { return row.dataset.targetId; });
			const nextIds = rows(b[index]).map(function (row) { return row.dataset.targetId; });
			return ids.length === nextIds.length && ids.every(function (id, i) { return id === nextIds[i]; });
		});
	}

	// The uptime cell comes in two markups (inline in the list, or from its own fragment with a
	// screen-reader cue); only the shown value and its level count as a change.
	function signature(cell) {
		const uptime = cell.querySelector('.uptime-value');
		if (!uptime) {
			const toggle = cell.querySelector('.targets-check-toggle');
			if (!toggle) return cell.innerHTML.trim();
			const copy = cell.cloneNode(true);
			const normalToggle = copy.querySelector('.targets-check-toggle');
			normalToggle.setAttribute('aria-expanded', 'false');
			normalToggle.textContent = '+' + normalToggle.dataset.more + ' more';
			return copy.innerHTML.trim();
		}
		return uptime.firstChild ? uptime.firstChild.textContent.trim() + '|' + Array.from(uptime.classList).join(' ') : '';
	}

	function controlsOf(cell) {
		return cell ? Array.from(cell.querySelectorAll('a[href], button:not([disabled])')) : [];
	}

	// Returns true when the cell was rewritten.
	function patchCell(cell, nextCell) {
		if (signature(cell) === signature(nextCell)) return false;
		// A cell that still waits for its own data (uptime placeholder) must not replace a loaded value.
		if (nextCell.querySelector('[hx-get]')) return false;
		const controls = controlsOf(cell);
		const focusedIndex = controls.indexOf(document.activeElement);
		cell.replaceChildren(...Array.from(nextCell.childNodes).map(function (node) { return document.importNode(node, true); }));
		if (cell.dataset.label === 'Checks') applyCheckState(cell.parentElement);
		window.htmx.process(cell);
		if (focusedIndex >= 0) {
			const successor = controlsOf(cell)[focusedIndex] || controlsOf(cell.parentElement.querySelector('td[data-label="Name"]'))[0];
			if (successor) successor.focus({ preventScroll: true });
		}
		return true;
	}

	function patchRow(row, nextRow) {
		let changed = false;
		if (row.dataset.search !== nextRow.dataset.search) {
			row.dataset.search = nextRow.dataset.search;
			changed = true;
		}
		const cells = Array.from(row.children);
		const nextCells = Array.from(nextRow.children);
		cells.forEach(function (cell, index) {
			if (cell.dataset.label === SELF_UPDATING_CELL || !nextCells[index]) return;
			if (patchCell(cell, nextCells[index])) changed = true;
		});
		return changed;
	}

	// Returns true when at least one node was written; an identical answer writes nothing (DC-LIVE-1).
	function patch(current, next) {
		const nextGroups = groups(next);
		let changed = false;
		groups(current).forEach(function (item, index) {
			const badge = item.querySelector('.targets-group-count');
			const nextBadge = nextGroups[index].querySelector('.targets-group-count');
			// While a search is active the badge shows "hits / total"; only the total is the server's.
			if (badge && nextBadge && badge.dataset.total !== nextBadge.dataset.total) {
				badge.dataset.total = nextBadge.dataset.total;
				badge.textContent = nextBadge.textContent;
				changed = true;
			}
			const nextRows = rows(nextGroups[index]);
			rows(item).forEach(function (row, rowIndex) {
				if (patchRow(row, nextRows[rowIndex])) changed = true;
			});
		});
		return changed;
	}

	const scroller = document.getElementById('main-content');
	const searchField = document.getElementById('targets-search');
	const scopeField = document.getElementById('targets-scope-state');

	function isRendered(element) {
		return !!element && !element.hidden && element.getClientRects().length > 0;
	}

	// The anchor is the first row that reaches into the visible part of the scroll region.
	function captureAnchor() {
		if (!scroller) return null;
		const top = scroller.getBoundingClientRect().top;
		for (const row of container.querySelectorAll('tr[data-target-id]')) {
			const box = row.getBoundingClientRect();
			if (box.height > 0 && box.bottom > top + 1) return { id: row.dataset.targetId, offset: box.top - top };
		}
		return null;
	}

	function rowOf(id) {
		return container.querySelector('tr[data-target-id="' + CSS.escape(id) + '"]');
	}

	function captureFocus() {
		const element = document.activeElement;
		if (!element || element === document.body || !container.contains(element)) return null;
		const row = element.closest('tr[data-target-id]');
		if (row) {
			const cell = element.closest('td');
			return { kind: 'row', element, id: row.dataset.targetId, label: cell ? cell.dataset.label : '', index: controlsOf(cell).indexOf(element) };
		}
		const header = element.closest('.targets-group-header');
		if (header) return { kind: 'group', element, key: header.dataset.groupKey };
		const pager = element.closest('.page-link');
		if (pager) return { kind: 'pager', element, label: pager.getAttribute('aria-label') };
		const region = element.closest('.ju-table-viewport');
		if (region) return { kind: 'region', element, label: region.getAttribute('aria-label') };
		return null;
	}

	function nameLink(row) {
		return row ? row.querySelector('td[data-label="Name"] a') : null;
	}

	// Same control of the same id; else the next row, the previous row, the search field (DC-LIVE-2).
	function focusSuccessor(saved, order) {
		if (saved.kind === 'row') {
			const row = rowOf(saved.id);
			if (isRendered(row)) {
				const cell = Array.from(row.children).find(function (td) { return td.dataset.label === saved.label; });
				return controlsOf(cell)[saved.index] || nameLink(row);
			}
			const at = order.indexOf(saved.id);
			const later = order.slice(at + 1);
			const earlier = order.slice(0, Math.max(at, 0)).reverse();
			for (const id of later.concat(earlier)) {
				const candidate = rowOf(id);
				if (isRendered(candidate) && nameLink(candidate)) return nameLink(candidate);
			}
		} else if (saved.kind === 'group') {
			const header = Array.from(container.querySelectorAll('.targets-group-header')).find(function (button) { return button.dataset.groupKey === saved.key; });
			if (isRendered(header)) return header;
		} else if (saved.kind === 'pager') {
			const buttons = Array.from(container.querySelectorAll('.page-link:not([disabled])'));
			return buttons.find(function (button) { return button.getAttribute('aria-label') === saved.label; }) || buttons[0] || null;
		} else if (saved.kind === 'region') {
			const region = Array.from(container.querySelectorAll('.ju-table-viewport')).find(function (node) { return node.getAttribute('aria-label') === saved.label; });
			if (isRendered(region)) return region;
		}
		return searchField;
	}

	// Scroll, anchor and focus around one structural swap; null when the request is not a live refresh.
	let transaction = null;

	function begin(userAction) {
		const order = Array.from(container.querySelectorAll('tr[data-target-id]')).filter(isRendered).map(function (row) { return row.dataset.targetId; });
		return {
			userAction: userAction,
			top: scroller ? scroller.scrollTop : 0,
			anchor: userAction ? null : captureAnchor(),
			focus: captureFocus(),
			order: order,
		};
	}

	function finish(saved) {
		if (saved.focus && !saved.focus.element.isConnected) {
			const target = focusSuccessor(saved.focus, saved.order);
			if (target) target.focus({ preventScroll: true });
		}
		if (!scroller) return;
		if (saved.userAction) {
			// A page change requested through the pager starts at the top of the list.
			const offset = container.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
			if (offset < 0) scroller.scrollTop += offset;
			return;
		}
		const row = saved.anchor ? rowOf(saved.anchor.id) : null;
		if (row && row.getBoundingClientRect().height > 0) {
			const delta = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - saved.anchor.offset;
			if (Math.abs(delta) >= 1) scroller.scrollTop += delta;
		} else if (scroller.scrollTop !== saved.top) {
			scroller.scrollTop = saved.top;
		}
	}

	// Request bookkeeping for DC-LIVE-4. A pager request is a user action: the newest one wins. A refresh
	// (poll, scope change, action trigger) applies only while the page and scope it asked for are still current.
	let requestSeq = 0;
	let latestUserSeq = 0;
	document.body.addEventListener('htmx:beforeRequest', function (event) {
		const detail = event.detail;
		if (!detail || detail.target !== container || !detail.xhr) return;
		detail.xhr.juSeq = ++requestSeq;
		detail.xhr.juUser = detail.elt !== container;
		if (detail.xhr.juUser) latestUserSeq = detail.xhr.juSeq;
	});

	function isSuperseded(detail) {
		if (detail.xhr.juUser) return detail.xhr.juSeq !== latestUserSeq;
		const path = detail.pathInfo && detail.pathInfo.finalRequestPath;
		if (!path) return false;
		const params = new URL(path, window.location.href).searchParams;
		const page = params.get('page') || '1';
		const all = params.get('all') === '1';
		// The page carrier is replaced out-of-band with every swap, so it is looked up each time.
		const pageField = document.getElementById('targets-page-state');
		return page !== (pageField ? pageField.value : '1') || all !== (!!scopeField && scopeField.value === '1');
	}

	// htmx's throttle flag is per element and also swallows the unthrottled 'refresh' for the window (user
	// actions, search), so the live events are throttled here and reach htmx only as 'refresh'. Events inside
	// the window collapse into one trailing refresh.
	const REFRESH_THROTTLE_MS = 2000;
	let windowOpen = false;
	let trailing = false;
	function refreshNow() {
		windowOpen = true;
		window.setTimeout(function () {
			windowOpen = false;
			if (trailing) {
				trailing = false;
				refreshNow();
			}
		}, REFRESH_THROTTLE_MS);
		window.htmx.trigger(container, 'refresh');
	}
	function refreshThrottled() {
		if (windowOpen) trailing = true;
		else refreshNow();
	}
	['justup-target-list', 'justup-live-sync'].forEach(function (name) {
		document.body.addEventListener(name, refreshThrottled);
	});

	document.body.addEventListener('htmx:beforeSwap', function (event) {
		const detail = event.detail;
		transaction = null;
		if (!detail || detail.target !== container || event.target !== container) return;
		if (!detail.xhr || detail.xhr.status !== 200 || typeof detail.serverResponse !== 'string') return;
		if (isSuperseded(detail)) {
			detail.shouldSwap = false;
			if (!detail.xhr.juUser) window.htmx.trigger(container, 'refresh');
			return;
		}
		// detail.elt of a swap event is the swap target, so the requester is the flag set in beforeRequest.
		const userAction = !!detail.xhr.juUser;
		const current = container.querySelector('#targets-accordion');
		if (!userAction && current) {
			const next = parse(detail.serverResponse);
			const nextList = next.querySelector('#targets-accordion');
			if (nextList && sameStructure(current, nextList)) {
				detail.shouldSwap = false;
				const changed = patch(current, nextList);
				// The page carrier normally refreshes out-of-band with the swap.
				const carrier = document.getElementById('targets-page-state');
				const nextCarrier = next.querySelector('#targets-page-state');
				if (carrier && nextCarrier) carrier.value = nextCarrier.value;
				if (changed) container.dispatchEvent(new CustomEvent('targets:patched'));
				return;
			}
		}
		transaction = begin(userAction);
	});

	// Registered after targets-search.js, so its filter and group state are applied before the scroll is restored.
	document.body.addEventListener('htmx:afterSwap', function (event) {
		if (event.target !== container || !transaction) return;
		const saved = transaction;
		transaction = null;
		finish(saved);
	});
})();
