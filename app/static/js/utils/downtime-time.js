//
// app/static/js/utils/downtime-time.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Compact time ranges of the downtime history in local time or UTC.
 * Mirrors app/utils/downtime_view.py (format_range, format_precise); only formats, never computes downtime.
 * @module utils/downtime-time
 */

const DOWNTIME_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function _downtimeParts(date, utc) {
	return utc
		? { y: date.getUTCFullYear(), mo: date.getUTCMonth(), d: date.getUTCDate(), h: date.getUTCHours(), mi: date.getUTCMinutes(), s: date.getUTCSeconds() }
		: { y: date.getFullYear(), mo: date.getMonth(), d: date.getDate(), h: date.getHours(), mi: date.getMinutes(), s: date.getSeconds() };
}

function _pad2(value) {
	return String(value).padStart(2, '0');
}

function _clock(p) {
	return `${_pad2(p.h)}:${_pad2(p.mi)}`;
}

function _day(p, withYear) {
	return `${p.d} ${DOWNTIME_MONTHS[p.mo]}${withYear ? ` ${p.y}` : ''}`;
}

/**
 * Compact range: `4 Nov · 18:57–21:50`, `4 Nov 23:50 → 5 Nov 00:15`, `4 Nov · since 13:06`.
 * The year is added only outside the current year or across a year boundary.
 * @param {string} startIso
 * @param {string} endIso - Empty for an ongoing period
 * @param {{utc?: boolean, now?: Date}} [options]
 * @returns {string} Empty for an unparseable start
 */
function formatDowntimeRange(startIso, endIso, options) {
	const utc = Boolean(options && options.utc);
	const now = (options && options.now) || new Date();
	const start = new Date(startIso);
	if (Number.isNaN(start.getTime())) return '';
	const end = endIso ? new Date(endIso) : null;
	const a = _downtimeParts(start, utc);
	const b = end && !Number.isNaN(end.getTime()) ? _downtimeParts(end, utc) : null;
	const withYear = a.y !== _downtimeParts(now, utc).y || (b !== null && b.y !== a.y);
	if (!b) return `${_day(a, withYear)} · since ${_clock(a)}`;
	if (a.y === b.y && a.mo === b.mo && a.d === b.d) {
		const span = _clock(a) === _clock(b) ? _clock(a) : `${_clock(a)}–${_clock(b)}`;
		return `${_day(a, withYear)} · ${span}`;
	}
	return `${_day(a, withYear)} ${_clock(a)} → ${_day(b, withYear)} ${_clock(b)}`;
}

function _zoneName(date) {
	const part = new Intl.DateTimeFormat('en-GB', { timeZoneName: 'short' }).formatToParts(date)
		.find(function (item) { return item.type === 'timeZoneName'; });
	return part ? part.value : '';
}

/**
 * Full timestamps with seconds for the tooltip and the screen-reader text.
 * @param {string} startIso
 * @param {string} endIso - Empty for an ongoing period
 * @param {{utc?: boolean}} [options]
 * @returns {string}
 */
function formatDowntimePrecise(startIso, endIso, options) {
	const utc = Boolean(options && options.utc);
	const one = function (date) {
		const p = _downtimeParts(date, utc);
		return `${p.y}-${_pad2(p.mo + 1)}-${_pad2(p.d)} ${_pad2(p.h)}:${_pad2(p.mi)}:${_pad2(p.s)} ${utc ? 'UTC' : _zoneName(date)}`;
	};
	const start = new Date(startIso);
	if (Number.isNaN(start.getTime())) return '';
	const end = endIso ? new Date(endIso) : null;
	return `${one(start)} → ${end && !Number.isNaN(end.getTime()) ? one(end) : 'ongoing'}`;
}
