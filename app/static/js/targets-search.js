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
	let requestedScope = null;

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
		memory.take().forEach(function (entry) {
			const collapse = container.querySelector('#' + CSS.escape(entry[0]));
			if (collapse) setOpen(collapse, entry[1]);
		});
		container.querySelectorAll('.accordion-item, tr[data-search]').forEach(function (element) {
			element.hidden = false;
		});
		container.querySelectorAll('.targets-group-count').forEach(function (badge) {
			badge.textContent = badge.dataset.total;
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
				let groupHits = 0;
				rows.forEach(function (row) {
					const hit = filter.matches(row.dataset.search, needle);
					row.hidden = !hit;
					if (hit) groupHits += 1;
				});
				rowTotal += rows.length;
				hits += groupHits;
				item.hidden = groupHits === 0;
				const collapse = collapseOf(item);
				if (collapse && groupHits > 0) {
					memory.remember(collapse.id, collapse.classList.contains('show'));
					setOpen(collapse, true);
				}
				const badge = item.querySelector('.targets-group-count');
				if (badge) badge.textContent = filter.countLabel(groupHits, rows.length);
			});
		}
		const noMatch = searching && rowTotal > 0 && hits === 0;
		if (emptyState) emptyState.hidden = !noMatch;
		if (termLabel) termLabel.textContent = noMatch ? '"' + input.value.trim() + '"' : '';
		if (status) status.textContent = searching && rowTotal > 0 ? filter.statusMessage(hits) : '';
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
			if (event.target.id) memory.override(event.target.id, name === 'shown.bs.collapse');
		});
	});

	// Polling and paging replace the fragment: the new DOM carries the stored group state, so re-evaluate it.
	document.body.addEventListener('htmx:afterSwap', function (event) {
		if (event.target !== container) return;
		requestedScope = null;
		memory.clear();
		render();
	});

	if (input.value !== '') render();
})();
