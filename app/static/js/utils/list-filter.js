//
// app/static/js/utils/list-filter.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Pure helpers for filter-as-you-type lists (no DOM): query normalisation, substring matching,
// result messages and the remembered open/closed state of collapsible groups.
(function (root) {
	'use strict';
	// Longer queries cannot match a real field and only cost time.
	const MAX_QUERY_LENGTH = 100;
	// Lower-case, strip diacritics (NFKD), collapse whitespace and trim, so "Lüttring" finds "luttring".
	function normalize(value) {
		return String(value == null ? '' : value)
			.normalize('NFKD')
			.replace(/\p{M}+/gu, '')
			.toLowerCase()
			.replace(/\s+/g, ' ')
			.trim();
	}
	function normalizeQuery(value) {
		return normalize(String(value == null ? '' : value).slice(0, MAX_QUERY_LENGTH));
	}
	// Plain substring test, never a pattern: "." and "(" are literal characters.
	function matches(haystack, needle) {
		return needle === '' || normalize(haystack).includes(needle);
	}
	// Shows or hides each row by its data-search text and returns the number of hits.
	function filterRows(rows, needle) {
		let hits = 0;
		Array.from(rows).forEach(function (row) {
			const hit = matches(row.dataset.search, needle);
			row.hidden = !hit;
			if (hit) hits += 1;
		});
		return hits;
	}
	function statusMessage(count, noun) {
		const word = noun || 'target';
		if (count === 0) return 'No ' + word + 's found';
		return count === 1 ? '1 ' + word + ' found' : count + ' ' + word + 's found';
	}
	function countLabel(count, total) {
		return count + ' / ' + total;
	}
	// Remembers the state a group had before the search opened it; the first remembered value wins.
	function createExpansionMemory() {
		const saved = new Map();
		return {
			remember: function (key, open) {
				if (!saved.has(key)) saved.set(key, Boolean(open));
			},
			// A manual toggle during the search replaces the remembered state of that group.
			override: function (key, open) {
				if (saved.has(key)) saved.set(key, Boolean(open));
			},
			take: function () {
				const entries = Array.from(saved);
				saved.clear();
				return entries;
			},
			clear: function () {
				saved.clear();
			}
		};
	}
	root.JuListFilter = {
		MAX_QUERY_LENGTH: MAX_QUERY_LENGTH,
		normalize: normalize,
		normalizeQuery: normalizeQuery,
		matches: matches,
		filterRows: filterRows,
		statusMessage: statusMessage,
		countLabel: countLabel,
		createExpansionMemory: createExpansionMemory
	};
})(window);
