//
// app/static/js/target-detail/date-range.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Date range input management: initialization, formatting, quick presets.
 * Depends on: appState.range (from target-detail.js)
 * @module target-detail/date-range
 */

/**
 * Initialize date range inputs to last 24 hours.
 * Sets both DOM inputs and appState.range.
 * Ensures newly created targets show data immediately.
 * 
 * @requires DOM elements: #range-from, #range-to
 * @requires appState.range (from target-detail.js)
 */
function initDateInputs() {
	const now = new Date();
	// Default to last 24 hours instead of "today since midnight"
	const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);

	const fromInput = document.getElementById('range-from');
	const toInput = document.getElementById('range-to');

	if (!fromInput || !toInput) {
		console.warn('date-range.js: range inputs not found in DOM');
		return;
	}

	toInput.value = formatDateForInput(now, true);    // Round up seconds
	fromInput.value = formatDateForInput(yesterday);  // Round down (default)

	if (typeof appState === 'undefined') {
		console.error('date-range.js: appState not found – is target-detail.js loaded?');
		return;
	}

	appState.range.from = yesterday;
	appState.range.to = now;
}

/**
 * Format a Date object for datetime-local input (local time, not UTC).
 * @param {Date} date - Date to format
 * @param {boolean} [roundUp=false] - If true, round up to next minute if seconds > 0
 * @returns {string} YYYY-MM-DDTHH:MM
 */
function formatDateForInput(date, roundUp = false) {
	const d = new Date(date);
	if (roundUp && d.getSeconds() > 0) {
		d.setMinutes(d.getMinutes() + 1);
		d.setSeconds(0);
	}
	const year = d.getFullYear();
	const month = String(d.getMonth() + 1).padStart(2, '0');
	const day = String(d.getDate()).padStart(2, '0');
	const hours = String(d.getHours()).padStart(2, '0');
	const minutes = String(d.getMinutes()).padStart(2, '0');
	return `${year}-${month}-${day}T${hours}:${minutes}`;
}

/**
 * Compute date range from a quick-select preset value.
 * All ranges are rolling (last N days/hours) for consistency, except 'today'.
 * @param {string} value - Preset key (e.g. '1h', '24h', '7d', 'today', 'month', etc.)
 * @returns {object|null} { from: Date, to: Date } or null if invalid
 */
function getQuickRangeDate(value) {
	const now = new Date();
	let from;

	switch (value) {
		case '1h':
			from = new Date(now.getTime() - 1 * 60 * 60 * 1000);
			break;
		case '6h':
			from = new Date(now.getTime() - 6 * 60 * 60 * 1000);
			break;
		case '24h':
			from = new Date(now.getTime() - 24 * 60 * 60 * 1000);
			break;
		case 'today':
			from = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
			break;
		case '7d':
			from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
			break;
		case 'month':
			// Rolling 30 days (consistent with other presets)
			from = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
			break;
		case 'quarter':
			// Rolling 90 days
			from = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
			break;
		case 'year':
			// Rolling 365 days (consistent with other rolling ranges)
			from = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);
			break;
		default:
			return null;
	}

	return { from, to: now };
}

/**
 * Parse a datetime-local input value as local time (browser timezone).
 * Avoids browser-dependent behavior of new Date() with timezone-less strings.
 * @private
 * @param {string} val - "YYYY-MM-DDTHH:MM"
 * @returns {Date|null}
 */
function _parseDatetimeLocal(val) {
	const match = val.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
	if (!match) return null;
	const [, y, m, d, h, min] = match;
	const date = new Date(+y, +m - 1, +d, +h, +min);
	return isNaN(date.getTime()) ? null : date;
}

/**
 * Read and validate the manual date inputs.
 * Shows a toast on validation failure.
 * 
 * @returns {{ from: Date, to: Date }|null} Validated range, or null on error
 */
function readAndValidateDateInputs() {
	const fromVal = document.getElementById('range-from')?.value;
	const toVal = document.getElementById('range-to')?.value;

	if (!fromVal || !toVal) {
		window.justUpToast?.error?.('Please select both start and end date.');
		return null;
	}

	// Parse as local time (consistent with datetime-local input)
	const from = _parseDatetimeLocal(fromVal);
	const to = _parseDatetimeLocal(toVal);

	if (!from || !to) {
		window.justUpToast?.error?.('Invalid date format.');
		return null;
	}

	if (from >= to) {
		window.justUpToast?.error?.('Start date must be before end date.');
		return null;
	}

	// Maximum range validation (prevent server overload and browser slowness)
	const MAX_RANGE_DAYS = 730; // 2 years
	const rangeDays = (to - from) / (1000 * 60 * 60 * 24);
	if (rangeDays > MAX_RANGE_DAYS) {
		window.justUpToast?.error?.(
			`Date range too large (${Math.round(rangeDays)} days). Maximum is ${MAX_RANGE_DAYS} days.`
		);
		return null;
	}

	return { from, to };
}

/**
 * Apply a quick-range value: update inputs, appState, and return the range.
 * @param {string} value - Quick-range preset
 * @returns {{ from: Date, to: Date }|null}
 */
function applyQuickRange(value) {
	const range = getQuickRangeDate(value);
	if (!range) return null;

	const fromInput = document.getElementById('range-from');
	const toInput = document.getElementById('range-to');

	if (fromInput) fromInput.value = formatDateForInput(range.from);
	if (toInput) toInput.value = formatDateForInput(range.to, true); // Round up for 'to'

	if (typeof appState === 'undefined') {
		console.error('date-range.js: appState not found – is target-detail.js loaded?');
		return range;
	}

	appState.range.from = range.from;
	appState.range.to = range.to;

	return range;
}
