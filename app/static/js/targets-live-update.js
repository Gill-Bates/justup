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
		if (!uptime) return cell.innerHTML.trim();
		return uptime.firstChild ? uptime.firstChild.textContent.trim() + '|' + Array.from(uptime.classList).join(' ') : '';
	}

	function patchCell(cell, nextCell) {
		if (signature(cell) === signature(nextCell)) return;
		// A cell that still waits for its own data (uptime placeholder) must not replace a loaded value.
		if (nextCell.querySelector('[hx-get]')) return;
		cell.replaceChildren(...Array.from(nextCell.childNodes).map(function (node) { return document.importNode(node, true); }));
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
