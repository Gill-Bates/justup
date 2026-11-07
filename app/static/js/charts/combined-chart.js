//
// app/static/js/charts/combined-chart.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Combined chart: latency lines (left axis), HTTP status markers (lower band, shared x-axis)
 * and downtime background bands. Legend toggles are real buttons for keyboard use.
 * Depends on: chart-config.js, chart-builder.js (_registerChart, _destroyExisting),
 *   combined-chart-data.js (CombinedChartData), appState.
 * @module charts/combined-chart
 */

/**
 * Status, downtime and SLA colors with >= 3:1 contrast on the card background in each theme.
 * Metric series colors are not here: they come from the --ju-dv-N tokens (see _combinedPalette).
 */
var COMBINED_PALETTES = {
  light: {
    "2xx": "#15803d",
    info: "#0369a1",
    "3xx": "#15803d",
    "4xx": "#c2410c",
    "5xx": "#b91c1c",
    err: "#7f1d1d",
    down: "#dc3545",
    sla: "#a16207",
    slaText: "#ffffff",
    peak: "#c62828",
  },
  dark: {
    "2xx": "#4ade80",
    info: "#7dd3fc",
    "3xx": "#4ade80",
    "4xx": "#fb923c",
    "5xx": "#f87171",
    err: "#fca5a5",
    down: "#f87171",
    sla: "#facc15",
    slaText: "#000000",
    peak: "#ff6b6b",
  },
};

/** Status palette plus the categorical series colors, read from the active theme's tokens. */
function _combinedPalette(theme) {
  var series = [];
  for (var i = 1; i <= CombinedChartData.SERIES_SLOT_COUNT; i++)
    series.push(getCssVar("--ju-dv-" + i, theme.text));
  var base = COMBINED_PALETTES[theme && theme.mode === "dark" ? "dark" : "light"];
  return Object.assign({ series: series }, base, {
    peak: getCssVar("--ju-peak", base.peak),
    singleMetric: getCssVar("--ju-st-online", base["2xx"]),
  });
}

/** Color of a metric dataset: its stable palette slot, independent of visibility or order. */
function _seriesColor(palette, dataset) {
  return dataset._singleMetric ? palette.singleMetric : palette.series[dataset._slot];
}

