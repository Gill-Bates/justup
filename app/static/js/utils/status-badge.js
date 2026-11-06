//
// app/static/js/utils/status-badge.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * The one JS producer of status badges (DESIGN.md, "Badges"). A status badge carries the state by
 * its variant colour and its word, never by a glyph, so this helper offers no way to add one.
 * Also exports JuStatusDot, the JS producer of status dots (DESIGN.md, "Status display").
 * @module utils/status-badge
 */
(function (root) {
	'use strict';

	var TONES = ['success', 'warning', 'danger', 'info', 'neutral'];
	var escape = function (text) {
		return String(text === null || text === undefined ? '' : text).replace(/[&<>"']/g, function (ch) {
			return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
		});
	};

	/** Class list of a status badge; `state` gives the equal-size edge, `unknown` the dashed one that keeps UNKNOWN apart from DOWN. */
	function classes(tone, options) {
		var opts = options || {};
		var variant = TONES.indexOf(tone) === -1 ? 'neutral' : tone;
		return 'badge ju-status-badge--' + variant
			+ (opts.state || opts.unknown ? ' ju-state-badge' : '')
			+ (opts.unknown ? ' ju-state-badge--unknown' : '')
			+ (opts.className ? ' ' + opts.className : '');
	}

	/** Markup of a status badge; label and title are escaped here. */
	function html(tone, label, options) {
		var opts = options || {};
		return '<span class="' + escape(classes(tone, opts)) + '"'
			+ (opts.title ? ' title="' + escape(opts.title) + '"' : '') + '>' + escape(label) + '</span>';
	}

	/** Class list of a status dot; any tone outside the whitelist is neutral (hollow ring). */
	function dotClasses(tone) {
		var variant = TONES.indexOf(tone) === -1 ? 'neutral' : tone;
		return 'ju-status-dot ju-status-dot--' + variant + (variant === 'neutral' ? ' ju-status-dot--unknown' : '');
	}

	/**
	 * JS counterpart of the `state_dot` macro (DESIGN.md, "Status display"): coloured dot plus the
	 * state word as plain visible label. Neutral tones get the hollow ring, so colour is never the only cue.
	 */
	function dotHtml(tone, label, options) {
		var opts = options || {};
		return '<span class="ju-status-item' + (opts.className ? ' ' + escape(opts.className) : '') + '"'
			+ (opts.title ? ' title="' + escape(opts.title) + '"' : '') + '>'
			+ '<span class="' + dotClasses(tone) + '" aria-hidden="true"></span>'
			+ escape(label) + '</span>';
	}

	root.JuStatusBadge = { classes: classes, html: html };
	root.JuStatusDot = { html: dotHtml, classes: dotClasses };
})(typeof window !== 'undefined' ? window : globalThis);
