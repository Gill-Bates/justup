//
// app/static/js/utils/csrf.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Classic script exposing getCsrfToken() and withCsrf(). The token is read from the cookie on every call because
 * the server rotates it after successful authentication transitions.
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

	var CSRF_HEADER = 'X-CSRF-Token';
	/**
	 * Returns a copy of fetch() init carrying the CSRF header (unless the caller set one) and same-origin
	 * credentials (unless the caller set them). Always sends the header, whatever the method.
	 */
	function withCsrf(init) {
		var opts = init ? Object.assign({}, init) : {};
		var headers = new Headers(opts.headers || {});
		var token = getCsrfToken();
		if (token && !headers.has(CSRF_HEADER)) headers.set(CSRF_HEADER, token);
		opts.headers = headers;
		if (!opts.credentials) opts.credentials = 'same-origin';
		return opts;
	}
	root.getCsrfToken = getCsrfToken;
	root.CSRF_HEADER = CSRF_HEADER;
	root.withCsrf = withCsrf;
})(typeof window !== 'undefined' ? window : globalThis);
