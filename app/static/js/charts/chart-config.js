//
// app/static/js/charts/chart-config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Shared Chart.js configuration: line chart factory, zoom options, SLA annotations.
 * Depends on: withAlpha(), getCssVar() from utils/with-alpha.js
 * @module charts/chart-config
 */

// Compact viewport for chart point density: the 576 px compact-header breakpoint of DC-BP-1 (width <= 576px).
const COMPACT_VIEWPORT_MAX = 576;
// Chart width in px (a container measure, not a viewport breakpoint) below which the legend moves to the bottom.
const LEGEND_BOTTOM_MAX_CHART_WIDTH = 768;
// SLA annotation styling
const SLA_LINE_COLOR = "#f0ad4e";
const SLA_LINE_WIDTH = 2;

function formatCompactAxisValue(value) {
  if (!Number.isFinite(value)) return "";
  const magnitude = Math.abs(value);
  if (magnitude < 1000) return String(value);
  const divisor = magnitude >= 999950 ? 1000000 : 1000;
  const suffix = divisor === 1000000 ? "M" : "K";
  const scaled = Math.round((value / divisor) * 10) / 10;
  return (
    new Intl.NumberFormat(navigator.language || "en", {
      maximumFractionDigits: 1,
    }).format(scaled) + suffix
  );
}

/**
 * One place for chart units. Axis ticks, tooltips and descriptions read the unit from here,
 * so a metric never gets its unit from a per-chart literal.
 */
const CHART_UNITS = Object.freeze({
  ms: Object.freeze({ suffix: "ms", axisTitle: "Response time (ms)" }),
  percent: Object.freeze({ suffix: "%", axisTitle: "Percent (%)" }),
  checks: Object.freeze({ suffix: "checks", axisTitle: "Checks" }),
  score: Object.freeze({ suffix: "pts", axisTitle: "Quality score (0-100)" }),
});
/** "123 ms": value plus the unit suffix of the metric. */
function formatChartValue(unit, value) {
  const def = CHART_UNITS[unit];
  return def ? value + " " + def.suffix : String(value);
}
/** Axis tick: compact number; carries the unit itself when the axis title is hidden. */
function formatChartTick(unit, value, withUnit) {
  const text = formatCompactAxisValue(value);
  const def = CHART_UNITS[unit];
  return withUnit && def && text !== "" ? text + " " + def.suffix : text;
}
/** Locale number formatter shared by chart counts and shares (same locale as the axis ticks). */
function formatChartCount(value) {
  return new Intl.NumberFormat(navigator.language || "en").format(value);
}
/**
 * Shares of a total in 0.1 % steps whose sum is exactly 100 % (largest-remainder rounding).
 * @param {number[]} counts
 * @returns {number[]} fractions (0..1) in the order of the input
 */
