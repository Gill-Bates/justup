//
// app/static/js/utils/quality-state.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Presentation of the monitor quality state for the dashboard widget.
 * The status dot carries status color; the score stays a neutral primary value in every state.
 * @module utils/quality-state
 */
(function (root) {
	'use strict';

	var BADGES = {
		ok: { cls: 'ju-status-badge--success', tone: 'success', text: 'OK' },
		degraded: { cls: 'ju-status-badge--warning', tone: 'warning', text: 'Degraded' },
		down: { cls: 'ju-status-badge--danger', tone: 'danger', text: 'Down' },
		unknown: { cls: 'ju-status-badge--neutral', tone: 'neutral', text: 'Unknown' },
	};

	/** Finite score or null for null, undefined, empty and non-numeric values. */
	function parseScore(score) {
		if (score === null || score === undefined || score === '') return null;
		var n = Number(score);
		return isFinite(n) ? n : null;
	}

	/**
	 * @param {{state?: string, score?: number|string|null}|null|undefined} data - /quality/current payload
	 * @returns {{state: string, badgeClass: string, badgeTone: string, badgeText: string, scoreText: string, scoreClass: string, label: string}}
	 */
	function describe(data) {
		var score = parseScore(data && data.score);
		var state = data && Object.prototype.hasOwnProperty.call(BADGES, data.state) ? data.state : 'unknown';
		// Without a measured score the state has no basis, so it is shown as unknown.
		if (score === null) state = 'unknown';
		var b = BADGES[state];
		// Unknown has no measurement basis, but score typography stays shared with measured states.
		var label = 'Connectivity status unavailable';
		if (state !== 'unknown') label = score >= 80 ? 'Excellent connectivity' : score >= 60 ? 'Good connectivity' : 'Poor connectivity';
		return {
			state: state,
			badgeClass: b.cls,
			badgeTone: b.tone,
			badgeText: b.text,
			scoreText: score === null ? '--' : score.toFixed(1),
			scoreClass: 'dashboard-widget-value fw-bold',
			label: label,
		};
	}

	/**
	 * Accessible text for a probe-target badge; no country code means the location is unknown,
	 * which is the normal case for anycast resolvers (GeoLite2 has no single country for them).
	 * @param {string} address - target without scheme
	 * @param {string|null} country - ISO 3166-1 alpha-2 code or null
	 * @returns {{label: string, title: string}}
	 */
	function describeTarget(address, country) {
		if (country) return { label: country + ': ' + address, title: '' };
		var text = 'Country unknown (anycast or no location data)';
		return { label: text + ': ' + address, title: text };
	}
	root.QualityState = { describe: describe, describeTarget: describeTarget };
})(typeof window !== 'undefined' ? window : globalThis);
