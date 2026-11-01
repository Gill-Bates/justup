//
// app/static/js/utils/quality-state.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Presentation of the monitor quality state for the dashboard widget.
 * Unknown, missing or incomplete data is always neutral grey: no measurement, no error colour.
 * @module utils/quality-state
 */
(function (root) {
	'use strict';

	var BADGES = {
		ok: { cls: 'bg-success', text: 'OK', scoreCls: 'text-success' },
		degraded: { cls: 'bg-warning text-dark', text: 'Degraded', scoreCls: 'text-warning' },
		down: { cls: 'bg-danger', text: 'Down', scoreCls: 'text-danger' },
		unknown: { cls: 'bg-secondary', text: 'Unknown', scoreCls: 'text-body-secondary' },
	};

	/** Finite score or null for null, undefined, empty and non-numeric values. */
	function parseScore(score) {
		if (score === null || score === undefined || score === '') return null;
		var n = Number(score);
		return isFinite(n) ? n : null;
	}

	/**
	 * @param {{state?: string, score?: number|string|null}|null|undefined} data - /quality/current payload
	 * @returns {{state: string, badgeClass: string, badgeText: string, scoreText: string, scoreClass: string, label: string}}
	 */
	function describe(data) {
		var score = parseScore(data && data.score);
		var state = data && Object.prototype.hasOwnProperty.call(BADGES, data.state) ? data.state : 'unknown';
		// Without a measured score the state has no basis, so it is shown as unknown.
		if (score === null) state = 'unknown';
		var b = BADGES[state];
		// Unknown has no measurement basis: neutral text regardless of any score.
		var label = 'Connectivity status unavailable';
		if (state !== 'unknown') label = score >= 80 ? 'Excellent connectivity' : score >= 60 ? 'Good connectivity' : 'Poor connectivity';
		return {
			state: state,
			badgeClass: b.cls,
			badgeText: b.text,
			scoreText: score === null ? '--' : score.toFixed(1),
			scoreClass: 'display-4 fw-bold ' + b.scoreCls,
			label: label,
		};
	}

	root.QualityState = { describe: describe };
})(typeof window !== 'undefined' ? window : globalThis);
