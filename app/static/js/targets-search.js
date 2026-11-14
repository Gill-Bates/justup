//
// app/static/js/targets-search.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Filter-as-you-type search of the Targets page (DESIGN.md, "Filter-as-you-type search").
// Filters the rendered rows client-side; groups with hits open for the duration of the search.
(function () {
	'use strict';
	const filter = window.JuListFilter;
	const input = document.getElementById('targets-search');
	const container = document.getElementById('targets-table');
	if (!filter || !input || !container) return;

	const clearButton = document.getElementById('targets-search-clear');
	const resetButton = document.getElementById('targets-search-reset');
	const status = document.getElementById('targets-search-status');
	const emptyState = document.getElementById('targets-search-empty');
	const termLabel = document.getElementById('targets-search-term');
	const scopeField = document.getElementById('targets-scope-state');
	const memory = filter.createExpansionMemory();
	// Groups the user closed by hand during the search; a live patch or the next keystroke must not reopen them.
	const closedByHand = new Set();
	let requestedScope = null;

	// Writing identical text still replaces the text node (a mutation, and a repeated announcement).
	function setText(element, text) {
		if (element.textContent !== text) element.textContent = text;
	}

	function collapseOf(item) {
		return item.querySelector('.accordion-collapse');
	}

	// Class changes only: Bootstrap fires no collapse events, so the stored manual state stays untouched.
	function setOpen(collapse, open) {
		const button = collapse.id ? container.querySelector('[data-bs-target="#' + CSS.escape(collapse.id) + '"]') : null;
		collapse.classList.toggle('show', open);
		if (button) {
			button.classList.toggle('collapsed', !open);
			button.setAttribute('aria-expanded', String(open));
		}
	}

	function restoreGroups() {
		closedByHand.clear();
		memory.take().forEach(function (entry) {
			const collapse = container.querySelector('#' + CSS.escape(entry[0]));
			if (collapse) setOpen(collapse, entry[1]);
		});
		container.querySelectorAll('.accordion-item, tr[data-search]').forEach(function (element) {
			element.hidden = false;
		});
		container.querySelectorAll('.targets-group-count').forEach(function (badge) {
			setText(badge, badge.dataset.total);
		});
	}

	// The search needs every target in the DOM; with several pages the fragment is requested once as one page.
	function syncScope(searching) {
		const list = container.querySelector('#targets-accordion');
		if (!list || !scopeField) return;
		const paged = Number(list.dataset.totalPages) > 1;
		const loadedAll = list.dataset.scope === 'all';
		const wanted = searching && paged ? 'all' : (!searching && loadedAll ? 'page' : null);
		if (!wanted || wanted === requestedScope) return;
		requestedScope = wanted;
		scopeField.value = wanted === 'all' ? '1' : '0';
		window.htmx.trigger(container, 'refresh');
	}

	function render() {
		const needle = filter.normalizeQuery(input.value);
		const searching = needle !== '';
		if (clearButton) clearButton.hidden = input.value === '';
		let rowTotal = 0;
		let hits = 0;
		if (!searching) {
			restoreGroups();
		} else {
			container.querySelectorAll('.accordion-item').forEach(function (item) {
				const rows = Array.from(item.querySelectorAll('tr[data-search]'));
				const groupHits = filter.filterRows(rows, needle);
				rowTotal += rows.length;
				hits += groupHits;
				item.hidden = groupHits === 0;
				const collapse = collapseOf(item);
				// A group that Bootstrap is closing right now is the user's choice, not a state to override.
				if (collapse && groupHits > 0 && !collapse.classList.contains('collapsing') && !closedByHand.has(collapse.id)) {
					memory.remember(collapse.id, collapse.classList.contains('show'));
					setOpen(collapse, true);
				}
				const badge = item.querySelector('.targets-group-count');
				if (badge) setText(badge, filter.countLabel(groupHits, rows.length));
			});
		}
		const noMatch = searching && rowTotal > 0 && hits === 0;
		if (emptyState) emptyState.hidden = !noMatch;
		if (termLabel) setText(termLabel, noMatch ? '"' + input.value.trim() + '"' : '');
		if (status) setText(status, searching && rowTotal > 0 ? filter.statusMessage(hits) : '');
		syncScope(searching);
	}

	function clearSearch() {
		input.value = '';
		render();
		input.focus();
	}

	input.addEventListener('input', render);
	input.addEventListener('keydown', function (event) {
		if (event.key === 'Escape' && input.value !== '') {
			event.preventDefault();
			clearSearch();
		}
	});
	if (clearButton) clearButton.addEventListener('click', clearSearch);
	if (resetButton) resetButton.addEventListener('click', clearSearch);

	// A manual toggle during the search is the state to return to afterwards.
	['shown.bs.collapse', 'hidden.bs.collapse'].forEach(function (name) {
		container.addEventListener(name, function (event) {
			if (!event.target.id) return;
			const shown = name === 'shown.bs.collapse';
			memory.override(event.target.id, shown);
			if (input.value === '') return;
			if (shown) closedByHand.delete(event.target.id);
			else closedByHand.add(event.target.id);
		});
	});

	// Polling and paging replace the fragment: the new DOM carries the stored group state, so re-evaluate it.
	document.body.addEventListener('htmx:afterSwap', function (event) {
		if (event.target !== container) return;
		requestedScope = null;
		memory.clear();
		render();
	});

	// A live update rewrites cells in place: re-apply the query to the changed rows.
	container.addEventListener('targets:patched', render);

	if (input.value !== '') render();
})();
