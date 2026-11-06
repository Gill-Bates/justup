//
// app/static/js/target-detail/date-range.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Date range input management: initialization, formatting, quick presets.
 * Depends on: appState.range (from target-detail.js)
 * @module target-detail/date-range
 */

/** Longest range accepted from the URL and from the manual inputs (2 years). */
const MAX_RANGE_DAYS = 730;
const RANGE_URL_PARAMS = ["range", "from", "to"];
// Strict UTC ISO-8601: seconds and milliseconds are optional, no offsets, no free-form dates.
const ISO_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?Z$/;

/**
 * Parse an ISO-8601 UTC timestamp strictly; rolled-over dates (31 Feb) are rejected.
 * @private
 * @param {string|null} value
 * @returns {Date|null}
 */
function _parseIsoUtc(value) {
  if (typeof value !== "string" || value.length > 30 || !ISO_UTC_RE.test(value)) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const [y, m, d] = value.split("T")[0].split("-").map(Number);
  const exact = date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
  return exact ? date : null;
}

/**
 * Read the time range a link carries: `?range=<preset>` or `?from=<ISO UTC>&to=<ISO UTC>`.
 * Anything missing or invalid yields null (caller falls back to the 24 h default); raw values
 * are never echoed.
 * @param {string} search - location.search
 * @returns {{ from: Date, to: Date, preset: string }|null} preset is "" for an explicit range
 */
function parseRangeFromSearch(search) {
  let params;
  try {
    params = new URLSearchParams(search || "");
  } catch {
    return null;
  }
  const preset = params.get("range");
  if (preset !== null) {
    // The shape check keeps arbitrary input away from the resolver.
    const range = /^[a-z0-9]{1,8}$/.test(preset) ? getQuickRangeDate(preset) : null;
    return range ? { ...range, preset } : null;
  }
  const from = _parseIsoUtc(params.get("from"));
  const to = _parseIsoUtc(params.get("to"));
  if (!from || !to || from >= to) return null;
  if ((to - from) / (24 * 60 * 60 * 1000) > MAX_RANGE_DAYS) return null;
  return { from, to, preset: "" };
}

/** Remove the range parameters from the address bar so a later reload does not override the user's choice. */
function _dropRangeFromUrl() {
  if (typeof location === "undefined" || typeof history === "undefined") return;
  try {
    const url = new URL(location.href);
    if (!RANGE_URL_PARAMS.some((name) => url.searchParams.has(name))) return;
    RANGE_URL_PARAMS.forEach((name) => url.searchParams.delete(name));
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  } catch {
    // Cosmetic only; the range is already applied.
  }
}

/**
 * Initialize date range inputs: the range carried by the URL (see parseRangeFromSearch), else the
 * last 24 hours. Sets the DOM inputs, the preset select and appState.range, so it must run before
 * the first chart load.
 *
 * @param {string} [search] - Query string to read; defaults to location.search
 * @requires DOM elements: #range-from, #range-to
 * @requires appState.range (from target-detail.js)
 */
