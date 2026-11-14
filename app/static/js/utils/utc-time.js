//
// app/static/js/utils/utc-time.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Classic script exposing parseUtcTimestamp(). The server stores naive timestamps ("YYYY-MM-DD HH:MM:SS"
 * or ISO without offset) in UTC, which Date.parse would read as local time.
 */
(function (root) {
	'use strict';
	var HAS_OFFSET = /(?:Z|[+-]\d{2}:\d{2})$/i;
	/** Returns epoch milliseconds for a timestamp string, treating a missing offset as UTC (NaN if invalid). */
	function parseUtcTimestamp(text) {
		var value = String(text);
		return Date.parse(HAS_OFFSET.test(value) ? value : value.replace(' ', 'T') + 'Z');
	}
	root.parseUtcTimestamp = parseUtcTimestamp;
})(typeof window !== 'undefined' ? window : globalThis);
