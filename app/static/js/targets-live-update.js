//
// app/static/js/targets-live-update.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Live refresh of the Targets list without replacing it (DESIGN.md, "Load stability"). The poll answer
// is compared with the page: when groups and rows are the same, only changed cells are rewritten, so
// rows, self-updating status icons, focus, open groups and the search state stay untouched. Any
// structural change (target added or removed, other group, page or scope) falls back to the normal swap.
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

	function patchCell(cell, nextCell) {
		if (signature(cell) === signature(nextCell)) return;
		// A cell that still waits for its own data (uptime placeholder) must not replace a loaded value.
		if (nextCell.querySelector('[hx-get]')) return;
		const focusedToggle = cell.querySelector('.targets-check-toggle') === document.activeElement;
		cell.replaceChildren(...Array.from(nextCell.childNodes).map(function (node) { return document.importNode(node, true); }));
		if (cell.dataset.label === 'Checks') {
			applyCheckState(cell.parentElement);
			if (focusedToggle) (cell.querySelector('.targets-check-toggle') || cell.parentElement.querySelector('td[data-label="Name"] a'))?.focus();
		}
		window.htmx.process(cell);
	}

	function patchRow(row, nextRow) {
		if (row.dataset.search !== nextRow.dataset.search) row.dataset.search = nextRow.dataset.search;
		const cells = Array.from(row.children);
		const nextCells = Array.from(nextRow.children);
		cells.forEach(function (cell, index) {
			if (cell.dataset.label === SELF_UPDATING_CELL || !nextCells[index]) return;
			patchCell(cell, nextCells[index]);
		});
	}

	function patch(current, next) {
		const nextGroups = groups(next);
		groups(current).forEach(function (item, index) {
			const badge = item.querySelector('.targets-group-count');
			const nextBadge = nextGroups[index].querySelector('.targets-group-count');
			// While a search is active the badge shows "hits / total"; only the total is the server's.
			if (badge && nextBadge && badge.dataset.total !== nextBadge.dataset.total) {
				badge.dataset.total = nextBadge.dataset.total;
				badge.textContent = nextBadge.textContent;
			}
			const nextRows = rows(nextGroups[index]);
			rows(item).forEach(function (row, rowIndex) { patchRow(row, nextRows[rowIndex]); });
		});
	}

	// htmx's throttle flag is per element and also swallows the unthrottled 'refresh' for the window (user
	// actions, search), so the live events are throttled here and reach htmx only as 'refresh'.
	const REFRESH_THROTTLE_MS = 2000;
	let throttled = false;
	function refreshThrottled() {
		if (throttled) return;
		throttled = true;
		window.setTimeout(function () { throttled = false; }, REFRESH_THROTTLE_MS);
		window.htmx.trigger(container, 'refresh');
	}
	['justup-target-list', 'justup-live-sync'].forEach(function (name) {
		document.body.addEventListener(name, refreshThrottled);
	});
	document.body.addEventListener('htmx:beforeSwap', function (event) {
		const detail = event.detail;
		if (!detail || detail.elt !== container || event.target !== container) return;
		if (!detail.xhr || detail.xhr.status !== 200 || typeof detail.serverResponse !== 'string') return;
		const current = container.querySelector('#targets-accordion');
		if (!current) return;
		const next = parse(detail.serverResponse);
		const nextList = next.querySelector('#targets-accordion');
		if (!nextList || !sameStructure(current, nextList)) return;
		detail.shouldSwap = false;
		patch(current, nextList);
		// The page carrier normally refreshes out-of-band with the swap.
		const carrier = document.getElementById('targets-page-state');
		const nextCarrier = next.querySelector('#targets-page-state');
		if (carrier && nextCarrier) carrier.value = nextCarrier.value;
		container.dispatchEvent(new CustomEvent('targets:patched'));
	});
})();
