//
// app/static/js/charts/combined-chart-data.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Pure data helpers for the combined response-time / status-code chart.
 * No DOM or Chart.js access, so the logic can be unit-tested with node --test.
 * @module charts/combined-chart-data
 */
(function (root) {
  "use strict";

  /** Status classes ordered by severity; the shape is the non-color cue. */
  var STATUS_CLASSES = {
    "2xx": { key: "2xx", label: "2xx Success", shape: "circle", rank: 1 },
    info: { key: "info", label: "1xx Informational", shape: "star", rank: 2 },
    "3xx": { key: "3xx", label: "3xx Redirect", shape: "rect", rank: 3 },
    "4xx": {
      key: "4xx",
      label: "4xx Client error",
      shape: "triangle",
      rank: 4,
    },
    "5xx": { key: "5xx", label: "5xx Server error", shape: "rectRot", rank: 5 },
    err: {
      key: "err",
      label: "Error / no valid code",
      shape: "crossRot",
      rank: 6,
    },
  };

  /** Priority HTTP > Ping > TCP, mirrors app/utils/uptime_metric.py. */
  var SOURCES = [
    {
      id: "http",
      flag: "enableHttp",
      msMetric: "http_ms",
      upMetric: "http_up",
      label: "HTTP response time",
    },
    {
      id: "ping",
      flag: "enablePing",
      msMetric: "ping_ms",
      upMetric: "ping_up",
      label: "Ping RTT",
    },
    {
      id: "tcp",
      flag: "enableTcp",
      msMetric: "tcp_ms",
      upMetric: "tcp_up",
      label: "TCP connect time",
    },
  ];

  /**
   * Normalize a status value (rollup medians may be fractional) to a valid HTTP status code.
   * @param {*} value
   * @returns {number|null} rounded code in 100-599, otherwise null
   */
  function normalizeHttpStatusCode(value) {
    var code = Math.round(Number(value));
    return isFinite(code) && code >= 100 && code <= 599 ? code : null;
  }
  /**
   * Classify an HTTP status code. Rollup medians may be fractional; they are rounded.
   * @param {number} value
   * @returns {object} entry of STATUS_CLASSES
   */
  function classifyStatus(value) {
    var code = normalizeHttpStatusCode(value);
    if (code === null) return STATUS_CLASSES.err;
    // 1xx is informational only: neither a successful final result nor an error.
    if (code < 200) return STATUS_CLASSES.info;
    return STATUS_CLASSES[String(Math.floor(code / 100)) + "xx"];
  }

  /**
   * Enabled latency sources in priority order; the first one is the default selection.
   * @param {object} flags - pageConfig-like object (enableHttp, enablePing, enableTcp)
   * @returns {object[]}
   */
  function enabledSources(flags) {
    return SOURCES.filter(function (s) {
      return !!(flags && flags[s.flag]);
    });
  }

  /** Median of consecutive timestamp deltas (ms), 0 if fewer than two points. */
  function medianInterval(points) {
    var deltas = [];
    for (var i = 1; i < points.length; i++) {
      var d = points[i].x - points[i - 1].x;
      if (d > 0) deltas.push(d);
    }
    if (!deltas.length) return 0;
    deltas.sort(function (a, b) {
      return a - b;
    });
    return deltas[Math.floor(deltas.length / 2)];
  }

  function insertGaps(series, minGapMs) {
    var fixedGap = Number.isFinite(minGapMs);
    var threshold = fixedGap
      ? minGapMs
      : Math.max(3 * medianInterval(series), 120000);
    var out = [];
    for (var i = 0; i < series.length; i++) {
      if (i > 0 && series[i].x - series[i - 1].x > threshold) {
        out.push({ x: series[i - 1].x + 1, y: null });
      }
      out.push(series[i]);
    }
    return out;
  }

  /**
   * Convert API points to {x: ms, y} and insert a null point into every gap larger than
   * minGapMs when it is a finite number, otherwise max(3 x median interval, 120000 ms),
   * so Chart.js breaks the line instead of interpolating.
   * @param {{ts: string|number, value: number}[]} points
   * @param {number} [minGapMs] - Fixed gap threshold in ms
   * @returns {{x: number, y: number|null}[]}
   */
  function toGappedSeries(points, minGapMs) {
    var series = (points || [])
      .map(function (p) {
        return {
          x: new Date(p.ts).getTime(),
          y: Number(p.value),
          min: p.min,
          max: p.max,
          count: p.count,
          rollup: p.rollup,
        };
      })
      .filter(function (p) {
        return isFinite(p.x) && isFinite(p.y);
      })
      .sort(function (a, b) {
        return a.x - b.x;
      });
    return insertGaps(series, minGapMs);
  }

  /**
   * Build a gap-preserving maximum-response-time line from raw display buckets.
   * Rollups do not expose raw extrema, so those intervals stay null instead of implying data.
   * @param {object[]} points
   * @param {number} [minGapMs] - Fixed gap threshold in ms
   * @returns {object[]}
   */
  function toPeakSeries(points, minGapMs) {
    var series = (points || [])
      .map(function (point) {
        var x = new Date(point.ts).getTime();
        if (!isFinite(x)) return null;
        var value = Number(point.value);
        var max = point.max == null ? null : Number(point.max);
        if (max === null || !isFinite(max)) {
          return {
            x: x,
            y: null,
            count: Number(point.count) || 0,
            median: isFinite(value) ? value : null,
            outlier: false,
          };
        }
        return {
          x: x,
          y: max,
          count: Number(point.count) || 1,
          median: isFinite(value) ? value : null,
          outlier: isFinite(value) && max > value * 2,
        };
      })
      .filter(Boolean)
      .sort(function (a, b) {
        return a.x - b.x;
      });
    return insertGaps(series, minGapMs);
  }

  /**
   * Reduce status points to at most maxBuckets markers; per bucket the WORST (highest) code wins,
   * so a single 503 is never hidden by neighbouring 200s.
   * @param {{ts: string|number, value: number}[]} points
   * @param {number} fromMs - range start
   * @param {number} toMs - range end
   * @param {number} maxBuckets
   * @param {number} [rollupBeforeMs] - points older than this are rollup medians, not single checks
   * @returns {{x: number, end: number, code: number, cls: string, count: number, codeCounts: object, rollup: boolean}[]}
   *   Both paths yield the same model: [x, end) are explicit bucket bounds in ms.
   */
  function bucketStatus(points, fromMs, toMs, maxBuckets, rollupBeforeMs) {
    if (
      (points || []).some(function (p) {
        return p.bucket_end;
      })
    ) {
      return points
        .map(function (p) {
          var code = Number(p.value);
          return {
            x: new Date(p.ts).getTime(),
            end: new Date(p.bucket_end).getTime(),
            code: code,
            cls: classifyStatus(code).key,
            count: p.count,
            codeCounts: p.code_counts || {},
            rollup: false,
          };
        })
        .filter(function (p) {
          return isFinite(p.x) && isFinite(p.end) && isFinite(p.code);
        });
    }
    var valid = (points || [])
      .map(function (p) {
        return { x: new Date(p.ts).getTime(), v: Number(p.value) };
      })
      .filter(function (p) {
        return isFinite(p.x) && isFinite(p.v);
      })
      .sort(function (a, b) {
        return a.x - b.x;
      });
    if (!valid.length) return [];
    var span = Math.max(1, toMs - fromMs);
    var width = span / Math.max(1, maxBuckets);
    var buckets = new Map();
    valid.forEach(function (p) {
      var idx = Math.min(
        Math.max(Math.floor((p.x - fromMs) / width), 0),
        Math.max(1, maxBuckets) - 1,
      );
      var cls = classifyStatus(p.v);
      var codeKey = String(Math.round(p.v));
      var cur = buckets.get(idx);
      var isRollup = typeof rollupBeforeMs === "number" && p.x < rollupBeforeMs;
      if (!cur) {
        var start = fromMs + idx * width;
        var counts = {};
        counts[codeKey] = 1;
        buckets.set(idx, {
          x: start,
          end: start + width,
          code: p.v,
          cls: cls.key,
          count: 1,
          codeCounts: counts,
          rollup: isRollup,
        });
        return;
      }
      cur.count += 1;
      cur.codeCounts[codeKey] = (cur.codeCounts[codeKey] || 0) + 1;
      cur.rollup = cur.rollup || isRollup;
      var curRank = STATUS_CLASSES[cur.cls].rank;
      if (cls.rank > curRank || (cls.rank === curRank && p.v > cur.code)) {
        cur.code = p.v;
        cur.cls = cls.key;
      }
    });
    return Array.from(buckets.values()).sort(function (a, b) {
      return a.x - b.x;
    });
  }

  /**
   * Derive down intervals from a 0/1 availability series. A down run starts at the first
   * value < 0.5 and ends at the next up point (or one interval after the last down point).
   * @param {{ts: string|number, value: number}[]} points
   * @returns {{start: number, end: number}[]}
   */
  function buildDownBands(points, bucketMs) {
    var series = (points || [])
      .map(function (p) {
        return { x: new Date(p.ts).getTime(), y: Number(p.value) };
      })
      .filter(function (p) {
        return isFinite(p.x) && isFinite(p.y);
      })
      .sort(function (a, b) {
        return a.x - b.x;
      });
    var interval = bucketMs || medianInterval(series) || 60000;
    var bands = [];
    var start = null;
    var last = null;
    series.forEach(function (p) {
      if (start !== null && last !== null && p.x - last > interval * 3) {
        bands.push({ start: start, end: last + interval });
        start = null;
      }
      if (p.y < 0.5) {
        if (start === null) start = p.x;
        last = p.x;
      } else if (start !== null) {
        bands.push({ start: start, end: p.x });
        start = null;
      }
    });
    if (start !== null) bands.push({ start: start, end: last + interval });
    return bands;
  }

  /** Linear-interpolated percentile of a sorted numeric array. */
  function percentile(sorted, q) {
    if (!sorted.length) return null;
    var pos = (sorted.length - 1) * q;
    var lo = Math.floor(pos);
    var hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  /**
   * Text summary used for aria-label and the visible caption.
   * @param {{y: number|null}[]} msSeries - gapped latency series
   * @param {{code: number, cls: string, x: number}[]} statusBuckets
   * @param {{start: number, end: number}[]} bands
   * @returns {{min: number|null, median: number|null, max: number|null, lastCode: number|null, outages: number}}
   */
  function summarize(msSeries, statusBuckets, bands) {
    var values = (msSeries || [])
      .filter(function (p) {
        return p.y !== null;
      })
      .map(function (p) {
        return p.y;
      })
      .sort(function (a, b) {
        return a - b;
      });
    var last =
      statusBuckets && statusBuckets.length
        ? statusBuckets[statusBuckets.length - 1]
        : null;
    return {
      min: values.length ? values[0] : null,
      median: percentile(values, 0.5),
      max: values.length ? values[values.length - 1] : null,
      lastCode: last ? Math.round(last.code) : null,
      outages: bands ? bands.length : 0,
    };
  }

  /**
   * Human-readable one-line summary (English UI language of the app).
   * @returns {string}
   */
  function describeSummary(sourceLabel, s) {
    var parts = [];
    if (s.median !== null) {
      parts.push(
        sourceLabel +
          ": min " +
          Math.round(s.min) +
          " ms, median " +
          Math.round(s.median) +
          " ms, max " +
          Math.round(s.max) +
          " ms",
      );
    } else {
      parts.push("No response time data in this range");
    }
    parts.push(
      s.lastCode !== null
        ? "last HTTP status " + s.lastCode
        : "no HTTP status codes",
    );
    parts.push(s.outages + (s.outages === 1 ? " outage" : " outages"));
    return parts.join("; ");
  }

  /** SLA threshold for a latency source, or null (only HTTP and Ping have thresholds). */
  function slaFor(sourceId, slaConfig) {
    if (!slaConfig || !slaConfig.enabled) return null;
    var value =
      sourceId === "http"
        ? slaConfig.responseTimeMs
        : sourceId === "ping"
          ? slaConfig.pingLatencyMs
          : null;
    return typeof value === "number" && isFinite(value)
      ? { value: value, label: value + " ms" }
      : null;
  }

  /**
   * Series definitions for the latency lines. TCP with several ports becomes one series per
   * port (falls back to the aggregate when no per-port data exists).
   * @param {object} flags - enableHttp/enablePing/enableTcp
   * @param {string[]} tcpPorts
   * @param {object} results - { http, ping, tcp, tcpPorts: [{points,error}, ...] } fetch results
   * @param {object} slaConfig - pageConfig.slaConfig
   * @returns {{key: string, id: string, label: string, points: object[], error: boolean, sla: object|null, dash: number}[]}
   */
  function buildLatencySources(flags, tcpPorts, results, slaConfig) {
    var out = [];
    var perPort = results && results.tcpPorts;
    var perPortHasData = !!(
      perPort &&
      perPort.some(function (r) {
        return r && r.points && r.points.length;
      })
    );
    enabledSources(flags).forEach(function (src) {
      if (
        src.id === "tcp" &&
        tcpPorts &&
        tcpPorts.length > 1 &&
        perPortHasData
      ) {
        tcpPorts.forEach(function (port, i) {
          var r = perPort[i] || {};
          out.push({
            key: "tcp:" + port,
            id: "tcp",
            label: "TCP connect :" + port,
            points: r.points || [],
            bucketSeconds: r.bucket_seconds,
            error: !!r.error,
            sla: null,
            dash: i,
          });
        });
        return;
      }
      var res = (results && results[src.id]) || {};
      out.push({
        key: src.id,
        id: src.id,
        label: src.label,
        points: res.points || [],
        bucketSeconds: res.bucket_seconds,
        error: !!res.error,
        sla: slaFor(src.id, slaConfig),
        dash: 0,
      });
    });
    return out;
  }

  /** Number of categorical series colors (--ju-dv-1 … --ju-dv-8). */
  var SERIES_SLOT_COUNT = 8;
  var SERIES_BASE_SLOT = { http: 0, ping: 1, tcp: 2 };

  /**
   * Stable palette slot per series key. HTTP and Ping have fixed slots; TCP ports follow in
   * ascending port order, so a color never depends on legend visibility or on the order
   * the sources arrive in. Beyond the palette size the slot wraps and `lap` shifts the dash pattern.
   * @param {string[]} keys - series keys ("http", "ping", "tcp", "tcp:<port>")
   * @returns {Object<string, {slot: number, lap: number}>}
   */
  function seriesSlots(keys) {
    var ports = (keys || [])
      .filter(function (k) {
        return k.indexOf("tcp:") === 0;
      })
      .map(function (k) {
        return Number(k.slice(4));
      })
      .sort(function (a, b) {
        return a - b;
      });
    var slots = {};
    (keys || []).forEach(function (key) {
      var index =
        key.indexOf("tcp:") === 0
          ? SERIES_BASE_SLOT.tcp + ports.indexOf(Number(key.slice(4)))
          : SERIES_BASE_SLOT[key];
      if (index === undefined || index < 0) index = 0;
      slots[key] = {
        slot: index % SERIES_SLOT_COUNT,
        lap: Math.floor(index / SERIES_SLOT_COUNT),
      };
    });
    return slots;
  }

  /**
   * Visibility of the peak layer. One switch drives all peak lines; a series hidden in the
   * legend shows no peaks, and the right axis is only visible while a peak line is.
   * @param {{msVisible: boolean, hasPeakData: boolean}[]} series
   * @param {boolean} showPeaks
   * @returns {{visible: boolean[], axisVisible: boolean}}
   */
  function peakVisibility(series, showPeaks) {
    var visible = series.map(function (s) {
      return !!(showPeaks && s.msVisible && s.hasPeakData);
    });
    return {
      visible: visible,
      axisVisible: visible.some(Boolean),
    };
  }

  var api = {
    seriesSlots: seriesSlots,
    peakVisibility: peakVisibility,
    SERIES_SLOT_COUNT: SERIES_SLOT_COUNT,
    slaFor: slaFor,
    buildLatencySources: buildLatencySources,
    STATUS_CLASSES: STATUS_CLASSES,
    SOURCES: SOURCES,
    classifyStatus: classifyStatus,
    normalizeHttpStatusCode: normalizeHttpStatusCode,
    enabledSources: enabledSources,
    toGappedSeries: toGappedSeries,
    toPeakSeries: toPeakSeries,
    bucketStatus: bucketStatus,
    buildDownBands: buildDownBands,
    summarize: summarize,
    describeSummary: describeSummary,
  };
  root.CombinedChartData = api;
})(typeof window !== "undefined" ? window : globalThis);