function _prefersReducedMotion() {
  return !!(
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Name of the chart-local interaction mode that makes the whole status segment hoverable. */
var COMBINED_INTERACTION_MODE = "combinedStatus";

/**
 * Register (once) a custom interaction mode: the ms/peak lines keep plain point+intersect hover,
 * and the status strip additionally resolves the bucket under the pointer's x across its full
 * [x, end] width — the colored segments span that range while the dataset points sit only at x.
 */
function _registerCombinedInteraction() {
  var modes = window.Chart && Chart.Interaction && Chart.Interaction.modes;
  if (!modes || modes[COMBINED_INTERACTION_MODE]) return;
  modes[COMBINED_INTERACTION_MODE] = function (
    chart,
    e,
    options,
    useFinalPosition,
  ) {
    var items = modes.point(chart, e, options, useFinalPosition);
    var di = chart.data.datasets.findIndex(function (d) {
      return d._kind === "status";
    });
    if (di < 0 || !chart.isDatasetVisible(di)) return items;
    var helpers = window.Chart && Chart.helpers;
    var isLeave = !e || e.type === "mouseout";
    var pos =
      isLeave || !helpers ? null : helpers.getRelativePosition(e, chart);
    var meta = chart.getDatasetMeta(di);
    var statusScale = chart.scales.yStatus;
    if (!pos || !meta || !chart.scales.x || !statusScale) return items;
    var statusTop = Math.min(statusScale.top, statusScale.bottom);
    var statusBottom = Math.max(statusScale.top, statusScale.bottom);
    if (pos.y < statusTop || pos.y > statusBottom)
      return items.filter(function (it) {
        return chart.data.datasets[it.datasetIndex]._kind !== "status";
      });
    if (
      items.some(function (it) {
        return chart.data.datasets[it.datasetIndex]._kind === "status";
      })
    )
      return items;
    var xVal = chart.scales.x.getValueForPixel(pos.x);
    var idx = chart.data.datasets[di].data.findIndex(function (p) {
      return xVal >= p.x && xVal <= p.end;
    });
    if (idx < 0 || !meta.data[idx]) return items;
    return items.concat([
      { element: meta.data[idx], datasetIndex: di, index: idx },
    ]);
  };
}

function _el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Draw a small CSS swatch whose shape mirrors the chart marker (not color-only). */
function _swatch(shape, color, isLine) {
  var s = _el(
    "span",
    "combined-swatch combined-swatch--" + (isLine ? "line" : shape),
  );
  s.style.setProperty("--swatch-color", color);
  s.setAttribute("aria-hidden", "true");
  return s;
}

/** Style downtime boxes ("down*"), SLA lines ("sla*") and status strip ("status*") per theme. */
function _styleAnnotations(ann, palette) {
  Object.keys(ann || {}).forEach(function (k) {
    if (k.indexOf("down") === 0) {
      ann[k].backgroundColor = withAlpha(palette.down, 0.14);
      ann[k].borderColor = palette.down;
    } else if (k.indexOf("sla") === 0) {
      ann[k].borderColor = palette.sla;
      if (ann[k].label) {
        ann[k].label.backgroundColor = palette.sla;
        ann[k].label.color = palette.slaText;
      }
    } else if (k.indexOf("status") === 0 && ann[k]._cls) {
      ann[k].backgroundColor = palette[ann[k]._cls];
    }
  });
}

function _downAnnotations(bands, palette, visible) {
  var annotations = {};
  bands.forEach(function (band, i) {
    annotations["down" + i] = {
      type: "box",
      xMin: band.start,
      xMax: band.end,
      display: visible,
      drawTime: "beforeDatasetsDraw",
      backgroundColor: withAlpha(palette.down, 0.14),
      borderColor: palette.down,
      borderWidth: 1,
      borderDash: [4, 3],
    };
  });
  return annotations;
}

/** True while the "HTTP status" dataset is visible, so the strip follows its legend toggle. */
function _statusDatasetVisible(chart) {
  var i = chart.data.datasets.findIndex(function (d) {
    return d._kind === "status";
  });
  return i < 0 || chart.isDatasetVisible(i);
}

/**
 * Box annotations that fill the yStatus lane with one adjacent colored segment per bucket,
 * producing a gap-free categorical status strip (no interpolation across codes). Each box's
 * display is bound to the status dataset's visibility so the legend toggle hides the strip too.
 * @param {{x:number,end:number,cls:string}[]} buckets
 * @param {object} palette
 * @returns {object} keyed statusN annotations
 */
function _statusStripAnnotations(buckets, palette) {
  var ann = {};
  buckets.forEach(function (b, i) {
    ann["status" + i] = {
      type: "box",
      xScaleID: "x",
      yScaleID: "yStatus",
      xMin: b.x,
      xMax: b.end,
      yMin: 0,
      yMax: 1,
      drawTime: "beforeDatasetsDraw",
      backgroundColor: palette[b.cls],
      borderWidth: 0,
      display: function (ctx) {
        return _statusDatasetVisible(ctx.chart);
      },
      _cls: b.cls,
    };
  });
  return ann;
}


/**
 * Chart-local plugin: one thin solid stem per peak, from the interval median up to the peak on
 * the shared response-time axis. Clipped to the plot area; peaks are never joined to each other.
 */
var COMBINED_PEAK_STEMS = {
  id: "combinedPeakStems",
  beforeDatasetDraw: function (chart, args) {
    var ds = chart.data.datasets[args.index];
    if (!ds || ds._kind !== "peak" || !chart.isDatasetVisible(args.index)) return;
    var xScale = chart.scales.x;
    var yScale = chart.scales[ds.yAxisID || "y"];
    var area = chart.chartArea;
    if (!xScale || !yScale || !area) return;
    var ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
    ctx.clip();
    ctx.strokeStyle = (chart._palette && chart._palette.peak) || ds.borderColor;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    ds.data.forEach(function (p) {
      if (!p || p.median == null || !isFinite(p.y)) return;
      var px = Math.round(xScale.getPixelForValue(p.x)) + 0.5;
      ctx.moveTo(px, yScale.getPixelForValue(p.median));
      ctx.lineTo(px, yScale.getPixelForValue(p.y));
    });
    ctx.stroke();
    ctx.restore();
  },
};

/** Re-apply theme colors to datasets, bands and legend swatches (called from applyChartTheme). */
function _themeCombined(chart, theme) {
  var palette = _combinedPalette(theme);
  chart._palette = palette;
  chart.data.datasets.forEach(function (ds) {
    if (ds._kind === "peak") {
      ds.borderColor = palette.peak;
      ds.backgroundColor = palette.peak;
      ds.pointBackgroundColor = palette.peak;
      ds.pointBorderColor = palette.peak;
    } else if (ds._srcId) {
      var color = _seriesColor(palette, ds);
      ds.borderColor = color;
      ds.backgroundColor = createAreaGradient(color, 0.18);
    }
  });
  _styleAnnotations(
    chart.options.plugins.annotation &&
      chart.options.plugins.annotation.annotations,
    palette,
  );
  ["y", "yStatus"].forEach(function (id) {
    var sc = chart.options.scales[id];
    if (!sc) return;
    if (sc.title) sc.title.color = theme.muted;
    if (sc.border && id === "yStatus") sc.border.color = theme.grid;
  });
  if (chart._renderLegend) chart._renderLegend();
}

/** Local date/time string, or UTC when the page is set to UTC display. */
function _fmtDateTime(ms) {
  var utc = typeof pageConfig !== "undefined" && pageConfig.useUtc === true;
  return new Date(ms).toLocaleString(
    undefined,
    utc ? { timeZone: "UTC" } : undefined,
  );
}

function _statusTooltip(item) {
  var raw = item.raw;
  var cls = CombinedChartData.STATUS_CLASSES[raw.cls];
  if (raw.end) {
    var period = _fmtDateTime(raw.x) + " – " + _fmtDateTime(raw.end);
    var codes = Object.keys(raw.codeCounts || {})
      .sort()
      .map(function (code) {
        return code + " × " + raw.codeCounts[code];
      })
      .join(", ");
    return [
      period,
      "Worst: HTTP " + Math.round(raw.code) + " (" + cls.label + ")",
      raw.count + " checks" + (codes ? ": " + codes : ""),
    ];
  }
  if (raw.rollup) {
    var med = Math.round(raw.code * 10) / 10;
    return [
      "Median status code " + med + " (" + cls.label + ")",
      "Rollup of an interval; individual codes are not kept",
    ];
  }
  var base =
    raw.cls === "err"
      ? "No valid status code (" + Math.round(raw.code) + ")"
      : "HTTP " + Math.round(raw.code) + " (" + cls.label + ")";
  return raw.count > 1
    ? [base, "Worst of " + raw.count + " checks in this interval"]
    : base;
}

/**
 * Apply the shared "Show peaks" state: the peak markers of a series follow its legend state.
 * Zoom range and data are untouched.
 */
function _syncPeaks(chart) {
  var datasets = chart.data.datasets;
  var peakIndexes = [];
  var series = [];
  datasets.forEach(function (d, i) {
    if (d._kind !== "peak") return;
    var msIndex = datasets.findIndex(function (m) {
      return m._kind === "ms" && m._srcKey === d._srcKey;
    });
    peakIndexes.push(i);
    series.push({
      msVisible: msIndex >= 0 && chart.isDatasetVisible(msIndex),
      hasPeakData: d.data.some(function (point) {
        return point.y !== null;
      }),
    });
  });
  var state = CombinedChartData.peakVisibility(series, chart._showPeaks);
  peakIndexes.forEach(function (datasetIndex, n) {
    chart.setDatasetVisibility(datasetIndex, state.visible[n]);
  });
}

// Re-rendering replaces the buttons; keep keyboard focus on the button at the same position.
function _rerenderCombinedLegend(chart, legendEl, activeBtn) {
  var index = Array.prototype.indexOf.call(legendEl.children, activeBtn);
  _renderCombinedLegend(chart, legendEl);
  var next = legendEl.children[index];
  if (next) next.focus();
}

function _combinedLegendLabel(dataset) {
  if (dataset._kind === "status") return "Status";
  var label =
    dataset._srcId === "http"
      ? "HTTP"
      : dataset._srcId === "ping"
        ? "Ping"
        : dataset._srcId === "tcp"
          ? "TCP"
          : dataset.label;
  if (dataset._srcKey && dataset._srcKey.indexOf("tcp:") === 0)
    label += " " + dataset._srcKey.slice(4);
  return label;
}

/**
 * Render the legend toggle buttons.
 * Buttons use aria-pressed so state is exposed without relying on color.
 */
function _renderCombinedLegend(chart, legendEl) {
  var palette = chart._palette;
  legendEl.textContent = "";
  chart.data.datasets.forEach(function (ds, i) {
    if (ds._kind === "peak") return;
    var btn = _el("button", "btn btn-sm combined-legend-btn");
    var label = _combinedLegendLabel(ds);
    btn.type = "button";
    btn.setAttribute("aria-label", label);
    btn.title = ds.label;
    btn.setAttribute(
      "aria-pressed",
      chart.isDatasetVisible(i) ? "true" : "false",
    );
    var color = ds._srcId ? _seriesColor(palette, ds) : palette["2xx"];
    btn.appendChild(
      _swatch(ds._kind === "status" ? "circle" : "line", color, ds._kind !== "status"),
    );
    btn.appendChild(_el("span", "", label));
    btn.addEventListener("click", function () {
      chart.setDatasetVisibility(i, !chart.isDatasetVisible(i));
      _syncPeaks(chart);
      chart.update("none");
      if (chart._renderText) chart._renderText();
      _rerenderCombinedLegend(chart, legendEl, btn);
    });
    legendEl.appendChild(btn);
  });
  if (chart._bandCount > 0) {
    var bandBtn = _el("button", "btn btn-sm combined-legend-btn");
    bandBtn.type = "button";
    var bandLabel = "Down (" + chart._bandSourceLabel + ")";
    bandBtn.setAttribute("aria-label", bandLabel);
    bandBtn.title = "Downtime (" + chart._bandSourceLabel + ")";
    bandBtn.setAttribute(
      "aria-pressed",
      chart._bandsVisible ? "true" : "false",
    );
    var sw = _swatch("band", palette.down, false);
    bandBtn.appendChild(sw);
    bandBtn.appendChild(_el("span", "", bandLabel));
    bandBtn.addEventListener("click", function () {
      chart._bandsVisible = !chart._bandsVisible;
      var ann = chart.options.plugins.annotation.annotations;
      Object.keys(ann).forEach(function (k) {
        if (k.indexOf("down") === 0) ann[k].display = chart._bandsVisible;
      });
      chart.update("none");
      if (chart._renderText) chart._renderText();
      _rerenderCombinedLegend(chart, legendEl, bandBtn);
    });
    legendEl.appendChild(bandBtn);
  }

}

function _summarizeSource(series, points) {
  var summary = CombinedChartData.summarize(series, [], []);
  var rawMax = Math.max.apply(
    null,
    points.map(function (p) {
      return p.max == null ? -Infinity : p.max;
    }),
  );
  if (isFinite(rawMax)) summary.max = rawMax;
  return summary;
}

// Keep the visible summary and canvas accessible name in sync with the displayed chart data.
function _renderCombinedText(canvas, input, view, chart) {
  var D = CombinedChartData;
  var visibleSrc = input.sources.filter(function (s) {
    var datasetIndex = chart
      ? chart.data.datasets.findIndex(function (dataset) {
          return dataset._kind === "ms" && dataset._srcKey === s.key;
        })
      : -1;
    return (
      view.summaries[s.key] &&
      view.summaries[s.key].median !== null &&
      (!chart ||
        (datasetIndex >= 0 && chart.isDatasetVisible(datasetIndex)))
    );
  })[0];
  var visibleSummary = D.summarize(
    visibleSrc ? view.series[visibleSrc.key] : [],
    !chart || _statusDatasetVisible(chart) ? view.statusBuckets : [],
    !chart || chart._bandsVisible ? view.bands : [],
  );
  if (visibleSrc && view.summaries[visibleSrc.key]) {
    var sourceSummary = view.summaries[visibleSrc.key];
    visibleSummary.min = sourceSummary.min;
    visibleSummary.median = sourceSummary.median;
    visibleSummary.max = sourceSummary.max;
  }
  var hasLatencyData = input.sources.some(function (source) {
    return view.summaries[source.key] && view.summaries[source.key].median !== null;
  });
  var text = visibleSrc
    ? D.describeSummary(visibleSrc.label, visibleSummary)
    : D.describeSummary("Response time", visibleSummary).replace(
        "No response time data in this range",
        hasLatencyData ? "No visible response-time series" : "No response time data in this range",
      );
  if (chart && input.statusEnabled && !_statusDatasetVisible(chart))
    text += "; HTTP status layer hidden";
  if (chart && chart._bandCount > 0 && !chart._bandsVisible)
    text += "; downtime bands hidden";
  if (
    chart &&
    chart._showPeaks === false &&
    chart.data.datasets.some(function (d) {
      return d._kind === "peak";
    })
  )
    text += "; peaks hidden";
  var rawDays =
    (typeof pageConfig !== "undefined" && pageConfig.rawRetentionDays) || 30;
  var rollupNote =
    input.range.from.getTime() < Date.now() - rawDays * 86400000
      ? " Data older than " + rawDays + " days uses stored rollups; raw latency extremes and individual status codes are unavailable there."
      : "";
  if (input.statusRawOnly)
    rollupNote += " Status markers cover retained raw checks only.";
  if (view.zoomed) rollupNote += " Figures describe the zoomed range.";
  canvas.setAttribute("role", "img");
  canvas.setAttribute(
    "aria-label",
    "Response time and HTTP status over time. " + text + ".",
  );
  var summaryEl = document.getElementById("combined-summary");
  if (summaryEl) summaryEl.textContent = text + "." + rollupNote;
}

// Original annotation objects are shared with the legend toggle, so restoring them must re-apply the current state
function _syncDownVisibility(annotations, visible) {
  Object.keys(annotations).forEach(function (key) {
    if (key.indexOf("down") === 0) annotations[key].display = visible;
  });
}

/** Wire the single "Show peaks" switch; it is hidden when no series has peak data. */
function _bindPeaksSwitch(chart, datasets, peaksEl) {
  var input = peaksEl && peaksEl.querySelector("input");
  if (!input) return;
  peaksEl.hidden = !datasets.some(function (d) {
    return (
      d._kind === "peak" &&
      d.data.some(function (point) {
        return point.y !== null;
      })
    );
  });
  input.checked = chart._showPeaks;
  input.onchange = function () {
    chart._showPeaks = input.checked;
    _syncPeaks(chart);
    chart.update("none");
    chart._renderText();
  };
}

function _beginCombinedChartBuild(canvasId) {
  var chart =
    typeof appState !== "undefined" && appState.charts
      ? appState.charts.get(canvasId)
      : null;
  if (!chart) return 1;
  if (chart._cancelZoomLoader) chart._cancelZoomLoader();
  chart._buildGeneration = (chart._buildGeneration || 0) + 1;
  return chart._buildGeneration;
}

function _setCombinedZoomNote(chart, message) {
  if (typeof document === "undefined") return;
  var noteEl = document.getElementById("combined-status-note");
  if (!noteEl) return;
  noteEl.textContent = [chart._statusNoteBase, message].filter(Boolean).join(" ");
  if (message) {
    noteEl.setAttribute("role", "status");
    noteEl.setAttribute("aria-live", "polite");
  }
}

function _createCombinedZoomLoader(
  chart,
  canvasId,
  input,
  datasets,
  fromMs,
  toMs,
  onView,
  buildGeneration,
) {
  var original = datasets.map(function (d) {
    return d.data;
  });
  var originalAnnotations = Object.assign(
    {},
    chart.config.options.plugins.annotation.annotations,
  );
  var originalBandCount = chart._bandCount;
  var generation =
    buildGeneration === undefined ? chart._buildGeneration : buildGeneration;
  var serial = 0;
  var controller = null;
  function cancelPending() {
    clearTimeout(chart._zoomTimer);
    chart._zoomTimer = null;
    serial += 1;
    if (controller) controller.abort();
    controller = null;
  }
  chart._cancelZoomLoader = cancelPending;
  function restoreLoadView() {
    chart.data.datasets.forEach(function (d, i) {
      d.data = original[i];
    });
    var annotations = chart.config.options.plugins.annotation.annotations;
    Object.keys(annotations).forEach(function (key) {
      delete annotations[key];
    });
    Object.assign(annotations, originalAnnotations);
    _styleAnnotations(annotations, chart._palette);
    _syncDownVisibility(
      annotations,
      chart._bandsVisible,
    );
    chart._bandCount = originalBandCount;
    _syncPeaks(chart);
    chart.update("none");
    chart._renderLegend();
    _setCombinedZoomNote(chart, "");
    if (onView) onView(null);
  }
  chart._restoreLoadView = restoreLoadView;
  return function () {
    cancelPending();
    var current = serial;
    var start = Math.max(fromMs, chart.scales.x.min);
    var end = Math.min(toMs, chart.scales.x.max);
    if (end <= start || end - start >= (toMs - fromMs) * 0.95) {
      restoreLoadView();
      return;
    }
    chart._zoomTimer = setTimeout(async function () {
      if (
        chart._buildGeneration !== generation ||
        appState.charts.get(canvasId) !== chart
      )
        return;
      controller = new AbortController();
      var width = chart.chartArea?.width || chart.canvas.clientWidth || 900;
      var requests = input.sources.map(function (src) {
        var metric =
          src.key.indexOf("tcp:") === 0
            ? "tcp_ms_" + src.key.split(":")[1]
            : src.id + "_ms";
        return fetchDisplayMetricRange(
          pageConfig.targetId,
          metric,
          start,
          end,
          width,
          controller.signal,
        );
      });
      if (input.statusEnabled) {
        requests.push(
          fetchDisplayMetricRange(
            pageConfig.targetId,
            "http_status",
            start,
            end,
            width,
            controller.signal,
          ),
        );
      }
      requests.push(
        fetchDisplayMetricRange(
          pageConfig.targetId,
          input.upSourceLabel.toLowerCase() + "_up",
          start,
          end,
          width,
          controller.signal,
        ),
      );
      try {
        var results = await Promise.all(requests);
        if (
          current !== serial ||
          chart._buildGeneration !== generation ||
          appState.charts.get(canvasId) !== chart
        )
          return;
        var view = {
          summaries: {},
          series: {},
          statusBuckets: [],
          bands: [],
          zoomed: true,
        };
        input.sources.forEach(function (src, i) {
          var result = results[i];
          var minGapMs = result.bucket_seconds
            ? result.bucket_seconds * 3000
            : undefined;
          var series = CombinedChartData.toGappedSeries(
            result.points,
            minGapMs,
          );
          view.series[src.key] = series;
          view.summaries[src.key] = _summarizeSource(series, result.points);
          chart.data.datasets.forEach(function (d) {
            if (d._srcKey !== src.key) return;
            if (d._kind === "ms") d.data = series;
            if (d._kind === "peak")
              d.data = CombinedChartData.toPeakSeries(result.points);
          });
        });
        var ann = chart.options.plugins.annotation.annotations;
        var statusIndex = input.sources.length;
        if (input.statusEnabled) {
          if (chart.options.scales.yStatus) {
            var status = results[statusIndex];
            var buckets = CombinedChartData.bucketStatus(
              status.points,
              start,
              end,
              1,
            );
            view.statusBuckets = buckets;
            var statusDataset = chart.data.datasets.find(function (d) {
              return d._kind === "status";
            });
            if (statusDataset)
              statusDataset.data = buckets.map(function (b) {
                return {
                  x: b.x,
                  y: 0.5,
                  end: b.end,
                  code: b.code,
                  cls: b.cls,
                  count: b.count,
                  codeCounts: b.codeCounts,
                  rollup: false,
                };
              });
            Object.keys(ann).forEach(function (key) {
              if (key.indexOf("status") === 0) delete ann[key];
            });
            Object.assign(ann, _statusStripAnnotations(buckets, chart._palette));
          }
          statusIndex += 1;
        }
        var upResult = results[statusIndex];
        var bands = CombinedChartData.buildDownBands(
          upResult.points,
          upResult.bucket_seconds * 1000,
        );
        Object.keys(ann).forEach(function (key) {
          if (key.indexOf("down") === 0) delete ann[key];
        });
        Object.assign(
          ann,
          _downAnnotations(bands, chart._palette, chart._bandsVisible),
        );
        chart._bandCount = bands.length;
        view.bands = bands;
        _syncPeaks(chart);
        chart.update("none");
        chart._renderLegend();
        _setCombinedZoomNote(chart, "");
        if (onView) onView(view);
      } catch (error) {
        if (
          error.name !== "AbortError" &&
          current === serial &&
          chart._buildGeneration === generation &&
          appState.charts.get(canvasId) === chart
        ) {
          console.warn("Could not load zoom detail:", error);
          _setCombinedZoomNote(
            chart,
            "Zoom details could not be loaded. Displayed values may be incomplete for this range; reset zoom to return to the overview.",
          );
        }
      }
    }, 300);
  };
}

function _configureCombinedAxes(options, width) {
  var compact = width < 500;
  options.layout.padding.right = compact ? 4 : 12;
  options.layout.padding.left = compact ? 2 : 6;
  options.scales.x.ticks.maxTicksLimit = compact ? 3 : undefined;
  options.scales.x.ticks.font.size = compact ? 10 : 12;
  options.scales.y.ticks.maxTicksLimit = compact ? 4 : undefined;
  options.scales.y.ticks.font.size = compact ? 10 : 12;
  options.scales.y.ticks.padding = compact ? 3 : 8;
  options.scales.y.title.display = !compact;
  if (options.scales.yStatus) options.scales.yStatus.title.display = !compact;
}

function _bindCombinedPointKeys(canvas) {
  if (canvas._combinedPointKeysBound) return;
  canvas._combinedPointKeysBound = true;
  canvas.tabIndex = 0;
  var announcement = document.createElement("span");
  announcement.id = "combined-point-announcement";
  announcement.className = "visually-hidden";
  announcement.setAttribute("role", "status");
  announcement.textContent = "Use left and right arrow keys to inspect response-time points.";
  canvas.parentNode.appendChild(announcement);
  canvas.setAttribute("aria-describedby", announcement.id);
  canvas.addEventListener("click", function (event) {
    if (event.pointerType !== "touch" &&
      !(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) return;
    var chart = Chart.getChart(canvas);
    if (!chart) return;
    var hit = chart.getElementsAtEventForMode(event, "point", { intersect: true }, false)
      .find(function (item) {
        return chart.data.datasets[item.datasetIndex]._kind === "ms";
      });
    if (!hit) return;
    var active = [{ datasetIndex: hit.datasetIndex, index: hit.index }];
    var element = chart.getDatasetMeta(hit.datasetIndex).data[hit.index];
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, { x: element.x, y: element.y });
    chart.update("none");
  });
  canvas.addEventListener("keydown", function (event) {
    if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    var chart = Chart.getChart(canvas);
    if (!chart) return;
    var points = [];
    chart.data.datasets.forEach(function (ds, datasetIndex) {
      if (ds._kind !== "ms" || !chart.isDatasetVisible(datasetIndex)) return;
      ds.data.forEach(function (point, index) {
        if (point.y == null || !isFinite(point.x) || !isFinite(point.y)) return;
        if (point.x < chart.scales.x.min || point.x > chart.scales.x.max) return;
        points.push({ datasetIndex: datasetIndex, index: index, point: point });
      });
    });
    if (!points.length) return;
    event.preventDefault();
    points.sort(function (a, b) {
      return a.point.x - b.point.x || a.datasetIndex - b.datasetIndex;
    });
    var current = canvas._combinedPointIndex == null ? -1 : canvas._combinedPointIndex;
    var next = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1
      : event.key === "ArrowLeft" ? (current < 0 ? points.length - 1 : Math.max(0, current - 1))
        : Math.min(points.length - 1, current + 1);
    canvas._combinedPointIndex = next;
    var selected = points[next];
    var active = [{ datasetIndex: selected.datasetIndex, index: selected.index }];
    var element = chart.getDatasetMeta(selected.datasetIndex).data[selected.index];
    chart.setActiveElements(active);
    if (chart.tooltip && element)
      chart.tooltip.setActiveElements(active, { x: element.x, y: element.y });
    chart.update("none");
    var label = chart.data.datasets[selected.datasetIndex].label;
    announcement.textContent = label + ", " +
      new Date(selected.point.x).toLocaleString(navigator.language || undefined) + ": " +
      formatChartValue("ms", Math.round(selected.point.y)) +
      ". Point " + (next + 1) + " of " + points.length + ".";
  });
}

/**
 * Build or rebuild the combined chart.
 * @param {string} canvasId
 * @param {object} input - { sources:[{key,id,label,points,error,sla,dash}], statusPoints, statusError, upPoints,
 *   upSourceLabel, statusEnabled, range:{from:Date,to:Date} }
 */
function buildCombinedChart(canvasId, input) {
  var canvas = document.getElementById(canvasId);
  if (!canvas) return;
  var buildGeneration = _beginCombinedChartBuild(canvasId);

  var D = window.CombinedChartData;
  var theme = appState.theme;
  var palette = _combinedPalette(theme);
  var emptyEl = document.getElementById("combined-empty");
  var plotEl = document.getElementById("combined-plot");
  var peaksEl = document.getElementById("combined-peaks");
  var legendEl = document.getElementById("combined-legend");
  var summaryEl = document.getElementById("combined-summary");

  var fromMs = input.range.from.getTime();
  var toMs = input.range.to.getTime();
  var statusBuckets = D.bucketStatus(
    input.statusPoints,
    fromMs,
    toMs,
    Math.max(
      1,
      Math.ceil((toMs - fromMs) / (1000 * (input.statusBucketSeconds || 1800))),
    ),
  );
  var bands = D.buildDownBands(
    input.upPoints,
    input.upBucketSeconds ? input.upBucketSeconds * 1000 : undefined,
  );

  var datasets = [];
  var summaries = {};
  var fullSeries = {};
  var firstWithData = true;
  var slaLines = [];
  var plotted = input.sources.map(function (src) {
    var minGapMs = src.bucketSeconds ? src.bucketSeconds * 3000 : undefined;
    var series = D.toGappedSeries(src.points, minGapMs);
    summaries[src.key] = _summarizeSource(series, src.points);
    fullSeries[src.key] = series;
    return { src: src, series: series };
  });
  // Only plotted series take a slot, so ports without data never cause color collisions.
  var plottedKeys = plotted.filter(function (row) {
    return row.series.length;
  }).map(function (row) {
    return row.src.key;
  });
  var slots = D.seriesSlots(plottedKeys);
  var singleMetric = plottedKeys.length === 1;
  plotted.forEach(function (row) {
    var src = row.src;
    var series = row.series;
    if (!series.length) return;
    if (src.sla) slaLines.push({ index: datasets.length, sla: src.sla });
    var slot = slots[src.key];
    var seriesInfo = { _slot: slot.slot, _singleMetric: singleMetric };
    var color = _seriesColor(palette, seriesInfo);
    datasets.push({
      label: src.label,
      data: series,
      _kind: "ms",
      _srcId: src.id,
      _srcKey: src.key,
      _slot: seriesInfo._slot,
      _singleMetric: singleMetric,
      yAxisID: "y",
      borderColor: color,
      backgroundColor: createAreaGradient(color, 0.18),
      fill: true,
      spanGaps: false,
      tension: 0,
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5,
      pointHitRadius: 12,
      hidden: !firstWithData,
    });
    // Peaks are single events on the response-time axis: markers plus stems, never a curve.
    datasets.push({
      label: src.label + " peak response time",
      data: D.toPeakSeries(src.points),
      _kind: "peak",
      _srcId: src.id,
      _srcKey: src.key,
      _slot: seriesInfo._slot,
      yAxisID: "y",
      type: "line",
      showLine: false,
      clip: 0,
      borderColor: palette.peak,
      backgroundColor: palette.peak,
      borderWidth: 1,
      pointStyle: "triangle",
      pointRotation: 0,
      pointRadius: 4,
      pointHoverRadius: 5,
      pointHitRadius: 6,
      pointBackgroundColor: palette.peak,
      pointBorderColor: palette.peak,
      hidden: !firstWithData,
    });
    firstWithData = false;
  });
  if (statusBuckets.length) {
    // The colored strip is drawn as box annotations (_statusStripAnnotations); this dataset
    // keeps invisible centered points so _statusTooltip still fires on direct hover.
    datasets.push({
      label: "HTTP status",
      _kind: "status",
      yAxisID: "yStatus",
      type: "line",
      showLine: false,
      data: statusBuckets.map(function (b) {
        return {
          x: b.x,
          y: 0.5,
          end: b.end,
          code: b.code,
          cls: b.cls,
          count: b.count,
          codeCounts: b.codeCounts,
          rollup: b.rollup,
        };
      }),
      pointRadius: 0,
      pointHoverRadius: 0,
      pointHitRadius: 8,
    });
  }

  var hasMs = datasets.some(function (d) {
    return d._kind === "ms";
  });
  var hasAny = hasMs || statusBuckets.length > 0;
  var allFailed =
    input.sources.length > 0 &&
    input.sources.every(function (s) {
      return s.error;
    });

  // Visible empty / error state instead of an empty canvas.
  if (!hasAny) {
    _destroyExisting(canvasId);
    if (plotEl) plotEl.hidden = true;
    if (legendEl) legendEl.textContent = "";
    if (peaksEl) peaksEl.hidden = true;
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.querySelector('[data-role="message"]').textContent = allFailed
        ? "The measurements could not be loaded. Reload the page or try again later."
        : "No measurements in the selected range. Pick a longer range or wait for the next check.";
    }
    if (summaryEl) summaryEl.textContent = "No data to display.";
    var staleNote = document.getElementById("combined-status-note");
    if (staleNote) staleNote.textContent = "";
    canvas.setAttribute(
      "aria-label",
      "Response time and HTTP status over time. No data to display.",
    );
    return;
  }
  if (plotEl) plotEl.hidden = false;
  if (emptyEl) emptyEl.hidden = true;

  var zoomOpts = createZoomOptions(function (ch) {
    if (ch._reloadVisible) ch._reloadVisible();
  });
  var config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);
  config.data = { datasets: datasets };
  config.plugins = (config.plugins || []).concat([COMBINED_PEAK_STEMS]);
  var o = config.options;
  o.animation = _prefersReducedMotion() ? false : { duration: 250 };
  // ms/peak points keep direct point+intersect hover; the custom mode adds whole-width
  // reach for the status strip, whose colored segments span [x, end] but have points only at x.
  _registerCombinedInteraction();
  o.interaction = { mode: COMBINED_INTERACTION_MODE, intersect: true };
  o.scales.x.min = fromMs;
  o.scales.x.max = toMs;
  o.scales.y.stack = "combined";
  // Headroom keeps a capped peak triangle inside the plot area.
  o.scales.y.grace = "6%";
  o.scales.y.stackWeight = 11;
  o.scales.y.title = {
    display: true,
    text: CHART_UNITS.ms.axisTitle,
    color: theme.muted,
  };
  o.scales.y.ticks.callback = function (v) {
    if (!Number.isInteger(v)) return "";
    var titleShown = !!(this && this.options && this.options.title && this.options.title.display);
    return formatChartTick("ms", v, !titleShown);
  };
  // Keep a visible SLA line in view even when every measurement is far below it.
  o.scales.y.afterDataLimits = function (scale) {
    slaLines.forEach(function (l) {
      if (!scale.chart.isDatasetVisible(l.index)) return;
      var top = l.sla.value * 1.1;
      if (top > scale.max) scale.max = top;
    });
  };
  if (statusBuckets.length) {
    o.scales.yStatus = {
      type: "linear",
      position: "left",
      stack: "combined",
      stackWeight: 1,
      min: 0,
      max: 1,
      grid: { display: false },
      border: { display: true, color: theme.grid },
      ticks: { display: false },
      title: { display: true, text: "Status", color: theme.muted },
    };
  }
  _configureCombinedAxes(o, canvas.clientWidth || 900);
  o.onResize = function (chart, size) {
    _configureCombinedAxes(chart.options, size.width);
  };
  o.plugins.legend = { display: false };
  o.plugins.tooltip.callbacks.label = function (item) {
    if (item.dataset._kind === "status") return _statusTooltip(item);
    if (item.dataset._kind === "peak") {
      var peakLabel =
        " " + item.dataset.label + ": " + formatChartValue("ms", Math.round(item.raw.max));
      if (item.raw.median !== null)
        peakLabel += " (median " + formatChartValue("ms", Math.round(item.raw.median)) + ")";
      peakLabel +=
        " across " +
        item.raw.count +
        " stored value" +
        (item.raw.count === 1 ? "" : "s");
      return item.raw.capped
        ? peakLabel + "; drawn at the axis limit"
        : peakLabel;
    }
    return " " + item.dataset.label + ": " + formatChartValue("ms", Math.round(item.parsed.y));
  };

  var annotations = _downAnnotations(bands, palette, true);
  slaLines.forEach(function (l) {
    var a = createSlaAnnotation({ enabled: true }, l.sla.value, l.sla.label);
    if (!a) return;
    a.display = function (c) {
      return c.chart.isDatasetVisible(l.index);
    };
    annotations["sla" + l.index] = a;
  });
  if (statusBuckets.length)
    Object.assign(annotations, _statusStripAnnotations(statusBuckets, palette));
  _styleAnnotations(annotations, palette);
  // The shared line config sets the root `clip: false`, which the annotation plugin inherits unless
  // it is set here; without it bands and the status strip run over the axes.
  o.plugins.annotation = { clip: true, annotations: annotations };

  var chart = _createOrUpdateChart(canvasId, canvas, config);
  chart._buildGeneration = buildGeneration;
  chart._palette = palette;
  chart._skipSampling = true;
  chart._bandCount = bands.length;
  chart._bandsVisible = true;
  chart._bandSourceLabel = input.upSourceLabel || "primary check";
  chart._renderLegend = function () {
    var active =
      legendEl && legendEl.contains(document.activeElement)
        ? document.activeElement
        : null;
    if (active) _rerenderCombinedLegend(chart, legendEl, active);
    else _renderCombinedLegend(chart, legendEl);
  };
  chart._themeHook = function (t) {
    _themeCombined(chart, t);
  };
  var fullView = {
    summaries: summaries,
    series: fullSeries,
    statusBuckets: statusBuckets,
    bands: bands,
    zoomed: false,
  };
  chart._currentView = fullView;
  chart._renderText = function () {
    _renderCombinedText(canvas, input, chart._currentView, chart);
  };
  chart._reloadVisible = _createCombinedZoomLoader(
    chart,
    canvasId,
    input,
    datasets,
    fromMs,
    toMs,
    function (view) {
      chart._currentView = view || fullView;
      chart._renderText();
    },
    buildGeneration,
  );
  _registerChart(canvasId, chart, canvas, true);
  canvas._combinedPointIndex = null;
  _bindCombinedPointKeys(canvas);
  // An updated chart keeps the user's choice across rebuilds; a new one starts with peaks shown.
  chart._showPeaks = chart._showPeaks !== false;
  _syncPeaks(chart);
  chart.update("none");
  _bindPeaksSwitch(chart, datasets, peaksEl);
  chart._renderLegend();

  chart._renderText();
  var noteEl = document.getElementById("combined-status-note");
  if (noteEl) {
    noteEl.textContent = _statusNoteText(input, statusBuckets);
    chart._statusNoteBase = noteEl.textContent;
  }
}

// Note under the chart. A target without an HTTP check says nothing (the missing series is no news);
// the empty note is hidden by CSS (.combined-note:empty), so it takes no space.
function _statusNoteText(input, statusBuckets) {
  var text = !input.statusEnabled
    ? ""
    : input.statusError
      ? "HTTP status codes could not be loaded."
      : !statusBuckets.length
        ? "No HTTP status codes recorded in this range."
        : "";
  if (input.upError) text += " Downtime bands could not be loaded.";
  return text;
}

window.buildCombinedChart = buildCombinedChart;
