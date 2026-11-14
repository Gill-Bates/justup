//
// app/static/js/traceroute-map.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Traceroute Map - Leaflet-based network path visualization
 * Initializes interactive maps for traceroute fragments with dark mode support
 */
(function () {
	'use strict';

	// Flag image failed to load: show the country code instead (error does not bubble, so capture).
	document.addEventListener('error', function (event) {
		var img = event.target;
		if (!img || !img.classList || !img.classList.contains('country-flag-svg')) return;
		img.hidden = true;
		if (img.nextElementSibling) img.nextElementSibling.hidden = false;
	}, true);

	// Global Leaflet loading promise (singleton pattern to prevent race conditions)
	window._leafletLoadingPromise = window._leafletLoadingPromise || null;

	/**
	 * Ensure Leaflet library is loaded
	 * @returns {Promise} Resolves when Leaflet is ready
	 */
	function ensureLeaflet() {
		if (typeof window.L !== 'undefined') {
			return Promise.resolve();
		}

		if (window._leafletLoadingPromise) {
			return window._leafletLoadingPromise;
		}

		window._leafletLoadingPromise = new Promise(function (resolve, reject) {
			if (!document.querySelector('link[href*="leaflet"]')) {
				var css = document.createElement('link');
				css.rel = 'stylesheet';
				css.href = '/static/css/leaflet.css';
				document.head.appendChild(css);
			}

			var script = document.createElement('script');
			script.src = '/static/js/leaflet.js';
			script.onload = function () { resolve(); };
			script.onerror = function () {
				// Allow retry on next call by clearing the promise
				window._leafletLoadingPromise = null;
				script.remove();
				reject(new Error('Failed to load Leaflet'));
			};
			document.head.appendChild(script);
		});

		return window._leafletLoadingPromise;
	}

	/**
	 * Local basemap: Natural Earth 1:110m country outlines (public domain),
	 * vendored under /static/vendor/geo/. No tile server, no external request.
	 */
	var BASEMAP_URL = '/static/vendor/geo/world-110m.geojson';
	var _basemapPromise = null;

	/**
	 * Load the basemap GeoJSON once per page
	 * @returns {Promise<Object>} GeoJSON FeatureCollection
	 */
	function loadBasemap() {
		if (!_basemapPromise) {
			_basemapPromise = fetch(BASEMAP_URL, { credentials: 'same-origin' })
				.then(function (resp) {
					if (!resp.ok) throw new Error('Basemap HTTP ' + resp.status);
					return resp.json();
				})
				.catch(function (err) {
					_basemapPromise = null;  // allow retry on next map
					throw err;
				});
		}
		return _basemapPromise;
	}

	/**
	 * Legibility rules come from the server (route_map_rules.map_style(), data-map-style) so the web
	 * map and the PDF share them. This copy only serves a cached fragment that predates the attribute.
	 */
	var DEFAULT_STYLE = {
		palette: {
			light: { water: '#e4ecf4', land: '#f4f6f9', border: '#d3dce6', route: '#0b4fc0', halo: '#ffffff', label: '#6b7785', label_strong: '#1f2a37', label_halo: '#f4f6f9' },
			dark: { water: '#0c1726', land: '#152538', border: '#233853', route: '#7cc0ff', halo: '#07101c', label: '#7d90a8', label_strong: '#d6e2f0', label_halo: '#0c1726' }
		},
		line: { weight: 3, halo: 3, dash: '9 6', minArrowSegment: 64, markerGap: 3 },
		labels: { maxCities: 8, countryMaxZoom: 5, cityMinZoom: 4 },
		view: { minZoom: 2, maxZoom: 8, fitMaxZoom: 6, singlePointZoom: 5 },
		web: { arrowSpacing: 112 }
	};

	function readJsonAttribute(element, name, fallback) {
		try {
			var parsed = JSON.parse(element.dataset[name] || 'null');
			return parsed && typeof parsed === 'object' ? parsed : fallback;
		} catch (e) {
			console.warn('Invalid ' + name + ' JSON for map:', e);
			return fallback;
		}
	}

	// Pre-measure fallback (DC-TOK-3): approximate glyph widths (px) of the 11px labels. They only decide which
	// names compete for space in placeLabels(); redraw() then measures each rendered label with
	// getBoundingClientRect() and re-checks every candidate position against the measured size, so a wrong
	// estimate never lets two drawn labels overlap. tests/js/traceroute-map-layout.test.mjs pins both parts.
	var CITY_CHAR_PX = 6;
	var COUNTRY_CHAR_PX = 7.5;
	var LABEL_HEIGHT_PX = 14;
	var LABEL_SEGMENT_STEP_PX = 12;

	/**
	 * Check whether a rectangle overlaps any already placed rectangle
	 * @param {Object} rect - {x1, y1, x2, y2}
	 * @param {Array} placed - Accepted rectangles
	 * @returns {boolean} True when overlapping
	 */
	function collides(rect, placed) {
		for (var i = 0; i < placed.length; i++) {
			var o = placed[i];
			if (rect.x1 < o.x2 && rect.x2 > o.x1 && rect.y1 < o.y2 && rect.y2 > o.y1) return true;
		}
		return false;
	}

	/** Small rectangles along a pixel segment, so a name never lies on the chain. */
	function segmentObstacles(a, b, halfWidth) {
		var length = Math.hypot(b.x - a.x, b.y - a.y);
		var steps = Math.max(1, Math.ceil(length / LABEL_SEGMENT_STEP_PX));
		var out = [];
		for (var i = 0; i <= steps; i++) {
			var x = a.x + (b.x - a.x) * i / steps;
			var y = a.y + (b.y - a.y) * i / steps;
			out.push({ x1: x - halfWidth, x2: x + halfWidth, y1: y - halfWidth, y2: y + halfWidth });
		}
		return out;
	}

	/**
	 * Decide which names are drawn at a zoom level. Priority: hop cities, countries, orientation cities.
	 * Countries show at overview zoom (<= countryMaxZoom), places from cityMinZoom on; a name is dropped
	 * when it would overlap a marker, the chain or a name already placed.
	 * @param {{countries: Array, cities: Array}} labels - map_labels from the server
	 * @param {number} zoom - Current map zoom
	 * @param {{countryMaxZoom: number, cityMinZoom: number}} rules
	 * @param {function(number, number):{x: number, y: number}} project - lat, lon -> pixel at this zoom
	 * @param {Array} obstacles - Rectangles {x1, y1, x2, y2} of markers and chain
	 * @returns {Array} Placed names {name, kind, role, lat, lon, rect, side}
	 */
	function placeLabels(labels, zoom, rules, project, obstacles) {
		var placed = (obstacles || []).slice();
		var out = [];
		var queue = [];
		if (zoom >= rules.cityMinZoom) {
			(labels.cities || []).filter(function (c) { return c.role === 'path'; }).forEach(function (c) { queue.push({ item: c, kind: 'city' }); });
		}
		if (zoom <= rules.countryMaxZoom) {
			(labels.countries || []).forEach(function (c) { queue.push({ item: c, kind: 'country' }); });
		}
		if (zoom >= rules.cityMinZoom) {
			(labels.cities || []).filter(function (c) { return c.role !== 'path'; }).forEach(function (c) { queue.push({ item: c, kind: 'city' }); });
		}
		queue.forEach(function (entry) {
			var item = entry.item;
			var pt = project(item.lat, item.lon);
			if (!pt || !isFinite(pt.x) || !isFinite(pt.y)) return;
			var width = item.name.length * (entry.kind === 'country' ? COUNTRY_CHAR_PX : CITY_CHAR_PX);
			var half = LABEL_HEIGHT_PX / 2;
			var options = entry.kind === 'country'
				? [{ side: 'center', x1: pt.x - width / 2, x2: pt.x + width / 2, y1: pt.y - half, y2: pt.y + half }]
				: [
					{ side: 'right', x1: pt.x + 22, x2: pt.x + 22 + width, y1: pt.y - half, y2: pt.y + half },
					{ side: 'left', x1: pt.x - 22 - width, x2: pt.x - 22, y1: pt.y - half, y2: pt.y + half },
					{ side: 'below', x1: pt.x - width / 2, x2: pt.x + width / 2, y1: pt.y + 20, y2: pt.y + 20 + LABEL_HEIGHT_PX },
					{ side: 'above', x1: pt.x - width / 2, x2: pt.x + width / 2, y1: pt.y - 20 - LABEL_HEIGHT_PX, y2: pt.y - 20 }
				];
			for (var i = 0; i < options.length; i++) {
				if (collides(options[i], placed)) continue;
				placed.push(options[i]);
				out.push({ name: item.name, kind: entry.kind, role: item.role || '', lat: item.lat, lon: item.lon, rect: options[i], side: options[i].side, pt: pt });
				return;
			}
		});
		return out;
	}

	/** Pixel distance from a marker center to its edge along the unit direction (ux, uy). */
	function markerEdgeDistance(size, ux, uy) {
		var rx = size.w / 2;
		var ry = size.h / 2;
		if (!(rx > 0) || !(ry > 0)) return 0;
		// Use the full rectangle bounds; rounded corners may be empty, but a route line must not
		// pass underneath a marker's hit box where visual and pointer ownership become ambiguous.
		return Math.min(ux ? rx / Math.abs(ux) : Infinity, uy ? ry / Math.abs(uy) : Infinity);
	}

	/**
	 * The visible part of a chain link: it starts and ends markerGap px outside the two markers.
	 * @returns {{a: Object, b: Object, length: number}|null} null when the markers touch
	 */
	function trimChainSegment(a, b, sizeA, sizeB, gap) {
		var dx = b.x - a.x;
		var dy = b.y - a.y;
		var distance = Math.hypot(dx, dy);
		if (!(distance > 0)) return null;
		var ux = dx / distance;
		var uy = dy / distance;
		var startOffset = markerEdgeDistance(sizeA, ux, uy) + gap;
		var endOffset = markerEdgeDistance(sizeB, ux, uy) + gap;
		var length = distance - startOffset - endOffset;
		if (!(length > 1)) return null;
		return {
			a: { x: a.x + ux * startOffset, y: a.y + uy * startOffset },
			b: { x: b.x - ux * endOffset, y: b.y - uy * endOffset },
			length: length
		};
	}

	/**
	 * Project a hop chain into a continuous copy of the wrapped map world.
	 * @param {Array} points
	 * @param {function(Object, number):Object} project - Hop -> projected pixel point at a fixed zoom
	 * @param {number} worldWidth - Width of one projected world
	 * @returns {Array} Projected hops with the nearest deterministic world copy
	 */
	function unwrapProjectedRoute(points, project, worldWidth) {
		var previousX = null;
		return points.map(function (point, index) {
			var projected = project(point, index);
			if (!projected || !isFinite(projected.x) || !isFinite(projected.y)) return null;
			var x = projected.x;
			if (previousX !== null && worldWidth > 0 && isFinite(worldWidth)) {
				x += Math.round((previousX - x) / worldWidth) * worldWidth;
			}
			previousX = x;
			return { point: point, index: index, x: x, y: projected.y,
				worldCopy: worldWidth > 0 ? Math.round((x - projected.x) / worldWidth) : 0 };
		});
	}

	/** Deterministic quadratic curve samples in screen pixels. */
	function sampleQuadraticBezier(start, control, end, steps) {
		var count = Math.max(2, Math.floor(steps || 12));
		var points = [];
		for (var i = 0; i <= count; i++) {
			var t = i / count;
			var oneMinus = 1 - t;
			points.push({
				x: oneMinus * oneMinus * start.x + 2 * oneMinus * t * control.x + t * t * end.x,
				y: oneMinus * oneMinus * start.y + 2 * oneMinus * t * control.y + t * t * end.y
			});
		}
		return points;
	}

	function quadraticControlPoint(start, end, offset) {
		var dx = end.x - start.x;
		var dy = end.y - start.y;
		var length = Math.hypot(dx, dy) || 1;
		return { x: (start.x + end.x) / 2 - dy / length * offset,
			y: (start.y + end.y) / 2 + dx / length * offset };
	}

	/** Point and forward tangent at a distance along a sampled screen-space polyline. */
	function pointAndTangentAt(points, distance) {
		if (!points || points.length < 2) return null;
		var remaining = Math.max(0, distance);
		for (var i = 1; i < points.length; i++) {
			var a = points[i - 1];
			var b = points[i];
			var dx = b.x - a.x;
			var dy = b.y - a.y;
			var length = Math.hypot(dx, dy);
			if (!length) continue;
			if (remaining <= length || i === points.length - 1) {
				var ratio = Math.min(1, remaining / length);
				return { x: a.x + dx * ratio, y: a.y + dy * ratio, tx: dx / length, ty: dy / length, segment: i - 1 };
			}
			remaining -= length;
		}
		return null;
	}

	function polylineLength(points) {
		var length = 0;
		for (var i = 1; i < points.length; i++) length += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y);
		return length;
	}

	function outsideMarkerBox(point, center, size, gap) {
		return point.x < center.x - size.w / 2 - gap || point.x > center.x + size.w / 2 + gap
			|| point.y < center.y - size.h / 2 - gap || point.y > center.y + size.h / 2 + gap;
	}

	/** Distances for repeated direction arrows along a long visible segment. */
	function directionArrowDistances(length, spacing) {
		if (!(length > 0)) return [];
		var count = Math.max(1, Math.ceil(length / Math.max(24, spacing || DEFAULT_STYLE.web.arrowSpacing)));
		return Array.from({ length: count }, function (_, index) { return (index + 0.5) * length / count; });
	}

	/** Cut a sampled curve outside both marker boxes while preserving its shape and total visible length. */
	function trimPolylineSegment(points, sizeA, sizeB, gap) {
		var total = polylineLength(points);
		if (!(total > 0)) return null;
		var first = pointAndTangentAt(points, 0);
		var last = pointAndTangentAt(points, total);
		var startOffset = markerEdgeDistance(sizeA, first.tx, first.ty) + gap;
		var endOffset = markerEdgeDistance(sizeB, -last.tx, -last.ty) + gap;
		for (var startPass = 0; startPass < 256 && startOffset < total
			&& !outsideMarkerBox(pointAndTangentAt(points, startOffset), points[0], sizeA, gap); startPass++) {
			startOffset += 2;
		}
		for (var endPass = 0; endPass < 256 && endOffset < total
			&& !outsideMarkerBox(pointAndTangentAt(points, total - endOffset), points[points.length - 1], sizeB, gap); endPass++) {
			endOffset += 2;
		}
		if (!(total - startOffset - endOffset > 1)) return null;
		var start = pointAndTangentAt(points, startOffset);
		var endDistance = total - endOffset;
		var end = pointAndTangentAt(points, endDistance);
		var visible = [{ x: start.x, y: start.y }];
		var walked = 0;
		for (var i = 1; i < points.length; i++) {
			var segmentLength = Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y);
			walked += segmentLength;
			if (walked > startOffset && walked < endDistance) visible.push(points[i]);
		}
		visible.push({ x: end.x, y: end.y });
		return { points: visible, a: visible[0], b: visible[visible.length - 1], length: endDistance - startOffset };
	}

	/** Rectangles sampled along a polyline for label and marker collision checks. */
	function polylineObstacles(points, halfWidth, spacing) {
		if (!points || points.length < 2) return [];
		var step = Math.max(2, spacing || LABEL_SEGMENT_STEP_PX);
		var out = [];
		for (var i = 1; i < points.length; i++) {
			var a = points[i - 1];
			var b = points[i];
			var length = Math.hypot(b.x - a.x, b.y - a.y);
			var count = Math.max(1, Math.ceil(length / step));
			for (var j = 0; j <= count; j++) {
				var t = j / count;
				var x = a.x + (b.x - a.x) * t;
				var y = a.y + (b.y - a.y) * t;
				out.push({ x1: x - halfWidth, x2: x + halfWidth, y1: y - halfWidth, y2: y + halfWidth });
			}
		}
		return out;
	}

	/** Choose the least-curved deterministic candidate that avoids known screen-space obstacles. */
	function chooseQuadraticCurve(start, end, offsets, obstacles, existingPaths, padding, bounds) {
		var chord = Math.hypot(end.x - start.x, end.y - start.y);
		var steps = Math.max(12, Math.ceil(chord / 16));
		var best = null;
		(offsets && offsets.length ? offsets : [0]).forEach(function (offset) {
			var control = quadraticControlPoint(start, end, offset);
			var points = sampleQuadraticBezier(start, control, end, steps);
			var trimmed = trimPolylineSegment(points, start, end, padding || 0);
			if (!trimmed) return;
			var rects = polylineObstacles(trimmed.points, 3, 8);
			var markerHits = 0;
			var pathHits = 0;
			var outside = bounds ? rects.filter(function (rect) {
				return rect.x1 < bounds.x1 || rect.x2 > bounds.x2 || rect.y1 < bounds.y1 || rect.y2 > bounds.y2;
			}).length : 0;
			(obstacles || []).forEach(function (obstacle) {
				if (rects.some(function (rect) { return collides(rect, obstacle); })) markerHits++;
			});
			(existingPaths || []).forEach(function (path) {
				if (rects.some(function (rect) { return collides(rect, path); })) pathHits++;
			});
			var score = markerHits * 1000000 + outside * 1000000 + pathHits * 10000 + Math.abs(offset) * 10 + (trimmed.length - chord);
			if (!best || score < best.score) best = { points: trimmed.points, length: trimmed.length, a: trimmed.a, b: trimmed.b,
				control: control, offset: offset, markerHits: markerHits, pathHits: pathHits, outside: outside,
				startOffset: trimmed.startOffset, startTangent: trimmed.startTangent, markerSize: trimmed.markerSize, score: score };
		});
		return best;
	}

	/** Lat/lon of every chain node: the fit shows the whole chain, outliers included. */
	function chainBounds(points) {
		var previousLon = null;
		return points.filter(function (p) { return isFinite(p.lat) && isFinite(p.lon); })
			.map(function (p) {
				var lon = p.lon;
				if (previousLon !== null) lon += Math.round((previousLon - lon) / 360) * 360;
				previousLon = lon;
				return [p.lat, lon];
			});
	}

	function shiftGeoJSONWorld(geojson, offset) {
		function shiftCoordinates(value) {
			if (!Array.isArray(value)) return value;
			if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') {
				var shifted = value.slice();
				shifted[0] += offset;
				return shifted;
			}
			return value.map(shiftCoordinates);
		}
		return {
			type: geojson.type,
			features: (geojson.features || []).map(function (feature) {
				return Object.assign({}, feature, { geometry: Object.assign({}, feature.geometry, {
					coordinates: shiftCoordinates(feature.geometry.coordinates)
				}) });
			})
		};
	}

	/**
	 * Choose one direction arrow for every visible route segment. On a dashed segment the arrow
	 * communicates hop order only; the dash still communicates that the geographic path is unknown.
	 * A segment too short for a clear arrow must be folded into a region marker, whose popup lists
	 * the hops in order, before this helper is called.
	 * @param {number} count - Number of points (segments = count - 1)
	 * @param {number|null} selection - Selected segment index, or null for all
	 * @param {function(number):number} [lengthAt] - Visible pixel length of segment i (default: all visible)
	 * @returns {number[]} Segment indices that should carry an arrow
	 */
	function computeArrowSegments(count, selection, lengthAt) {
		var out = [];
		if (count < 2) return out;
		function eligible(i) {
			return typeof lengthAt !== 'function' || lengthAt(i) > 0;
		}
		if (selection !== null && selection !== undefined) {
			if (selection >= 0 && selection < count - 1 && eligible(selection)) out.push(selection);
			return out;
		}
		for (var i = 0; i < count - 1; i++) {
			if (eligible(i)) out.push(i);
		}
		return out;
	}

	/** Visible hop label of a map point: its hop number, or "a–b" for a merged run of consecutive hops. */
	function pointLabel(point) {
		return point._hop_range ? point._hop_range.replace('-', '–') : String(point.hop || '?');
	}

	/**
	 * Roles are decided by the server (map_role); the client only reads them. An estimate is an
	 * approximate position: a hollow chain node whose links are dashed.
	 */
	function isEstimate(point) {
		return Boolean(point) && point.map_role === 'estimate';
	}

	/** True when the route line leaving this point is not an established geographic path. */
	function isDashedSegment(from) {
		return from.segment_to_next === 'dashed';
	}

	/** The nodes of the route chain, in hop order: every located point, estimates and destination included. */
	function routePointsOf(points) {
		return points.slice();
	}

	function pointContainsHop(point, hop) {
		if (!Number.isFinite(hop)) return false;
		if (!point._hop_range) return Number(point.hop) === hop;
		var bounds = parseHopRange(point._hop_range);
		return bounds !== null && hop >= bounds[0] && hop <= bounds[1];
	}
	/** Hop range carried by a table row or timeline button (data-hop-first / data-hop-last); NaN when absent. */
	function elementHopRange(el) {
		return { first: Number(el.dataset.hopFirst), last: Number(el.dataset.hopLast) };
	}
	/** Parse a "first-last" hop range string into [first, last]; null when malformed. */
	function parseHopRange(text) {
		var bounds = String(text).split('-').map(Number);
		return bounds.length === 2 && isFinite(bounds[0]) && isFinite(bounds[1]) ? bounds : null;
	}

	/**
	 * Kind and visible label of one marker group. "D" is shown only for a point the server flagged
	 * as the destination, never for the last mapped hop; hops of a group are listed explicitly.
	 * @param {Object} group - Group from groupNearbyHopPoints
	 * @param {number} startHop - Number of the route's first hop (NaN when unknown)
	 * @param {boolean} startIsMapped - True when the first hop is on the map
	 * @returns {Object} {kind, visibleLabel, labels, isStart, isDestination, isApproximate, startMember, destinationMember}
	 */
	function describeMarkerGroup(group, startHop, startIsMapped) {
		var startMember = startIsMapped
			? group.members.find(function (member) { return pointContainsHop(member.point, startHop); })
			: null;
		var destinationMember = group.members.find(function (member) { return Boolean(member.point._destination); });
		var isStart = Boolean(startMember);
		var isDestination = Boolean(destinationMember);
		var approximateMembers = group.members.filter(function (member) {
			var point = member.point;
			return Boolean(point._approximate || point.accuracy_class === 'approximate' || point.loc_precision === 'approximate');
		});
		var isApproximate = group.isRegionAggregate ? approximateMembers.length === group.members.length : approximateMembers.length > 0;
		var isBothEndpoints = isStart && isDestination;
		var labels = group.members.map(function (member) { return pointLabel(member.point); });
		labels = labels.filter(function (label, index) { return labels.indexOf(label) === index; });
		var otherLabels = labels.filter(function (label) {
			var first = Number(label.split(/[–-]/, 1)[0]);
			if (isStart && pointContainsHop(startMember.point, first)) return false;
			if (isDestination && pointContainsHop(destinationMember.point, first)) return false;
			return true;
		});
		var estimateMembers = group.members.filter(function (member) { return isEstimate(member.point); });
		var isEstimateGroup = group.isRegionAggregate ? estimateMembers.length === group.members.length : estimateMembers.length > 0;
		var cityNames = Array.from(new Set(group.members.map(function (member) { return member.point.city; }).filter(Boolean)));
		var countryCodes = Array.from(new Set(group.members.map(function (member) { return member.point.country; }).filter(Boolean)));
		var hasPlaceName = cityNames.length > 0 || countryCodes.length > 0;
		var placeLabel = cityNames.length === 1
			? cityNames[0] + (countryCodes.length === 1 ? ' · ' + countryCodes[0] : '')
			: (countryCodes.length === 1 ? countryCodes[0] + ' route area' : 'Dense route area');
		if (!cityNames.length && !countryCodes.length) placeLabel = labels.join('·');
		var kind = isBothEndpoints ? 'both' : (isStart ? 'start' : (isDestination ? 'destination' : (group.isRegionAggregate ? 'region' : (isEstimateGroup ? 'estimate' : (group.members[0].index === 0 ? 'first-mapped' : 'hop')))));
		var visibleLabel;
		if (isBothEndpoints) visibleLabel = 'S/D';
		else if (group.isRegionAggregate) visibleLabel = (isStart ? 'S · ' : (isDestination ? 'D · ' : ''))
			+ (isEstimateGroup ? '~' : '') + group.regionLabel;
		else if (isStart) visibleLabel = hasPlaceName ? 'S · ' + placeLabel : ['S'].concat(otherLabels).join('·');
		else if (isDestination) visibleLabel = hasPlaceName ? placeLabel + ' · D' : otherLabels.concat(['D']).join('·');
		else if (isEstimateGroup) visibleLabel = '~' + placeLabel;  // "~" keeps the estimate legible without color
		else visibleLabel = placeLabel;
		return {
			kind: kind, visibleLabel: visibleLabel, labels: labels, isStart: isStart, isDestination: isDestination,
			isApproximate: isApproximate, isEstimate: isEstimateGroup,
			startMember: startMember, destinationMember: destinationMember
		};
	}

	function groupNearbyHopPoints(points, project, radius) {
		var groups = [];
		points.forEach(function (point, index) {
			if (!point || point._interpolated) return;
			var projected = project(point, index);
			if (!projected || !isFinite(projected.x) || !isFinite(projected.y)) return;
			// Only collapse consecutive visits. A later return to the same city must
			// remain a separate route node so the map does not imply a shortcut. An estimate
			// never shares a marker with a route hop, so its uncertainty stays visible.
			var group = groups[groups.length - 1];
			if (group && (isEstimate(point) || group.members.some(function (member) { return isEstimate(member.point); })
				|| !group.members.every(function (member) {
					return Math.hypot(member.projected.x - projected.x, member.projected.y - projected.y) <= radius;
				}))) group = null;
			if (!group) {
				group = { members: [], center: { x: projected.x, y: projected.y }, displayCenter: null };
				groups.push(group);
			}
			group.members.push({ point: point, index: index, projected: projected });
			group.center = group.members.reduce(function (sum, member) {
				return { x: sum.x + member.projected.x / group.members.length, y: sum.y + member.projected.y / group.members.length };
			}, { x: 0, y: 0 });
		});
		return groups;
	}

	function fanOutOverlappingHopGroups(groups, radius) {
		var parents = groups.map(function (_, index) { return index; });
		function root(index) {
			while (parents[index] !== index) {
				parents[index] = parents[parents[index]];
				index = parents[index];
			}
			return index;
		}
		function unite(left, right) {
			var leftRoot = root(left);
			var rightRoot = root(right);
			if (leftRoot !== rightRoot) parents[rightRoot] = leftRoot;
		}
		function markerRectsOverlap(left, right) {
			var leftW = left.markerWidth || Math.min(32, radius * 2);
			var leftH = left.markerHeight || Math.min(32, radius * 2);
			var rightW = right.markerWidth || Math.min(32, radius * 2);
			var rightH = right.markerHeight || Math.min(32, radius * 2);
			return left.center.x - leftW / 2 - 4 < right.center.x + rightW / 2 + 4
				&& left.center.x + leftW / 2 + 4 > right.center.x - rightW / 2 - 4
				&& left.center.y - leftH / 2 - 4 < right.center.y + rightH / 2 + 4
				&& left.center.y + leftH / 2 + 4 > right.center.y - rightH / 2 - 4;
		}
		for (var i = 0; i < groups.length; i++) {
			for (var j = i + 1; j < groups.length; j++) {
				if (markerRectsOverlap(groups[i], groups[j])) {
					unite(i, j);
				}
			}
		}
		var clusters = new Map();
		groups.forEach(function (group, index) {
			var key = root(index);
			if (!clusters.has(key)) clusters.set(key, []);
			clusters.get(key).push(group);
		});
		var clusterIndex = 0;
		clusters.forEach(function (cluster) {
			if (cluster.length === 1) {
				cluster[0].displayCenter = cluster[0].center;
				cluster[0].overlapRegion = cluster[0].members.length > 1 ? 'route-region-' + clusterIndex++ : null;
				return;
			}
			var center = cluster.reduce(function (sum, group) {
				return { x: sum.x + group.center.x / cluster.length, y: sum.y + group.center.y / cluster.length };
			}, { x: 0, y: 0 });
			cluster.sort(function (left, right) { return left.members[0].index - right.members[0].index; });
			var columns = Math.ceil(Math.sqrt(cluster.length));
			var rows = Math.ceil(cluster.length / columns);
			var spacingX = Math.max(radius + 8, Math.max.apply(null, cluster.map(function (group) { return group.markerWidth || radius * 2; })) + 12);
			var spacingY = Math.max(radius + 8, Math.max.apply(null, cluster.map(function (group) { return group.markerHeight || radius * 2; })) + 12);
			var region = 'route-region-' + clusterIndex++;
			cluster.forEach(function (group, index) {
				var row = Math.floor(index / columns);
				var column = index % columns;
				if (row % 2) column = columns - column - 1;
				group.displayCenter = {
					x: center.x + (column - (columns - 1) / 2) * spacingX,
					y: center.y + (row - (rows - 1) / 2) * spacingY
				};
				group.overlapRegion = region;
			});
		});
		return groups;
	}

	/** Replace every dense overview cluster with one location marker; its popup lists the hops. */
	function aggregateOverlapRegions(groups) {
		var regions = new Map();
		var output = [];
		groups.forEach(function (group) {
			if (!group.overlapRegion) { output.push(group); return; }
			if (!regions.has(group.overlapRegion)) regions.set(group.overlapRegion, []);
			regions.get(group.overlapRegion).push(group);
		});
		regions.forEach(function (regionGroups, regionId) {
			var members = regionGroups.reduce(function (all, group) { return all.concat(group.members); }, [])
				.sort(function (left, right) { return left.index - right.index; });
			var center = regionGroups.reduce(function (sum, group) {
				return { x: sum.x + group.center.x / regionGroups.length, y: sum.y + group.center.y / regionGroups.length };
			}, { x: 0, y: 0 });
			var displayCenter = regionGroups.reduce(function (sum, group) {
				return { x: sum.x + group.displayCenter.x / regionGroups.length, y: sum.y + group.displayCenter.y / regionGroups.length };
			}, { x: 0, y: 0 });
			var cities = Array.from(new Set(members.map(function (member) { return member.point.city; }).filter(Boolean)));
			var countries = Array.from(new Set(members.map(function (member) { return member.point.country; }).filter(Boolean)));
			var regionLabel = cities.length === 1 ? cities[0] + (countries.length === 1 ? ' · ' + countries[0] : '')
				: (countries.length === 1 ? countries[0] + ' route area' : 'Dense route area');
			output.push({ members: members, center: center, displayCenter: displayCenter, overlapRegion: regionId,
				isRegionAggregate: true, regionLabel: regionLabel, markerWidth: Math.max(84, regionLabel.length * 8 + 18), markerHeight: 32 });
		});
		return output.sort(function (left, right) { return left.members[0].index - right.members[0].index; });
	}

	/**
	 * Label layer: draws the names that placeLabels() accepts. It is redrawn with the markers and the
	 * chain (obstacles) after every zoom, so a name never lies on either of them.
	 * @param {Object} map - Leaflet map
	 * @param {Object} labels - map_labels from the server
	 * @param {Object} rules - style.labels
	 */
	function createLabelLayer(map, labels, rules) {
		map.createPane('labels');
		var pane = map.getPane('labels');
		pane.style.zIndex = 250;  // above basemap (200), below the chain (390) and markers
		pane.style.pointerEvents = 'none';
		var group = window.L.layerGroup().addTo(map);

		function redraw(obstacles, project) {
			group.clearLayers();
			var z = map.getZoom();
			if (typeof z !== 'number' || !isFinite(z) || !labels) return;
			var placed = placeLabels(labels, z, rules, project || function (lat, lon) { return map.project([lat, lon], z); }, obstacles || []);
			var occupied = (obstacles || []).slice();
			var mapRect = map.getContainer().getBoundingClientRect();
			var pixelOrigin = map.getPixelOrigin();
			function layerRect(rect) {
				var first = map.containerPointToLayerPoint([rect.left - mapRect.left, rect.top - mapRect.top]);
				var last = map.containerPointToLayerPoint([rect.right - mapRect.left, rect.bottom - mapRect.top]);
				return { x1: first.x + pixelOrigin.x, y1: first.y + pixelOrigin.y,
					x2: last.x + pixelOrigin.x, y2: last.y + pixelOrigin.y };
			}
			placed.forEach(function (item) {
				var el = document.createElement('span');
				el.setAttribute('aria-hidden', 'true');
				var text = document.createElement('span');
				text.className = 'tr-label-text';
				text.textContent = item.name;
				el.appendChild(text);
				var cls = 'tr-label tr-label-' + item.kind + (item.role === 'path' ? ' tr-label-route' : '');
				var marker = window.L.marker([item.lat, item.lon], {
					pane: 'labels',
					interactive: false,
					keyboard: false,
					icon: window.L.divIcon({ html: el, className: cls, iconSize: [0, 0], iconAnchor: [0, 0] })
				}).addTo(group);
				var measured = text.getBoundingClientRect();
				var labelWidth = measured.width || item.rect.x2 - item.rect.x1;
				var labelHeight = measured.height || item.rect.y2 - item.rect.y1;
				var half = labelHeight / 2;
				var candidates = item.kind === 'country'
					? [{ side: 'center', x1: item.pt.x - labelWidth / 2, y1: item.pt.y - half }]
					: [
						{ side: 'right', x1: item.pt.x + 22, y1: item.pt.y - half },
						{ side: 'left', x1: item.pt.x - 22 - labelWidth, y1: item.pt.y - half },
						{ side: 'below', x1: item.pt.x - labelWidth / 2, y1: item.pt.y + 20 },
						{ side: 'above', x1: item.pt.x - labelWidth / 2, y1: item.pt.y - 20 - labelHeight }
					];
			candidates.sort(function (left, right) {
				return Number(right.side === item.side) - Number(left.side === item.side);
			});
			var placedActual = false;
			for (var i = 0; i < candidates.length; i++) {
				var candidate = candidates[i];
				var expected = { x1: candidate.x1, y1: candidate.y1, x2: candidate.x1 + labelWidth, y2: candidate.y1 + labelHeight };
				if (collides(expected, occupied)) continue;
				text.style.left = (candidate.x1 - item.pt.x) + 'px';
				text.style.top = (candidate.y1 - item.pt.y) + 'px';
				var actual = text.getBoundingClientRect();
				var actualInLayer = layerRect(actual);
				if (actual.left < mapRect.left || actual.right > mapRect.right || actual.top < mapRect.top || actual.bottom > mapRect.bottom
					|| collides(actualInLayer, occupied)) continue;
				occupied.push(actualInLayer);
				placedActual = true;
				break;
			}
			if (!placedActual) marker.remove();
			});
		}

		return { redraw: redraw };
	}

	/** Palette of the active theme (shared rules, route_map_rules.PALETTE). */
	function themePalette(style) {
		var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
		return style.palette[dark ? 'dark' : 'light'];
	}

	/**
	 * Apply the palette to the map container, the basemap layer and the CSS variables of the frame
	 * (labels, legend swatches).
	 * @param {HTMLElement} container
	 * @param {Object|null} layer - Leaflet GeoJSON layer
	 * @param {Object} style - map_style
	 */
	function applyBasemapTheme(container, layers, style) {
		var c = themePalette(style);
		container.style.backgroundColor = c.water;
		var frame = container.parentElement;
		if (frame && frame.style) {
			frame.style.setProperty('--tr-route-color', c.route);
			frame.style.setProperty('--tr-label-color', c.label);
			frame.style.setProperty('--tr-label-strong', c.label_strong);
			frame.style.setProperty('--tr-label-halo', c.label_halo);
		}
		(layers || []).forEach(function (layer) {
			layer.setStyle({ fillColor: c.land, fillOpacity: 1, color: c.border, weight: 0.5 });
		});
	}

	/**
	 * Cleanup map resources on a container
	 * @param {HTMLElement} container
	 */
	function cleanupMapContainer(container) {
		if (container._leaflet_map) {
			try {
				container._leaflet_map.stop();
				container._leaflet_map.off();
				container._leaflet_map.remove();
			} catch (e) {
				// Map might already be partially destroyed, ignore errors
			}
			container._leaflet_map = null;
		}
		if (container._themeListener) {
			window.removeEventListener('theme-changed', container._themeListener);
			container._themeListener = null;
		}
		if (container._htmxHandler) {
			document.body.removeEventListener('htmx:beforeSwap', container._htmxHandler);
			container._htmxHandler = null;
		}
		if (container._unlinkTimeline) {
			container._unlinkTimeline();
			container._unlinkTimeline = null;
		}
		if (container._unlinkRows) {
			container._unlinkRows();
			container._unlinkRows = null;
		}
		if (container._unlinkSelection) {
			container._unlinkSelection();
			container._unlinkSelection = null;
		}
		if (container._resetControl) {
			container._resetControl.remove();
			container._resetControl = null;
		}
		if (container._pagehideHandler) {
			window.removeEventListener('pagehide', container._pagehideHandler);
			container._pagehideHandler = null;
		}
		if (container._resizeObserver) {
			container._resizeObserver.disconnect();
			container._resizeObserver = null;
		}
		if (container._resizeTimeout) {
			clearTimeout(container._resizeTimeout);
			container._resizeTimeout = null;
		}
		_activeContainers.delete(container);
	}

	/**
	 * Initialize a single traceroute map
	 * @param {HTMLElement} mapContainer
	 */
	function initMap(mapContainer) {
		if (!mapContainer || !window.L) return;

		// Read and validate geo_points from data-attribute (XSS-safe, HTML-escaped by Jinja2)
		var geoPoints;
		try {
			geoPoints = JSON.parse(mapContainer.dataset.geoPoints || '[]');
		} catch (e) {
			console.error('Invalid geoPoints JSON for map:', mapContainer.dataset.mapId, e);
			return;
		}

		if (!geoPoints || geoPoints.length < 1) return;

		// Filter valid points (check for NaN/Infinity)
		var validPoints = geoPoints.filter(function (p) {
			return typeof p.lat === 'number' && typeof p.lon === 'number'
				&& isFinite(p.lat) && isFinite(p.lon);
		});

		if (validPoints.length < 1) {
			console.warn('No valid geo points for map:', mapContainer.dataset.mapId);
			return;
		}

		// The route line joins the server-assigned route points in hop order; estimates only get markers.
		var routePoints = routePointsOf(validPoints);

		var style = readJsonAttribute(mapContainer, 'mapStyle', DEFAULT_STYLE);
		var mapLabels = readJsonAttribute(mapContainer, 'mapLabels', { countries: [], cities: [] });

		// Create map using container element directly (prevents duplicate ID issues)
		var map = window.L.map(mapContainer, {
			scrollWheelZoom: true,
			attributionControl: false,
			zoomSnap: 1,
			zoomDelta: 1,
			minZoom: style.view.minZoom,
			// 1:110m outlines get coarse beyond this zoom
			maxZoom: style.view.maxZoom,
			// One spare world copy either side allows a shortest route across ±180°.
			maxBounds: [[-85.05112878, -540], [85.05112878, 540]],
			maxBoundsViscosity: 1.0
		});
		mapContainer._leaflet_map = map;

		// Local vector basemap in its own pane below the route
		map.createPane('basemap');
		map.getPane('basemap').style.zIndex = 200;
		var basemapLayers = [];
		applyBasemapTheme(mapContainer, basemapLayers, style);
		loadBasemap()
			.then(function (geojson) {
				if (mapContainer._leaflet_map !== map) return;  // map destroyed meanwhile
				var bounds = chainBounds(validPoints);
				var minLon = Math.min.apply(null, bounds.map(function (point) { return point[1]; }));
				var maxLon = Math.max.apply(null, bounds.map(function (point) { return point[1]; }));
			var firstWorld = Math.floor((minLon + 180) / 360);
			var lastWorld = Math.ceil((maxLon - 180) / 360);
			for (var world = firstWorld; world <= lastWorld; world++) {
				var worldData = world === 0 ? geojson : shiftGeoJSONWorld(geojson, world * 360);
				basemapLayers.push(window.L.geoJSON(worldData, { pane: 'basemap', interactive: false }).addTo(map));
			}
				applyBasemapTheme(mapContainer, basemapLayers, style);
			})
			.catch(function (err) {
				console.warn('Basemap could not be loaded:', err);
			});

		// Names come from the server selection (map_labels); the layer redraws with markers and chain.
		var labelLayer = createLabelLayer(map, mapLabels, style.labels);

		var latlngs = chainBounds(validPoints);

		// The chain: segment i joins the display positions of node i and i+1, trimmed to stop markerGap px
		// before each marker. Every segment is a halo line under the colored line; both live in panes
		// between the basemap/labels and the markers. A dashed segment marks an estimated or unknown
		// path (never a physical link). Route length is bounded (<=30 hops).
		map.createPane('chainHalo');
		map.getPane('chainHalo').style.zIndex = 380;
		map.createPane('chain');
		map.getPane('chain').style.zIndex = 390;
		map.createPane('regionLeaders');
		map.getPane('regionLeaders').style.zIndex = 395;
		var regionLeaderLayer = window.L.layerGroup().addTo(map);
		var chainLayer = window.L.layerGroup().addTo(map);
		var chainRecords = [];  // {index, dashed, length, a, b, pl, halo} in pixels at the current zoom
		var colors = themePalette(style);
		var routeSelection = null;  // segment index, or null for the whole route

		function baseOpacity() { return 0.95; }

		// Emphasize the selected segment and recede the rest; null restores all.
		function applySegmentStyles() {
			chainRecords.forEach(function (rec) {
				var selected = routeSelection === null || rec.index === routeSelection;
				rec.pl.setStyle({ opacity: selected ? baseOpacity() : 0.2 });
				rec.halo.setStyle({ opacity: selected ? 0.9 : 0.15 });
				var element = rec.pl.getElement();
				if (element) element.classList.toggle('is-selected', routeSelection !== null && selected);
			});
		}

		function segmentTooltip(from, dashed) {
			var el = document.createElement('span');
			var text = dashed
				? 'Arrow shows hop order; geographic path is estimated or unknown'
				: 'Arrow shows hop order between reliable positions; curve is schematic';
			if (dashed && from.segment_note) text += ' · ' + from.segment_note;
			el.textContent = text;
			return el;
		}

		// nodes: node index -> {x, y, w, h} pixel position and marker size at the current zoom
		function drawChain(nodes, zoom) {
			chainLayer.clearLayers();
			chainRecords = [];
			var markerRects = [];
			var seenCenters = new Set();
			nodes.forEach(function (node) {
				var centerKey = Math.round(node.x) + ':' + Math.round(node.y);
				if (seenCenters.has(centerKey)) return;
				seenCenters.add(centerKey);
				markerRects.push({ x1: node.x - node.w / 2 - 4, x2: node.x + node.w / 2 + 4,
					y1: node.y - node.h / 2 - 4, y2: node.y + node.h / 2 + 4 });
			});
			var usedCorridors = new Map();
			var routedPaths = [];
			for (var i = 0; i < routePoints.length - 1; i++) {
				var from = nodes.get(i);
				var to = nodes.get(i + 1);
				if (!from || !to) continue;
				if (from.x === to.x && from.y === to.y) continue; // the region marker owns this local edge
				var startKey = Math.round(from.x) + ':' + Math.round(from.y);
				var endKey = Math.round(to.x) + ':' + Math.round(to.y);
				var corridorKey = startKey < endKey ? startKey + '|' + endKey : endKey + '|' + startKey;
				var usage = usedCorridors.get(corridorKey) || { forward: 0, reverse: 0 };
				var forward = startKey < endKey;
				var rank = forward ? usage.forward++ : usage.reverse++;
				usedCorridors.set(corridorKey, usage);
				var baseOffset = rank === 0 ? 0 : 18 + rank * 18;
				var offsets = [baseOffset];
				for (var lane = 1; lane <= 12; lane++) offsets.push(baseOffset + lane * 24, baseOffset - lane * 24);
				var origin = map.getPixelOrigin();
				var size = map.getSize();
				var bounds = { x1: origin.x + 8, y1: origin.y + 8, x2: origin.x + size.x - 8, y2: origin.y + size.y - 8 };
				var seg = chooseQuadraticCurve(from, to, offsets, markerRects, routedPaths, style.line.markerGap, bounds);
				if (!seg) continue;
				var dashed = isDashedSegment(routePoints[i]);
				var latlngs2 = seg.points.map(function (point) { return map.unproject([point.x, point.y], zoom); });
				var pathClass = 'traceroute-route-segment traceroute-route-segment--' + i;
				var halo = window.L.polyline(latlngs2, {
					pane: 'chainHalo', color: colors.halo, weight: style.line.weight + style.line.halo, opacity: 0.9,
					lineCap: 'butt', lineJoin: 'round', dashArray: dashed ? style.line.dash : null,
					className: pathClass, interactive: false
				}).addTo(chainLayer);
				var pl = window.L.polyline(latlngs2, {
					pane: 'chain', color: colors.route, weight: style.line.weight, opacity: baseOpacity(),
					lineCap: 'butt', lineJoin: 'round', dashArray: dashed ? style.line.dash : null,
					className: pathClass
				}).addTo(chainLayer);
				pl.bindTooltip(segmentTooltip(routePoints[i], dashed), { sticky: true, direction: 'top', className: 'traceroute-hop-tooltip' });
				(function (segmentIndex) {
					pl.on('click', function () { selectSegment(segmentIndex); });
				})(i);
				chainRecords.push({ index: i, dashed: dashed, length: seg.length, a: seg.a, b: seg.b,
					points: seg.points, pl: pl, halo: halo });
				routedPaths.push(polylineObstacles(seg.points, style.line.weight + style.line.halo, 8));
			}
			applySegmentStyles();
		}

		var routeStartHop = Number.parseInt(mapContainer.dataset.routeStartHop, 10);
		var routeStartIsMapped = mapContainer.dataset.routeStartIsMapped === 'true';
		var destinationHost = mapContainer.dataset.routeDestinationHost || '';
		var destinationIp = mapContainer.dataset.routeDestinationIp || '';

		function markerIcon(label, kind, isCluster, isApproximate) {
			var el = document.createElement('span');
			el.className = 'traceroute-hop-marker traceroute-hop-marker--' + kind
				+ (isCluster || kind === 'estimate' ? ' traceroute-hop-marker--cluster' : '')
				+ (isApproximate ? ' traceroute-hop-marker--approximate' : '');
			el.textContent = label;
			var width = Math.max(32, label.length * 8 + 16);
			return window.L.divIcon({
				html: el,
				className: 'traceroute-hop-marker-icon',
				iconSize: [width, 32],
				iconAnchor: [width / 2, 16]
			});
		}

		function hopSpan(point) {
			var bounds = point._hop_range ? parseHopRange(point._hop_range) : null;
			if (bounds) return bounds;
			var n = Number(point.hop);
			return isFinite(n) ? [n, n] : null;
		}

		// Table row <-> map marker correspondence (hover and keyboard focus only highlight)
		var markerRecords = [];
		var activeMarkerGroups = [];

		function overlaps(spans, first, last) {
			return spans.some(function (span) { return rangesOverlap(first, last, span[0], span[1]); });
		}

		function setLinkedRows(spans, on) {
			var root = mapContainer.closest('.traceroute-diagnostics') || document;
			root.querySelectorAll('.tr-hop-row').forEach(function (row) {
				var range = elementHopRange(row);
				if (!on) { row.classList.remove('is-linked'); return; }
				if (isFinite(range.first) && isFinite(range.last) && overlaps(spans, range.first, range.last)) row.classList.add('is-linked');
			});
		}

		function setLinkedMarkers(first, last, on) {
			markerRecords.forEach(function (rec) {
				var el = rec.marker.getElement();
				if (!el) return;
				el.classList.toggle('is-linked', Boolean(on) && overlaps(rec.spans, first, last));
			});
		}

		function bindSafeTooltip(marker, label, permanent) {
			var el = document.createElement('span');
			el.textContent = label;
			marker.bindTooltip(el, {
				permanent: permanent,
				direction: 'top',
				offset: [0, -12],
				className: 'traceroute-hop-tooltip'
			});
		}

		// Arrow centered on the visible curve; its angle follows the local route tangent.
		function addDirectionMarker(rec, distance, arrowIndex) {
			var zoom = map.getZoom();
			var tangent = pointAndTangentAt(rec.points, distance);
			if (!tangent) return null;
			var angle = Math.atan2(tangent.ty, tangent.tx) * 180 / Math.PI;
			var midpoint = map.unproject([tangent.x, tangent.y], zoom);
			var containerPoint = map.latLngToContainerPoint(midpoint);
			var mapSize = map.getSize();
			if (containerPoint.x < 10 || containerPoint.y < 10 || containerPoint.x > mapSize.x - 10 || containerPoint.y > mapSize.y - 10) return null;
			var arrow = document.createElement('span');
			arrow.className = 'traceroute-direction-arrow';
			arrow.setAttribute('aria-hidden', 'true');
			arrow.textContent = '➤';
			arrow.style.transform = 'rotate(' + angle + 'deg)';
			arrow.dataset.segmentIndex = String(rec.index);
			arrow.dataset.arrowIndex = String(arrowIndex);
			return window.L.marker(midpoint, {
				icon: window.L.divIcon({
					html: arrow,
					className: 'traceroute-direction-icon',
					iconSize: [18, 18],
					iconAnchor: [9, 9]
				}),
				interactive: false,
				keyboard: false
			});
		}

		// Each visible edge carries one arrow. On dashed paths the arrow indicates hop order only;
		// the dash continues to identify an unknown or estimated geographic path.
		var arrowLayer = window.L.layerGroup().addTo(map);

		function drawDirectionArrows() {
			arrowLayer.clearLayers();
			var zoom = map.getZoom();
			if (typeof zoom !== 'number' || !isFinite(zoom)) return;
			var byIndex = {};
			chainRecords.forEach(function (rec) { byIndex[rec.index] = rec; });
			var lengthAt = function (index) {
				var rec = byIndex[index];
				if (!rec) return 0;
				var points = [map.latLngToContainerPoint(map.unproject(rec.a, zoom)), map.latLngToContainerPoint(map.unproject(rec.b, zoom))];
				var minX = Math.min(points[0].x, points[1].x), maxX = Math.max(points[0].x, points[1].x);
				var minY = Math.min(points[0].y, points[1].y), maxY = Math.max(points[0].y, points[1].y);
				var size = map.getSize();
				var visible = maxX >= 0 && minX <= size.x && maxY >= 0 && minY <= size.y;
				return visible ? rec.length : 0;
			};
			computeArrowSegments(routePoints.length, routeSelection, lengthAt).forEach(function (index) {
				var rec = byIndex[index];
				directionArrowDistances(rec.length, style.web && style.web.arrowSpacing).forEach(function (distance, arrowIndex) {
					var marker = addDirectionMarker(rec, distance, arrowIndex);
					if (marker) arrowLayer.addLayer(marker);
				});
			});
		}

		// Segment selection. Highlights one segment, dims the rest, links the two
		// endpoint hop rows, and redraws arrows for the selection. No hover auto-
		// animate or jump: selection is an explicit click or keyboard activation.
		function selectSegment(index) {
			if (!isFinite(index) || index < 0 || index >= routePoints.length - 1) return;
			setSegmentSelection(index);
			var first = Number(routePoints[index].hop) || 0;
			var last = Number(routePoints[index + 1].hop) || first;
			var root = mapContainer.closest('.traceroute-diagnostics');
			if (root) root.dispatchEvent(new CustomEvent('traceroute:route-selection', {
				bubbles: true,
				detail: { source: mapContainer.dataset.mapId || 'route-map', segmentIndex: index,
					first: Math.min(first, last), last: Math.max(first, last) }
			}));
		}

		function setSegmentSelection(index) {
			routeSelection = index;
			applySegmentStyles();
			drawDirectionArrows();
			if (index === null) {
				setLinkedRows([], false);
				setLinkedMarkers(0, 0, false);
			} else {
				var first = Number(routePoints[index].hop) || 0;
				var last = Number(routePoints[index + 1].hop) || first;
				var span = [Math.min(first, last), Math.max(first, last)];
				setLinkedRows([span], true);
				setLinkedMarkers(span[0], span[1], true);
			}
			if (resetControl) resetControl.style.display = index === null ? 'none' : '';
		}

		function clearSelection() {
			setSegmentSelection(null);
			var root = mapContainer.closest('.traceroute-diagnostics');
			if (root) root.dispatchEvent(new CustomEvent('traceroute:route-selection', {
				bubbles: true, detail: { source: mapContainer.dataset.mapId || 'route-map', segmentIndex: null }
			}));
		}

		// Group markers that would collide at the current zoom. The route line still
		// follows exact coordinates; short leader lines fan nearby hops out to one
		// readable marker that lists every hop in the cluster.
		var markerLayer = window.L.layerGroup().addTo(map);
		function syncVisibleMarkers() {
			var viewport = null;
			markerRecords.forEach(function (rec) {
				var el = rec.marker.getElement();
				if (!el) return;
				if (!viewport) viewport = mapContainer.getBoundingClientRect();
				var bounds = el.getBoundingClientRect();
				var visible = bounds.left >= viewport.left && bounds.right <= viewport.right
					&& bounds.top >= viewport.top && bounds.bottom <= viewport.bottom;
				el.tabIndex = visible ? 0 : -1;
				el.setAttribute('aria-hidden', visible ? 'false' : 'true');
			});
		}
		// Accuracy radius of an approximate position (map_radius_km).
		function addAccuracyCircle(point, projected) {
			var radiusKm = point.map_radius_km;  // the server decides whether a radius is drawable
			if (!(radiusKm > 0)) return;
			// Web Mercator: a circle that fills most of the frame explains nothing and hides the route.
			var metersPerPixel = 156543.03392 * Math.cos(point.lat * Math.PI / 180) / Math.pow(2, map.getZoom());
			var size = map.getSize();
			if (radiusKm * 1000 / metersPerPixel > Math.min(size.x, size.y) * 0.3) return;
			var center = projected ? map.unproject([projected.x, projected.y], map.getZoom()) : [point.lat, point.lon];
			window.L.circle(center, {
				radius: radiusKm * 1000,
				color: colors.route,
				weight: 1,
				opacity: 0.8,
				dashArray: '3, 4',
				fillColor: colors.route,
				fillOpacity: 0.05,
				interactive: false
			}).addTo(markerLayer);
		}

		function drawRegionLeaders(groups, zoom) {
			regionLeaderLayer.clearLayers();
			var regions = new Map();
			groups.forEach(function (group) {
				if (!group.overlapRegion) return;
				var value = regions.get(group.overlapRegion) || { anchor: { x: 0, y: 0 }, display: { x: 0, y: 0 }, count: 0 };
				value.anchor.x += group.center.x;
				value.anchor.y += group.center.y;
				value.display.x += group.displayCenter.x;
				value.display.y += group.displayCenter.y;
				value.count++;
				regions.set(group.overlapRegion, value);
			});
			regions.forEach(function (region) {
				region.anchor.x /= region.count; region.anchor.y /= region.count;
				region.display.x /= region.count; region.display.y /= region.count;
				if (Math.hypot(region.anchor.x - region.display.x, region.anchor.y - region.display.y) < 5) return;
				window.L.polyline([
					map.unproject([region.anchor.x, region.anchor.y], zoom),
					map.unproject([region.display.x, region.display.y], zoom)
				], { pane: 'regionLeaders', color: colors.label, weight: 1, opacity: 0.7, dashArray: '3 4', interactive: false })
					.addTo(regionLeaderLayer);
			});
		}

		function drawHopMarkers() {
			markerLayer.clearLayers();
			markerRecords = [];
			var zoom = map.getZoom();
			if (typeof zoom !== 'number' || !isFinite(zoom)) return;
			var worldWidth = 256 * Math.pow(2, zoom);
			var projectedRoute = unwrapProjectedRoute(validPoints, function (point) {
				return map.project([point.lat, point.lon], zoom);
			}, worldWidth);
			var markerGroups = groupNearbyHopPoints(routePoints, function (_point, index) {
				return { x: projectedRoute[index].x, y: projectedRoute[index].y };
			}, 36);
			markerGroups = markerGroups.filter(function (group) {
				var hasStart = routeStartIsMapped && group.members.some(function (member) { return pointContainsHop(member.point, routeStartHop); });
				var hasDestination = group.members.some(function (member) { return Boolean(member.point._destination); });
				return !(hasStart && hasDestination);
			});
			markerGroups.forEach(function (group) {
				var groupInfo = describeMarkerGroup(group, routeStartHop, routeStartIsMapped);
				group.markerWidth = Math.max(32, groupInfo.visibleLabel.length * 8 + 16);
				group.markerHeight = 32;
			});
			markerGroups = fanOutOverlappingHopGroups(markerGroups, 36);
			markerGroups = aggregateOverlapRegions(markerGroups);
			drawRegionLeaders(markerGroups, zoom);

			function makeNodes() {
				var nodes = new Map();
				markerGroups.forEach(function (group) {
					group.members.forEach(function (member) {
						nodes.set(member.index, { index: member.index, x: group.displayCenter.x, y: group.displayCenter.y,
							w: group.markerWidth || 32, h: group.markerHeight || 32 });
					});
				});
				return nodes;
			}

			markerGroups.forEach(function (group) {
			var info = describeMarkerGroup(group, routeStartHop, routeStartIsMapped);
			var startMember = info.startMember;
			var destinationMember = info.destinationMember;
			var isStart = info.isStart;
			var isDestination = info.isDestination;
			var isBothEndpoints = isStart && isDestination;
			var labels = info.labels;
			var kind = info.kind;
			var visibleLabel = info.visibleLabel;

			var primary = destinationMember || startMember || group.members[0];
			var point = primary.point;
			var location = [point.city, point.country].filter(Boolean).join(', ') || 'Location unknown';
			var hopDescription = labels.join(', ');
			var endpointLabel = isBothEndpoints ? 'Start and destination' : (isStart ? 'Start' : (isDestination ? 'Destination' : (info.isEstimate ? 'Estimated position' : 'Hops ' + hopDescription)));
			// The server names the source and radius of an approximate position (loc_note).
			var approximateNote = 'Approximate location' + (point.loc_note ? ' (' + point.loc_note + ')' : '');
			var markerLabel = endpointLabel + ' · ' + hopDescription + ' · ' + location;
			if (info.isApproximate) markerLabel += ' · ' + approximateNote;
			if (isDestination && destinationHost) markerLabel += ' · ' + destinationHost;
			var popup = '<strong>' + escapeHtml(endpointLabel) + '</strong><br>' + escapeHtml('Hops ' + hopDescription) + '<br>' + escapeHtml(location);
			if (info.isApproximate) popup += '<br>' + escapeHtml(approximateNote);
			if (group.members.length > 1) {
				var details = group.members.map(function (member) {
					var memberPoint = member.point;
					var memberLocation = [memberPoint.city, memberPoint.country].filter(Boolean).join(', ');
					var description = 'Hop ' + pointLabel(memberPoint);
					if (memberPoint.ip) description += ' · ' + memberPoint.ip;
					if (memberLocation) description += ' · ' + memberLocation;
					return '<li>' + escapeHtml(description) + '</li>';
				}).join('');
				popup += '<ul class="traceroute-hop-popup-list">' + details + '</ul>';
			}
			var pointIp = (isDestination && destinationIp) ? destinationIp : point.ip;
			if (pointIp) popup += '<br><code>' + escapeHtml(pointIp) + '</code>';

			var center = map.unproject([group.displayCenter.x, group.displayCenter.y], zoom);
			group.members.forEach(function (member) { addAccuracyCircle(member.point, projectedRoute[member.index]); });

			var marker = window.L.marker(center, {
				icon: markerIcon(visibleLabel, kind, labels.length > 1, info.isApproximate),
				title: markerLabel,
				alt: markerLabel,
				zIndexOffset: isStart || isDestination ? 1000 : 0
			}).bindPopup(popup).addTo(markerLayer);
			// City names come from the label layer; the full text is the hover tooltip and popup.
			bindSafeTooltip(marker, markerLabel, false);

			var spans = group.members.map(function (member) { return hopSpan(member.point); }).filter(Boolean);
			markerRecords.push({ marker: marker, spans: spans, group: group });
			marker.on('mouseover', function () { setLinkedRows(spans, true); });
			marker.on('mouseout', function () { setLinkedRows(spans, false); });
			var markerEl = marker.getElement();
			if (markerEl) {
				markerEl.addEventListener('focus', function () { setLinkedRows(spans, true); });
				markerEl.addEventListener('blur', function () { setLinkedRows(spans, false); });
			}
		});
			// Pass two uses the actual browser marker boxes. This is bounded to one correction
			// and runs in the same frame, so the approximate first placement is never painted.
			markerRecords.forEach(function (record) {
				var el = record.marker.getElement();
				if (!el) return;
				var rect = el.getBoundingClientRect();
				if (rect.width > 0 && rect.height > 0) {
					record.group.markerWidth = rect.width;
					record.group.markerHeight = rect.height;
				}
			});
			markerGroups = fanOutOverlappingHopGroups(markerGroups, 36);
			drawRegionLeaders(markerGroups, zoom);
			activeMarkerGroups = markerGroups;
			markerRecords.forEach(function (record) {
				record.marker.setLatLng(map.unproject([record.group.displayCenter.x, record.group.displayCenter.y], zoom));
			});
			var nodes = makeNodes();
			if (!fitDisplayNodes(nodes, zoom)) return;
			drawChain(nodes, zoom);
			var obstacles = [];
			nodes.forEach(function (node) {
				obstacles.push({ x1: node.x - node.w / 2, x2: node.x + node.w / 2,
					y1: node.y - node.h / 2, y2: node.y + node.h / 2 });
			});
			chainRecords.forEach(function (record) {
				obstacles.push.apply(obstacles, polylineObstacles(record.points, style.line.weight + style.line.halo, 8));
			});
			var routeCenterX = projectedRoute.length ? (projectedRoute[0].x + projectedRoute[projectedRoute.length - 1].x) / 2 : 0;
			labelLayer.redraw(obstacles, function (lat, lon) {
				var point = map.project([lat, lon], zoom);
				point.x += Math.round((routeCenterX - point.x) / worldWidth) * worldWidth;
				return point;
			});
			syncVisibleMarkers();
		}
		map.on('zoomend', function () { drawHopMarkers(); drawDirectionArrows(); });
		map.on('moveend', syncVisibleMarkers);

		// "Show entire route" control: clears a segment selection. Hidden until a
		// segment is selected, so the default view carries no redundant control.
		var resetControl = document.createElement('button');
		resetControl.type = 'button';
		resetControl.className = 'traceroute-map-reset btn btn-sm btn-outline-secondary';
		resetControl.textContent = 'Show entire route';
		resetControl.style.display = 'none';
		resetControl.addEventListener('click', function () { clearSelection(); });
		mapContainer.appendChild(resetControl);
		mapContainer._resetControl = resetControl;

		var rowRoot = mapContainer.closest('.traceroute-diagnostics');
		var tbody = rowRoot ? rowRoot.querySelector('.hop-table tbody') : null;
		// Timeline preview highlights markers; route selection synchronizes segment state across views.
		var onTimelineHighlight = function (evt) {
			var d = evt.detail || {};
			if (d.on) setLinkedMarkers(Number(d.first), Number(d.last), true); else setLinkedMarkers(0, 0, false);
		};
		var onRouteSelection = function (evt) {
			var d = evt.detail || {};
			if (d.source === mapContainer.dataset.mapId) return;
			var index = Number.isInteger(d.segmentIndex) ? d.segmentIndex : segmentIndexForHop(Number(d.first));
			setSegmentSelection(index === null || index < 0 ? null : index);
		};
		if (rowRoot) {
			rowRoot.addEventListener('traceroute:hop-highlight', onTimelineHighlight);
			rowRoot.addEventListener('traceroute:route-selection', onRouteSelection);
			mapContainer._unlinkTimeline = function () {
				rowRoot.removeEventListener('traceroute:hop-highlight', onTimelineHighlight);
			};
			mapContainer._unlinkSelection = function () {
				rowRoot.removeEventListener('traceroute:route-selection', onRouteSelection);
			};
		}
		if (tbody) {
			var onRowOver = function (evt) {
				var row = evt.target.closest ? evt.target.closest('.tr-hop-row') : null;
				if (!row) return;
				var range = elementHopRange(row);
				setLinkedMarkers(range.first, range.last, true);
			};
			var onRowOut = function () { setLinkedMarkers(0, 0, false); };
			// Clicking a hop row selects the segment leaving that hop (explicit, not
			// hover). Keyboard activation on the row's own control works the same way.
			var onRowClick = function (evt) {
				var row = evt.target.closest ? evt.target.closest('.tr-hop-row') : null;
				if (!row) return;
				var first = elementHopRange(row).first;
				var segmentIndex = segmentIndexForHop(first);
				if (segmentIndex === null) return;
				if (routeSelection === segmentIndex) { clearSelection(); } else { selectSegment(segmentIndex, 'table'); }
			};
			tbody.addEventListener('mouseover', onRowOver);
			tbody.addEventListener('mouseleave', onRowOut);
			tbody.addEventListener('click', onRowClick);
			mapContainer._unlinkRows = function () {
				tbody.removeEventListener('mouseover', onRowOver);
				tbody.removeEventListener('mouseleave', onRowOut);
				tbody.removeEventListener('click', onRowClick);
			};
		}

		// Map a hop number to the index of the segment leaving it (the segment whose
		// first endpoint contains that hop). Returns null when the hop is the last
		// mapped point or is not on the map.
		function segmentIndexForHop(hop) {
			if (!Number.isFinite(hop)) return null;
			for (var i = 0; i < routePoints.length - 1; i++) {
				if (pointContainsHop(routePoints[i], hop)) return i;
			}
			return null;
		}

		// Auto-zoom to bounds with ResizeObserver
		var hasInitialZoom = false;
		var displayFitComplete = false;
		var displayFitAttempts = 0;
		var displayFitScheduled = false;
		function fitDisplayNodes(nodes, zoom) {
			if (displayFitComplete) return true;
			var size = map.getSize();
			if (!size || !size.x || !size.y) return true;
			var pixelOrigin = map.getPixelOrigin();
			var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
			nodes.forEach(function (node) {
				var x = node.x - pixelOrigin.x;
				var y = node.y - pixelOrigin.y;
				minX = Math.min(minX, x - node.w / 2);
				maxX = Math.max(maxX, x + node.w / 2);
				minY = Math.min(minY, y - node.h / 2);
				maxY = Math.max(maxY, y + node.h / 2);
			});
			var availableWidth = Math.max(1, size.x - 48);
			var availableHeight = Math.max(1, size.y - 48);
			var tooWide = maxX - minX > availableWidth;
			var tooTall = maxY - minY > availableHeight;
			var minZoom = style.view.minZoom;
			if ((tooWide || tooTall) && zoom > minZoom && displayFitAttempts < 6) {
				displayFitAttempts++;
				if (!displayFitScheduled) {
					displayFitScheduled = true;
					requestAnimationFrame(function () {
						displayFitScheduled = false;
						if (mapContainer._leaflet_map === map) map.setZoom(zoom - 1, { animate: false });
					});
				}
				return false;
			}
			displayFitComplete = true;
			var centerX = (minX + maxX) / 2;
			var centerY = (minY + maxY) / 2;
			var dx = centerX - size.x / 2;
			var dy = centerY - size.y / 2;
			if (Math.abs(dx) > 1 || Math.abs(dy) > 1) map.panBy([dx, dy], { animate: false });
			return true;
		}
		// Only a user-driven pan/zoom freezes the view; layout resizes (sidebar, tabs) refit it.
		var userAdjustedView = false;
		['pointerdown', 'wheel', 'keydown'].forEach(function (name) {
			mapContainer.addEventListener(name, function () { userAdjustedView = true; }, { passive: true });
		});
		var applyAutoZoom = function () {
			map.invalidateSize();

			// Don't count as "initial zoom" if container has no dimensions yet
			var area = map.getSize();
			if (area.x === 0 || area.y === 0) return;

			if (hasInitialZoom && userAdjustedView) {
				// Keep the manually chosen view, but recompute measured markers, lanes and curves.
				drawHopMarkers();
				drawDirectionArrows();
				syncVisibleMarkers();
				return;
			}
			displayFitComplete = false;
			displayFitAttempts = 0;
			if (latlngs.length >= 2) {
				var routeBounds = chainBounds(validPoints);
				var minLon = Math.min.apply(null, routeBounds.map(function (point) { return point[1]; }));
				var maxLon = Math.max.apply(null, routeBounds.map(function (point) { return point[1]; }));
				if (typeof map.setMaxBounds === 'function') {
					map.setMaxBounds([[-85.05112878, minLon - 180], [85.05112878, maxLon + 180]]);
				}
				var bounds = window.L.latLngBounds(routeBounds);
				map.fitBounds(bounds, {
					paddingTopLeft: [64, 40],
					paddingBottomRight: [64, 80],
					maxZoom: style.view.fitMaxZoom,
					animate: false
				});
			} else {
				map.setView(latlngs[0], style.view.singlePointZoom, { animate: false });
			}
			drawHopMarkers();
			drawDirectionArrows();
			hasInitialZoom = true;
		};

		requestAnimationFrame(function () {
			applyAutoZoom();

			// Keep observing for accordion/tab reveals - retry auto-zoom when container becomes visible
			var ro = new ResizeObserver(function (entries) {
				for (var k = 0; k < entries.length; k++) {
					if (entries[k].contentRect.width > 0 && entries[k].contentRect.height > 0) {
						syncVisibleMarkers();
						if (mapContainer._resizeTimeout) clearTimeout(mapContainer._resizeTimeout);
						mapContainer._resizeTimeout = setTimeout(function () {
							// Guard: check map still exists before calling
							if (!mapContainer._leaflet_map) return;
							applyAutoZoom();
						}, 60);
						break;
					}
				}
			});
			ro.observe(mapContainer);

			// Cleanup observer when map is destroyed
			mapContainer._resizeObserver = ro;
		});

		// Theme change listener: restyle basemap and route when theme switches
		var updateTheme = function () {
			// Guard against destroyed map
			if (!mapContainer._leaflet_map) return;
			applyBasemapTheme(mapContainer, basemapLayers, style);
			// Refresh the palette so the chain, halo and leader lines follow the theme
			colors = themePalette(style);
			drawHopMarkers();
			drawDirectionArrows();
		};

		// Listen for theme changes
		window.addEventListener('theme-changed', updateTheme);
		mapContainer._themeListener = updateTheme;
	}

	// ------------------------------------------------------------------
	// Route timeline: selection, group expansion, table/map highlight
	// ------------------------------------------------------------------
	/**
	 * True when the hop range [first, last] touches the table row range.
	 * @param {number} first
	 * @param {number} last
	 * @param {number} rowFirst
	 * @param {number} rowLast
	 * @returns {boolean}
	 */
	function rangesOverlap(first, last, rowFirst, rowLast) {
		return isFinite(first) && isFinite(last) && isFinite(rowFirst) && isFinite(rowLast) &&
			rowFirst <= last && rowLast >= first;
	}
	/** Replace the ISO country code in a label with the localized name (display only). */
	function localizeCountry(text, code, names) {
		if (!names || !code) return text;
		var name;
		try { name = names.of(code); } catch (e) { return text; }
		if (!name || name === code) return text;
		return text.split(code).join(name);
	}
	function initTimeline(section) {
		var track = section.querySelector('[data-tr-track]');
		var detail = section.querySelector('[data-tr-detail]');
		var root = section.closest('.traceroute-diagnostics') || section;
		var buttons = Array.prototype.slice.call(section.querySelectorAll('[data-tr-hop], [data-tr-group]'));
		var selected = null;  // the pressed leaf button, or null
		var idleText = detail ? detail.textContent : '';
		var names = null;
		try {
			if (typeof Intl !== 'undefined' && Intl.DisplayNames) names = new Intl.DisplayNames(['en'], { type: 'region' });
		} catch (e) { names = null; }
		function setRows(range) {
			root.querySelectorAll('.tr-hop-row').forEach(function (row) {
				var rowRange = elementHopRange(row);
				var on = range !== null && rangesOverlap(range.first, range.last, rowRange.first, rowRange.last);
				row.classList.toggle('is-linked', on);
			});
		}
		function emit(range) {
			var detailData = range === null ? { on: false } : { on: true, first: range.first, last: range.last };
			section.dispatchEvent(new CustomEvent('traceroute:hop-highlight', { bubbles: true, detail: detailData }));
		}
		function emitRouteSelection(range) {
			section.dispatchEvent(new CustomEvent('traceroute:route-selection', {
				bubbles: true,
				detail: { source: 'timeline', first: range ? range.first : null, last: range ? range.last : null }
			}));
		}
		function highlight(range) { setRows(range); emit(range); }
		function showSelectionOrIdle() {
			highlight(selected ? elementHopRange(selected) : null);
		}
		function describe(btn) {
			if (detail) detail.textContent = btn ? btn.dataset.detail || '' : idleText;
		}
		function select(btn) {
			if (selected) selected.setAttribute('aria-pressed', 'false');
			selected = selected === btn ? null : btn;
			if (selected) selected.setAttribute('aria-pressed', 'true');
			describe(selected);
			showSelectionOrIdle();
			emitRouteSelection(selected ? elementHopRange(selected) : null);
		}
		function toggleGroup(btn) {
			var li = btn.closest('.tr-step');
			var members = document.getElementById(btn.getAttribute('aria-controls'));
			if (!li || !members) return;
			var open = btn.getAttribute('aria-expanded') !== 'true';
			btn.setAttribute('aria-expanded', open ? 'true' : 'false');
			members.hidden = !open;
			li.classList.toggle('is-open', open);
			describe(open ? btn : null);
			highlight(open ? elementHopRange(btn) : null);
			if (open && selected) {
				selected.setAttribute('aria-pressed', 'false');
				selected = null;
				emitRouteSelection(null);
			}
		}
		function onClick(evt) {
			var btn = evt.target.closest ? evt.target.closest('[data-tr-hop], [data-tr-group]') : null;
			if (!btn || !section.contains(btn)) return;
			if (btn.hasAttribute('data-tr-group')) toggleGroup(btn); else select(btn);
		}
		function onPreview(evt) {
			var btn = evt.target.closest ? evt.target.closest('[data-tr-hop], [data-tr-group]') : null;
			if (btn && section.contains(btn)) highlight(elementHopRange(btn));
		}
		function onPreviewEnd() { showSelectionOrIdle(); }
		function onFocusIn(evt) {
			var btn = evt.target.closest ? evt.target.closest('[data-tr-hop], [data-tr-group]') : null;
			if (!btn) return;
			// Keep the focused node inside the scrolling track without moving the page
			if (track && btn.scrollIntoView) btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
			highlight(elementHopRange(btn));
		}
		function onRouteSelection(evt) {
			var data = evt.detail || {};
			if (data.source === 'timeline') return;
			var next = Number.isFinite(data.first)
				? buttons.find(function (button) {
					var range = elementHopRange(button);
					return rangesOverlap(data.first, data.last, range.first, range.last);
				}) || null
				: null;
			if (selected) selected.setAttribute('aria-pressed', 'false');
			selected = next;
			if (selected) selected.setAttribute('aria-pressed', 'true');
			describe(selected);
			showSelectionOrIdle();
		}
		function onKeydown(evt) {
			// Arrow keys scroll the track while the track itself has focus
			if (evt.target !== track || !track) return;
			var step = 120;
			if (evt.key === 'ArrowRight') track.scrollLeft += step;
			else if (evt.key === 'ArrowLeft') track.scrollLeft -= step;
			else if (evt.key === 'Home') track.scrollLeft = 0;
			else if (evt.key === 'End') track.scrollLeft = track.scrollWidth;
			else return;
			evt.preventDefault();
		}
		// Only scrollable tracks are a tab stop (otherwise the vertical layout gets a dead stop)
		function syncScrollable() {
			if (!track) return;
			var scrollable = track.scrollWidth > track.clientWidth + 1;
			if (scrollable) track.setAttribute('tabindex', '0'); else track.removeAttribute('tabindex');
		}
		buttons.forEach(function (btn) {
			var code = btn.dataset.country;
			if (!code) return;
			btn.setAttribute('aria-label', localizeCountry(btn.getAttribute('aria-label') || '', code, names));
			btn.dataset.detail = localizeCountry(btn.dataset.detail || '', code, names);
			btn.title = btn.dataset.detail;
			var title = btn.querySelector('.tr-step-title');
			if (title) title.textContent = localizeCountry(title.textContent, code, names);
		});
		section.addEventListener('click', onClick);
		section.addEventListener('mouseover', onPreview);
		section.addEventListener('mouseleave', onPreviewEnd);
		section.addEventListener('focusin', onFocusIn);
		section.addEventListener('focusout', onPreviewEnd);
		root.addEventListener('traceroute:route-selection', onRouteSelection);
		if (track) track.addEventListener('keydown', onKeydown);
		var observer = null;
		if (track && typeof ResizeObserver === 'function') {
			observer = new ResizeObserver(syncScrollable);
			observer.observe(track);
		}
		syncScrollable();
		section._trCleanup = function () {
			section.removeEventListener('click', onClick);
			section.removeEventListener('mouseover', onPreview);
			section.removeEventListener('mouseleave', onPreviewEnd);
			section.removeEventListener('focusin', onFocusIn);
			section.removeEventListener('focusout', onPreviewEnd);
			root.removeEventListener('traceroute:route-selection', onRouteSelection);
			if (track) track.removeEventListener('keydown', onKeydown);
			if (observer) observer.disconnect();
			section._trCleanup = null;
		};
	}
	function initAllTimelines(rootEl) {
		var root = rootEl && typeof rootEl.querySelectorAll === 'function' ? rootEl : document;
		var sections = Array.prototype.slice.call(root.querySelectorAll('.traceroute-route-overview:not([data-tr-timeline])'));
		if (root !== document && root.matches && root.matches('.traceroute-route-overview:not([data-tr-timeline])')) sections.unshift(root);
		sections.forEach(function (section) {
			section.setAttribute('data-tr-timeline', '1');
			initTimeline(section);
		});
	}
	// Shared pagehide listener to avoid accumulation
	var _pagehideRegistered = false;
	var _activeContainers = new Set();

	function _handlePagehide() {
		_activeContainers.forEach(function (container) {
			cleanupMapContainer(container);
		});
		_activeContainers.clear();
	}

	/**
	 * Initialize all traceroute maps on the page
	 */
	function initAllMaps(rootEl) {
		// Guard: ensure root is a valid element with querySelectorAll
		var root = rootEl && typeof rootEl.querySelectorAll === 'function' ? rootEl : document;
		initAllTimelines(root);
		var containers = Array.prototype.slice.call(
			root.querySelectorAll('.traceroute-map-container:not([data-initialized])')
		);

		// Check if the root element itself is a map container
		if (root !== document && root.matches
			&& root.matches('.traceroute-map-container:not([data-initialized])')) {
			containers.unshift(root);
		}
		containers.forEach(function (container) {
			// Mark as initialized BEFORE async load to prevent race condition
			container.setAttribute('data-initialized', '1');
			
			// Register cleanup handlers
			var htmxHandler = function (evt) {
				var swapTarget = evt.detail.target;
				if (swapTarget && (swapTarget === container || swapTarget.contains(container))) {
					cleanupMapContainer(container);
				}
			};
			var pagehideHandler = function () {
				cleanupMapContainer(container);
			};

			// Store handlers on container for cleanup
			container._htmxHandler = htmxHandler;
			container._pagehideHandler = pagehideHandler;
			
			document.body.addEventListener('htmx:beforeSwap', htmxHandler);
			window.addEventListener('pagehide', pagehideHandler);

			// Register shared pagehide handler
			_activeContainers.add(container);
			if (!_pagehideRegistered) {
				_pagehideRegistered = true;
				window.addEventListener('pagehide', _handlePagehide);
			}

			// Load Leaflet and initialize this map
			ensureLeaflet()
				.then(function () {
					// Fragment was replaced or cleaned up while Leaflet loaded
					if (container._pagehideHandler !== pagehideHandler || !container.isConnected) return;
					initMap(container);
				})
				.catch(function (err) {
					console.error('Leaflet loading failed:', err);
					if (container._pagehideHandler !== pagehideHandler) return;
					// Remove data-initialized to allow retry
					container.removeAttribute('data-initialized');
					if (container) {
						// Cleanup existing map if re-init failed
						cleanupMapContainer(container);

						// Create error message using DOM API (XSS-safe)
						var errorDiv = document.createElement('div');
						errorDiv.className = 'd-flex align-items-center justify-content-center h-100 text-body-secondary small';

						var icon = document.createElement('span');
						icon.className = 'material-icons me-2';
						icon.textContent = 'map';

						var text = document.createTextNode('Map could not be loaded.');

						errorDiv.appendChild(icon);
						errorDiv.appendChild(text);

						container.innerHTML = '';
						container.appendChild(errorDiv);
					}
				});
		});
	}

	window.TracerouteMap = window.TracerouteMap || {};
	window.TracerouteMap.initAllMaps = initAllMaps;
	window.TracerouteMap.groupNearbyHopPoints = groupNearbyHopPoints;
	window.TracerouteMap.fanOutOverlappingHopGroups = fanOutOverlappingHopGroups;
		window.TracerouteMap.aggregateOverlapRegions = aggregateOverlapRegions;
	window.TracerouteMap.computeArrowSegments = computeArrowSegments;
	window.TracerouteMap.describeMarkerGroup = describeMarkerGroup;
	window.TracerouteMap.pointLabel = pointLabel;
	window.TracerouteMap.routePointsOf = routePointsOf;
	window.TracerouteMap.isDashedSegment = isDashedSegment;
	window.TracerouteMap.placeLabels = placeLabels;
	window.TracerouteMap.trimChainSegment = trimChainSegment;
	window.TracerouteMap.unwrapProjectedRoute = unwrapProjectedRoute;
	window.TracerouteMap.sampleQuadraticBezier = sampleQuadraticBezier;
	window.TracerouteMap.chooseQuadraticCurve = chooseQuadraticCurve;
	window.TracerouteMap.pointAndTangentAt = pointAndTangentAt;
	window.TracerouteMap.trimPolylineSegment = trimPolylineSegment;
	window.TracerouteMap.polylineObstacles = polylineObstacles;
	window.TracerouteMap.directionArrowDistances = directionArrowDistances;
	window.TracerouteMap.chainBounds = chainBounds;
	window.TracerouteMap.segmentObstacles = segmentObstacles;

	/**
	 * Destroy every map below root (call before replacing a traceroute fragment)
	 * @param {HTMLElement} rootEl - Element whose map containers are released
	 */
	window.TracerouteMap.cleanupAll = function (rootEl) {
		if (!rootEl || typeof rootEl.querySelectorAll !== 'function') return;
		rootEl.querySelectorAll('.traceroute-map-container').forEach(cleanupMapContainer);
		rootEl.querySelectorAll('.traceroute-route-overview').forEach(function (section) {
			if (section._trCleanup) section._trCleanup();
		});
	};
	window.TracerouteMap.initAllTimelines = initAllTimelines;

	// Initialize maps on page load
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', function () { initAllMaps(); });
	} else {
		initAllMaps();
	}

	// Re-initialize maps after HTMX swaps (scoped to swapped subtree)
	document.body.addEventListener('htmx:load', function (evt) {
		// Small delay to ensure DOM is stable after swap
		setTimeout(function () {
			initAllMaps(evt.detail.elt);
		}, 50);
	});

})();