function chartShares(counts) {
  const total = counts.reduce((sum, c) => sum + c, 0);
  if (!(total > 0)) return counts.map(() => 0);
  const exact = counts.map((c) => (c / total) * 1000);
  const tenths = exact.map(Math.floor);
  let rest = 1000 - tenths.reduce((sum, t) => sum + t, 0);
  exact
    .map((value, index) => ({ index, frac: value - Math.floor(value) }))
    .sort((a, b) => b.frac - a.frac || a.index - b.index)
    .forEach((item) => {
      if (rest > 0) {
        tenths[item.index] += 1;
        rest -= 1;
      }
    });
  return tenths.map((t) => t / 1000);
}
/** "97,5 %" / "97.5%" in the page locale, one decimal. */
function formatChartShare(fraction) {
  return new Intl.NumberFormat(navigator.language || "en", {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(fraction);
}
/**
 * Scriptable fill: vertical gradient from the line color down to transparent.
 * Resolved lazily because chartArea is unavailable until the first layout pass.
 * @param {string} color - Line color
 * @param {number} [topAlpha=0.2] - Opacity at the top of the plot area
 * @returns {Function} Chart.js scriptable option
 */
function createAreaGradient(color, topAlpha) {
  const alpha = typeof topAlpha === "number" ? topAlpha : 0.2;
  return function (context) {
    const area = context.chart.chartArea;
    if (!area) return withAlpha(color, alpha);
    const gradient = context.chart.ctx.createLinearGradient(
      0,
      area.top,
      0,
      area.bottom,
    );
    gradient.addColorStop(0, withAlpha(color, alpha));
    gradient.addColorStop(1, withAlpha(color, 0));
    return gradient;
  };
}

/**
 * Zoom/pan plugin options (shared by all line charts).
 * Requires chartjs-plugin-zoom.
 * @param {Function} onComplete - Callback after zoom/pan (e.g. updateChartSampling)
 */
function createZoomOptions(onComplete) {
  return {
    zoom: {
      drag: {
        enabled: true,
        backgroundColor: withAlpha(getCssVar("--bs-primary", "#0d6efd"), 0.2),
        borderColor: withAlpha(getCssVar("--bs-primary", "#0d6efd"), 0.8),
        borderWidth: 1,
      },
      mode: "x",
      onZoomComplete: onComplete
        ? function (context) {
            if (context && context.chart) onComplete(context.chart);
          }
        : undefined,
    },
    pan: {
      enabled: true,
      mode: "x",
      onPanComplete: onComplete
        ? function (context) {
            if (context && context.chart) onComplete(context.chart);
          }
        : undefined,
    },
  };
}

/**
 * Create SLA annotation line for charts.
 * @param {object} slaConfig - { enabled, availabilityPct, responseTimeMs, pingLatencyMs }
 * @param {number|null} value - SLA target value
 * @param {string} label - Label text (e.g. "200 ms")
 * @returns {object|null} Chart.js annotation config, or null if disabled
 */
function createSlaAnnotation(slaConfig, value, label) {
  if (!slaConfig || !slaConfig.enabled || value === null || value === undefined)
    return null;
  return {
    type: "line",
    yMin: value,
    yMax: value,
    borderColor: SLA_LINE_COLOR,
    borderWidth: SLA_LINE_WIDTH,
    borderDash: [6, 4],
    label: {
      display: true,
      content: "SLA: " + String(label || "").substring(0, 50),
      position: "end",
      backgroundColor: SLA_LINE_COLOR,
      color: "#000",
      font: { size: 10, weight: "bold" },
      padding: { top: 2, bottom: 2, left: 4, right: 4 },
    },
  };
}

/**
 * Build a standard Chart.js line-chart config object.
 * Does NOT create a Chart instance — returns the config for `new Chart(ctx, config)`.
 * @param {object} theme - Chart theme colors
 * @param {string} theme.text - Primary text color
 * @param {string} theme.muted - Secondary/muted text color (axis ticks)
 * @param {string} theme.grid - Grid line color
 * @param {object} [zoomOpts] - Zoom plugin options (from createZoomOptions)
 * @param {Function} [getVisibleRangeMs] - Function returning visible x-range in ms
 * @returns {object} Chart.js config
 */
function createLineConfig(theme, zoomOpts, getVisibleRangeMs) {
  // Locale-aware axis formatters
  const userLocale = navigator.language || "en";

  // Read lazily: pageConfig is filled after this module loads (UTC display setting).
  function tzOptions() {
    return typeof pageConfig !== "undefined" && pageConfig.useUtc === true
      ? { timeZone: "UTC" }
      : {};
  }
  function formatAxisTime(date) {
    return date.toLocaleTimeString(
      userLocale,
      Object.assign(
        {
          hour: "2-digit",
          minute: "2-digit",
          second: undefined,
          hour12: false, // consistent width across locales
        },
        tzOptions(),
      ),
    );
  }
  function formatAxisDate(date) {
    return date.toLocaleDateString(
      userLocale,
      Object.assign({ month: "short", day: "numeric" }, tzOptions()),
    );
  }
  function formatAxisDateTime(date) {
    return formatAxisDate(date) + " " + formatAxisTime(date);
  }

  return {
    type: "line",
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 12, right: 12, left: 6, bottom: 6 } },
      clip: false,
      scales: {
        x: {
          type: "time",
          display: true,
          ticks: {
            color: theme.muted,
            font: { size: 12 },
            maxRotation: 0,
            autoSkipPadding: 24,
            padding: 8,
            callback: function (value) {
              const date = new Date(value);
              const rangeMs =
                typeof getVisibleRangeMs === "function"
                  ? getVisibleRangeMs(this.chart)
                  : 0;
              const rangeDays = rangeMs / (1000 * 60 * 60 * 24);
              if (rangeDays <= 1) return formatAxisTime(date);
              if (rangeDays <= 7) return formatAxisDateTime(date);
              return formatAxisDate(date);
            },
          },
          grid: { display: false },
          border: { display: false },
          title: { display: false },
        },
        y: {
          display: true,
          beginAtZero: true,
          ticks: {
            color: theme.muted,
            font: { size: 12 },
            padding: 8,
            callback: function (value) {
              if (Number.isInteger(value)) return value;
              // Show one decimal for fractional SLA values (e.g., 99.9)
              // Suppress Chart.js auto-generated intermediate sub-ticks
              if (value === Math.round(value * 10) / 10) {
                return value.toFixed(1);
              }
              return "";
            },
          },
          grid: { color: theme.grid, drawTicks: false, tickBorderDash: [4, 3] },
          border: { display: false },
        },
      },
      // Fire hover/tooltip only when the pointer is directly over a point,
      // not anywhere along the x-axis band.
      interaction: { mode: "point", intersect: true },
      plugins: {
        legend: { display: false, labels: { color: theme.text } },
        zoom: zoomOpts || {},
        tooltip: {
          padding: 10,
          cornerRadius: 6,
          boxPadding: 4,
          borderWidth: 1,
          titleFont: { size: 12, weight: "600" },
          bodyFont: { size: 12 },
          callbacks: {
            title: function (tooltipItems) {
              if (!tooltipItems.length) return "";
              return formatAxisDateTime(new Date(tooltipItems[0].parsed.x));
            },
            label: function (ctx) {
              // Default: unit-agnostic. Chart builders override for "ms" or "%"
              return " " + Math.round(ctx.parsed.y);
            },
          },
        },
      },
      elements: {
        // Tight hitRadius so the tooltip triggers on the point itself, not a wide band.
        point: { radius: 0, hoverRadius: 5, borderWidth: 2, hitRadius: 3 },
        line: { tension: 0.4, borderWidth: 2 },
      },
    },
  };
}

// Explicit exports for global access
window.createLineConfig = createLineConfig;
window.createAreaGradient = createAreaGradient;
window.createZoomOptions = createZoomOptions;
window.createSlaAnnotation = createSlaAnnotation;
window.formatCompactAxisValue = formatCompactAxisValue;
window.CHART_UNITS = CHART_UNITS;
window.formatChartValue = formatChartValue;
window.formatChartTick = formatChartTick;
window.formatChartCount = formatChartCount;
window.chartShares = chartShares;
window.formatChartShare = formatChartShare;
