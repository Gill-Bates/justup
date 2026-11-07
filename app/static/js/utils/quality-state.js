//
// app/static/js/utils/quality-state.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Presentation of the monitor quality state for the dashboard widget.
 * The status dot and the score value carry the state color (success/warning/danger); unknown stays neutral.
 * @module utils/quality-state
 */
(function (root) {
	'use strict';

	var BADGES = {
		ok: { tone: 'success', text: 'OK' },
		degraded: { tone: 'warning', text: 'Degraded' },
		down: { tone: 'danger', text: 'Down' },
		unknown: { tone: 'neutral', text: 'Unknown' },
	};

	/** Finite score or null for null, undefined, empty and non-numeric values. */
	function parseScore(score) {
		if (score === null || score === undefined || score === '') return null;
		var n = Number(score);
		return isFinite(n) ? n : null;
	}

	/**
	 * @param {{state?: string, score?: number|string|null}|null|undefined} data - /quality/current payload
	 * @returns {{state: string, badgeTone: string, badgeText: string, scoreText: string, scoreClass: string, label: string}}
	 */
	function describe(data) {
		var score = parseScore(data && data.score);
		var state = data && Object.prototype.hasOwnProperty.call(BADGES, data.state) ? data.state : 'unknown';
		// Without a measured score the state has no basis, so it is shown as unknown.
		if (score === null) state = 'unknown';
		var b = BADGES[state];
		// Unknown has no measurement basis and stays neutral; typography is shared with measured states.
		var label = 'Connectivity status unavailable';
		if (state !== 'unknown') label = score >= 80 ? 'Excellent connectivity' : score >= 60 ? 'Good connectivity' : 'Poor connectivity';
		return {
			state: state,
			badgeTone: b.tone,
			badgeText: b.text,
			scoreText: score === null ? '--' : score.toFixed(1),
			scoreClass: 'dashboard-widget-value fw-bold' + (b.tone === 'neutral' ? '' : ' quality-score--' + b.tone),
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
