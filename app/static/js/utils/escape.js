//
// app/static/js/utils/escape.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Classic script exposing escapeHtml() and escapeAttr(). Both neutralize quotes as well as
 * angle brackets, so the result is safe in element text and in quoted attribute values.
 */
(function (root) {
	'use strict';

	var ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

	function escapeHtml(text) {
		if (text === null || text === undefined) return '';
		return String(text).replace(/[&<>"']/g, function (ch) { return ENTITIES[ch]; });
	}

	root.escapeHtml = escapeHtml;
	root.escapeAttr = escapeHtml;
})(typeof window !== 'undefined' ? window : globalThis);
