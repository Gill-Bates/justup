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

		// Return existing promise if Leaflet is already being loaded
		if (window._leafletLoadingPromise) {
			return window._leafletLoadingPromise;
		}

		// Create new loading promise
		window._leafletLoadingPromise = new Promise(function (resolve, reject) {
			// Load CSS if not already present
			if (!document.querySelector('link[href*="leaflet"]')) {
				var css = document.createElement('link');
				css.rel = 'stylesheet';
				css.href = '/static/css/leaflet.css';
				document.head.appendChild(css);
			}

			// Load Leaflet JS
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
	 * HTML escape helper for popup content (XSS prevention)
	 * Reuses a single detached element for efficiency
	 * @param {string} text - Text to escape
	 * @returns {string} HTML-safe text
	 */
	var _escDiv = document.createElement('div');
	function esc(text) {
		_escDiv.textContent = text || '';
		var result = _escDiv.innerHTML;
		_escDiv.textContent = '';  // Clear to avoid holding references
		return result;
	}

	/**
	 * Apply jitter to coordinates for better marker visibility
	 * Only applies jitter when coordinates actually collide
	 * Uses proximity threshold (~11m) instead of exact equality for floating-point safety
	 * @param {number} lat - Latitude
	 * @param {number} lon - Longitude
	 * @param {number} idx - Index for jitter calculation
	 * @param {Array} allPoints - All points to check for collisions
	 * @returns {Array} [lat, lon] with jitter applied if needed
	 */
	function jitter(lat, lon, idx, allPoints) {
		// Proximity threshold: ~11m (0.0001 degrees ~ 11 meters at equator)
		var PROXIMITY_THRESHOLD = 0.0001;
		
		// Only jitter if coordinates collide with another point
		var hasCollision = allPoints.some(function (other, otherIdx) {
			return otherIdx !== idx 
				&& Math.abs(other.lat - lat) < PROXIMITY_THRESHOLD
				&& Math.abs(other.lon - lon) < PROXIMITY_THRESHOLD;
		});
		if (!hasCollision) return [lat, lon];

		var angle = (idx * 137.5) % 360;  // Golden angle for good distribution
		var distance = 0.001;  // ~111m
		var rad = angle * Math.PI / 180;
		return [
			lat + distance * Math.cos(rad),
			lon + distance * Math.sin(rad)
		];
	}

	/**
	 * Get tile URL based on current theme
	 * @returns {string} Tile layer URL
	 */
	function getTileUrl() {
		var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
		return dark
			? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
			: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png';
	}

	/**
	 * Get route colors based on current theme
	 * @returns {Object} Colors for solid and dashed routes
	 */
	function getRouteColors() {
		var dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
		return {
			solid: dark ? '#198754' : '#146c43',
			dashed: dark ? '#9ca3af' : '#6c757d'
		};
	}

	/**
	 * Shared tile layer configuration
	 */
	var tileOptions = {
		minZoom: 2,
		maxZoom: 18,
		tileSize: 256,
		zoomOffset: 0,
		noWrap: true,
		detectRetina: false,
		bounds: [[-90, -180], [90, 180]]
	};

	/**
	 * Cleanup map resources on a container
	 * @param {HTMLElement} container - Map container element
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
	 * @param {HTMLElement} mapContainer - Map container element
	 */
	function initMap(mapContainer) {
		if (!mapContainer || !window.L) return;

		// Cleanup existing map before re-init
		cleanupMapContainer(mapContainer);

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

		// Create map using container element directly (prevents duplicate ID issues)
		var map = window.L.map(mapContainer, {
			scrollWheelZoom: true,
			attributionControl: false,
			zoomSnap: 1,
			zoomDelta: 1,
			minZoom: 2,
			maxBounds: [[-90, -180], [90, 180]],
			maxBoundsViscosity: 1.0
		});
		mapContainer._leaflet_map = map;

		// Add initial tile layer
		var currentTileLayer = window.L.tileLayer(getTileUrl(), tileOptions).addTo(map);

		var latlngs = validPoints.map(function (p) { return [p.lat, p.lon]; });

		// Helper to check if segment is interpolated
		function isSegmentInterpolated(p1, p2) {
			return p1.segment_to_next === 'dashed' ||
				p2._interpolated ||
				p2.segment_type === 'dashed';
		}

		// Batch route segments by style for better performance
		// Process only segments (pairs of consecutive points), not the final point independently
		var segments = { solid: [], dashed: [] };
		var currentStyle = null;
		var currentCoords = [];

		for (var i = 0; i < validPoints.length - 1; i++) {
			var style = isSegmentInterpolated(validPoints[i], validPoints[i + 1])
				? 'dashed' : 'solid';

			if (style !== currentStyle && currentCoords.length > 0) {
				segments[currentStyle].push(currentCoords);
				currentCoords = [currentCoords[currentCoords.length - 1]]; // keep last point for continuity
			}
			currentStyle = style;
			if (currentCoords.length === 0) {
				currentCoords.push([validPoints[i].lat, validPoints[i].lon]);
			}
			currentCoords.push([validPoints[i + 1].lat, validPoints[i + 1].lon]);
		}
		if (currentCoords.length > 1) segments[currentStyle].push(currentCoords);

		// Draw batched polylines with theme-aware colors
		// Store polylines for theme updates
		var polylines = [];
		var colors = getRouteColors();
		segments.solid.forEach(function (coords) {
			var pl = window.L.polyline(coords, { color: colors.solid, weight: 3, opacity: 0.8 }).addTo(map);
			pl._routeStyle = 'solid';
			polylines.push(pl);
		});
		segments.dashed.forEach(function (coords) {
			var pl = window.L.polyline(coords, { color: colors.dashed, weight: 2, opacity: 0.8, dashArray: '8, 8' }).addTo(map);
			pl._routeStyle = 'dashed';
			polylines.push(pl);
		});

		// Custom marker icons
		var sourceIcon = window.L.divIcon({
			html: '<span class="material-icons" style="font-size: 24px; color: #0d6efd;">location_on</span>',
			className: 'leaflet-div-icon-custom',
			iconSize: [24, 24],
			iconAnchor: [12, 24]
		});
		var destIcon = window.L.divIcon({
			html: '<span class="material-icons" style="font-size: 24px; color: #dc3545;">location_on</span>',
			className: 'leaflet-div-icon-custom',
			iconSize: [24, 24],
			iconAnchor: [12, 24]
		});
		var hopIcon = window.L.divIcon({
			html: '<span style="display:block;width:6px;height:6px;border-radius:50%;background:#198754;"></span>',
			className: 'leaflet-div-icon-custom',
			iconSize: [6, 6],
			iconAnchor: [3, 3]
		});

		// Source marker (XSS-safe: escape all dynamic content)
		var first = validPoints[0];
		var sourceLabel = (first.city || 'Source') + (first.country ? ', ' + first.country : '');
		window.L.marker([first.lat, first.lon], { icon: sourceIcon })
			.bindPopup(
				'<strong>Source</strong><br>' +
				esc(sourceLabel) + '<br><code>' + esc(first.ip) + '</code>'
			)
			.addTo(map);

		// Destination marker (XSS-safe)
		var last = validPoints[validPoints.length - 1];
		var destLabel = (last.city || 'Destination') + (last.country ? ', ' + last.country : '');
		window.L.marker([last.lat, last.lon], { icon: destIcon })
			.bindPopup(
				'<strong>Destination</strong><br>' +
				esc(destLabel) + '<br><code>' + esc(last.ip) + '</code>'
			)
			.addTo(map);

		// Intermediate hop markers (skip interpolated points, XSS-safe)
		for (var j = 1; j < validPoints.length - 1; j++) {
			var p = validPoints[j];
			if (p._interpolated) continue;  // Don't show markers for interpolated hops

			var jittered = jitter(p.lat, p.lon, j, validPoints);
			var label = (p.city || p.country || 'Hop ' + p.hop);
			// Show hop range if multiple hops were merged at this location
			var hopLabel = p._hop_range ? ('Hops ' + p._hop_range) : ('Hop ' + p.hop);
			window.L.marker(jittered, { icon: hopIcon })
				.bindPopup(
					'<strong>' + esc(hopLabel) + '</strong><br>' +
					esc(label) + '<br><code>' + esc(p.ip) + '</code>'
				)
				.addTo(map);
		}

		// Auto-zoom to bounds with ResizeObserver
		var hasInitialZoom = false;
		var applyAutoZoom = function () {
			map.invalidateSize();

			// Don't count as "initial zoom" if container has no dimensions yet
			var area = map.getSize();
			if (area.x === 0 || area.y === 0) return;

			if (hasInitialZoom) return; // Preserve user's manual zoom
			if (latlngs.length >= 2) {
				var bounds = window.L.latLngBounds(latlngs);
				map.fitBounds(bounds, {
					padding: [35, 35],
					maxZoom: 7,
					animate: false
				});
			} else {
				map.setView(latlngs[0], 5, { animate: false });
			}
			hasInitialZoom = true;
		};

		requestAnimationFrame(function () {
			applyAutoZoom();

			// Keep observing for accordion/tab reveals - retry auto-zoom when container becomes visible
			var ro = new ResizeObserver(function (entries) {
				for (var k = 0; k < entries.length; k++) {
					if (entries[k].contentRect.width > 0 && entries[k].contentRect.height > 0) {
						if (mapContainer._resizeTimeout) clearTimeout(mapContainer._resizeTimeout);
						mapContainer._resizeTimeout = setTimeout(function () {
							// Guard: check map still exists before calling
							if (!mapContainer._leaflet_map) return;
							applyAutoZoom();
						}, 150);
						break;
					}
				}
			});
			ro.observe(mapContainer);

			// Cleanup observer when map is destroyed
			mapContainer._resizeObserver = ro;
		});

		// Theme change listener: update tile layer when theme switches
		var updateTileLayer = function () {
			// Guard against destroyed map
			if (!mapContainer._leaflet_map) return;
			// Remove old layer, add new one (prevents layer stacking)
			if (currentTileLayer) {
				map.removeLayer(currentTileLayer);
			}
			currentTileLayer = window.L.tileLayer(getTileUrl(), tileOptions).addTo(map);
		};

		// Listen for theme changes
		window.addEventListener('theme-changed', updateTileLayer);
		mapContainer._themeListener = updateTileLayer;
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
					initMap(container);
				})
				.catch(function (err) {
					console.error('Leaflet loading failed:', err);
					// Remove data-initialized to allow retry
					container.removeAttribute('data-initialized');
					if (container) {
						// Cleanup existing map if re-init failed
						cleanupMapContainer(container);

						// Create error message using DOM API (XSS-safe)
						var errorDiv = document.createElement('div');
						errorDiv.className = 'd-flex align-items-center justify-content-center h-100 text-muted small';

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
