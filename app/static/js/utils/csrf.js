//
// app/static/js/utils/csrf.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Classic script exposing getCsrfToken(). The token is read from the cookie on every call because
 * the server rotates it after each validated state-changing request.
 */
(function (root) {
	'use strict';

	function getCsrfToken() {
		var cookies = (root.document && root.document.cookie) || '';
		var parts = cookies ? cookies.split(';') : [];
		for (var i = 0; i < parts.length; i++) {
			var part = parts[i].trim();
			if (part.indexOf('csrf_token=') === 0) {
				var raw = part.slice('csrf_token='.length);
				try { return decodeURIComponent(raw); } catch (e) { return raw; }
			}
		}
		return '';
	}

	root.getCsrfToken = getCsrfToken;
})(typeof window !== 'undefined' ? window : globalThis);