function initDateInputs(search) {
  const fromInput = document.getElementById("range-from");
  const toInput = document.getElementById("range-to");

  if (!fromInput || !toInput) {
    console.warn("date-range.js: range inputs not found in DOM");
    return;
  }

  const query = search !== undefined ? search : typeof location !== "undefined" ? location.search : "";
  const fromUrl = parseRangeFromSearch(query);
  const range = fromUrl || { ...getQuickRangeDate("24h"), preset: "24h" };

  toInput.value = formatDateForInput(range.to, true); // Round up seconds
  fromInput.value = formatDateForInput(range.from); // Round down (default)

  if (fromUrl) {
    const quick = document.getElementById("quick-range");
    if (quick) quick.value = fromUrl.preset;
    const custom = document.getElementById("target-custom-range");
    if (custom) custom.open = fromUrl.preset === "";
  }
  // Strip valid and invalid range overrides alike. A stale or malformed link must not keep
  // overriding the range after the page has selected its safe default.
  _dropRangeFromUrl();

  if (typeof appState === "undefined") {
    console.error(
      "date-range.js: appState not found – is target-detail.js loaded?",
    );
    return;
  }

  appState.range.from = range.from;
  appState.range.to = range.to;
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
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hours = String(d.getHours()).padStart(2, "0");
  const minutes = String(d.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function _usesUtc() {
  return typeof pageConfig !== "undefined" && pageConfig.useUtc === true;
}
/**
 * First instant of the current month or year, in UTC when pageConfig.useUtc is set.
 * @private
 * @param {Date} now
 * @param {boolean} wholeYear - true for January 1st, false for the 1st of the month
 * @returns {Date}
 */

function _calendarStart(now, wholeYear) {
  const utc = _usesUtc();
  if (utc) {
    return new Date(
      Date.UTC(now.getUTCFullYear(), wholeYear ? 0 : now.getUTCMonth(), 1),
    );
  }
  return new Date(now.getFullYear(), wholeYear ? 0 : now.getMonth(), 1);
}

/**
 * Compute date range from a quick-select preset value.
 * Rolling windows (last N hours/days) except 'today', 'month' and 'year', which start at the
 * calendar boundary (local time, or UTC when pageConfig.useUtc is set).
 * @param {string} value - Preset key (e.g. '1h', '24h', '7d', 'today', 'month', etc.)
 * @returns {object|null} { from: Date, to: Date } or null if invalid
 */
function getQuickRangeDate(value) {
  const now = new Date();
  let from;

  switch (value) {
    case "1h":
      from = new Date(now.getTime() - 1 * 60 * 60 * 1000);
      break;
    case "6h":
      from = new Date(now.getTime() - 6 * 60 * 60 * 1000);
      break;
    case "24h":
      from = new Date(now.getTime() - 24 * 60 * 60 * 1000);
      break;
    case "today":
      from = _usesUtc()
        ? new Date(
            Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()),
          )
        : new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      break;
    case "7d":
      from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
      break;
    case "month":
      from = _calendarStart(now, false);
      break;
    case "quarter":
      // Rolling 90 days
      from = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
      break;
    case "year":
      from = _calendarStart(now, true);
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
  // Date() rolls over 31 Feb and skips DST gaps; reject anything that did not round-trip.
  const exact =
    date.getFullYear() === +y &&
    date.getMonth() === +m - 1 &&
    date.getDate() === +d &&
    date.getHours() === +h &&
    date.getMinutes() === +min;
  return exact ? date : null;
}

/**
 * Read and validate the manual date inputs.
 * Shows a toast on validation failure.
 *
 * @returns {{ from: Date, to: Date }|null} Validated range, or null on error
 */
function readAndValidateDateInputs() {
  const fromVal = document.getElementById("range-from")?.value;
  const toVal = document.getElementById("range-to")?.value;

  if (!fromVal || !toVal) {
    window.justUpToast?.error?.("Please select both start and end date.");
    return null;
  }

  // Parse as local time (consistent with datetime-local input)
  const from = _parseDatetimeLocal(fromVal);
  const to = _parseDatetimeLocal(toVal);

  if (!from || !to) {
    window.justUpToast?.error?.(
      "Invalid date or time (it may not exist in your time zone).",
    );
    return null;
  }

  if (from >= to) {
    window.justUpToast?.error?.("Start date must be before end date.");
    return null;
  }

  // Maximum range validation (prevent server overload and browser slowness)
  const rangeDays = (to - from) / (1000 * 60 * 60 * 24);
  if (rangeDays > MAX_RANGE_DAYS) {
    window.justUpToast?.error?.(
      `Date range too large (${Math.round(rangeDays)} days). Maximum is ${MAX_RANGE_DAYS} days.`,
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

  const fromInput = document.getElementById("range-from");
  const toInput = document.getElementById("range-to");

  if (fromInput) fromInput.value = formatDateForInput(range.from);
  if (toInput) toInput.value = formatDateForInput(range.to, true); // Round up for 'to'

  if (typeof appState === "undefined") {
    console.error(
      "date-range.js: appState not found – is target-detail.js loaded?",
    );
    return range;
  }

  appState.range.from = range.from;
  appState.range.to = range.to;

  return range;
}
