//
// app/static/js/dashboard-grid.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// GridStack layout layer for the dashboard: position, size and visibility of the four status heroes,
// quality and downtime widgets. It owns none of their data (htmx and dashboard.js keep loading it);
// it only arranges the tiles and persists the arrangement per user via /me/dashboard-layout.
(function () {
	'use strict';

	const root = document.getElementById('dashboard-grid');
	// Only on the dashboard page, and never twice; GridStack/registry must have loaded.
	if (!root || window.JuDashboardGrid || !window.GridStack || !window.JuDashboardRegistry) return;

	const $ = (id) => document.getElementById(id);
	const ENDPOINT = '/me/dashboard-layout';
	const DEFAULT_CELL_HEIGHT = 72;
	// Initialization-failure fallbacks only; they equal the token defaults in dashboard.css (1.25rem gap / 2).
	const DEFAULT_GRID_MARGIN = 10;
	const DEFAULT_HANDLE_HEIGHT = 24;
	// Mirrors DASHBOARD_MAX_Y on the server; no widget may cross this row bound (y + h <= MAX_Y).
	const MAX_Y = 1000;
	const REVEAL_TIMEOUT_MS = 2500; // below the CSS failsafe (4s)
	const LAYOUT_LOAD_TIMEOUT_MS = 3000;
	// Keep in sync with the stacked block in dashboard.css (Bootstrap lg breakpoint).
	const MOBILE_QUERY = '(max-width: 991.98px)';
	const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
	const PARK_ROWS = 10000;
	const NO_ROW_LIMIT = PARK_ROWS + MAX_Y;
	// Rows below the settled layout a drag or resize may use (DESIGN.md). Without a bound GridStack grows
	// the container to wherever the pointer is, and the page scroll height follows the cursor.
	const EDIT_RESERVE_ROWS = 4;

	const registry = window.JuDashboardRegistry.forRole(root.dataset.isAdmin === 'true');
	const DEFAULT_LAYOUT = registry.map((def) => ({
		id: def.id, x: def.defaultX, y: def.defaultY,
		w: def.defaultSize.w, h: def.defaultSize.h,
		visible: def.defaultVisible, height_mode: 'auto', label: def.label,
	}));
	const MIN_SIZE = new Map(registry.map((def) => [def.id, { w: def.minW, h: def.minH }]));
	// Registry minimum as GridStack node limits; the template carries no gs-min-* of its own.
	const minLimits = (id) => ({ minW: MIN_SIZE.get(id).w, minH: MIN_SIZE.get(id).h });
	const defaultEntry = (def) => ({ id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: def.visible, height_mode: 'auto' });

	let grid = null;
	const widgetState = new Map(); // id -> { id, x, y, w, h, visible }
	let savedLayout = null;
	let fitFrame = 0;
	let fitting = false;
	let applyingLayout = false;
	let editingNow = false;
	let slotLayer = null;
	let cellHeight = DEFAULT_CELL_HEIGHT;
	let gridMargin = DEFAULT_GRID_MARGIN;
	let handleHeight = DEFAULT_HANDLE_HEIGHT;
	const resizeStartHeights = new Map();

	const widgetElement = (id) => document.querySelector(`.grid-stack-item[data-widget-id="${id}"]`);
	const messageFrom = (error) => (error && error.message) ? String(error.message) : 'Something went wrong.';

	function notify(message, ok) {
		const toast = window.justUpToast;
		if (toast) (ok ? toast.success : toast.error)(message);
	}

	function announce(message) {
		const live = $('dashboard-grid-status');
		if (live) live.textContent = message;
	}

	function cssPixels(property, fallback) {
		const measure = document.createElement('span');
		measure.style.cssText = `position:absolute;visibility:hidden;pointer-events:none;width:var(${property});height:0`;
		root.append(measure);
		const pixels = measure.getBoundingClientRect().width;
		measure.remove();
		return pixels || fallback;
	}

	async function request(method, body, signal) {
		const options = { method, headers: { Accept: 'application/json' } };
		if (signal) options.signal = signal;
		if (body !== undefined) {
			options.headers['Content-Type'] = 'application/json';
			options.body = JSON.stringify(body);
		}
		const response = await window.apiFetch(ENDPOINT, options);
		if (!response.ok) {
			let detail = '';
			try { detail = (await response.json()).detail; } catch (e) { /* non-JSON error body */ }
			throw new Error(typeof detail === 'string' && detail ? detail : `Request failed (${response.status})`);
		}
		return response.json();
	}

	// A broken, foreign or future-version stored layout must never break the dashboard: any problem
	// falls back to the known-good default instead of raising.
	function mergeWithDefault(stored) {
		const byId = new Map();
		const hasStoredLayout = stored && stored.version === 2 && Array.isArray(stored.widgets);
		if (hasStoredLayout) {
			for (const widget of stored.widgets) {
				if (widget && typeof widget.id === 'string') byId.set(widget.id, widget);
			}
		}
		const inRange = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
		const merged = DEFAULT_LAYOUT.map((def) => {
			const saved = byId.get(def.id);
			if (!saved) return { ...defaultEntry(def), visible: hasStoredLayout ? false : def.visible };
			const valid = inRange(saved.x, 11) && inRange(saved.y, MAX_Y) &&
				Number.isInteger(saved.w) && saved.w >= 1 && saved.w <= 12 &&
				Number.isInteger(saved.h) && saved.h >= 1 && saved.h <= 100 &&
				saved.x + saved.w <= 12 && saved.y + saved.h <= MAX_Y &&
				(saved.height_mode === 'auto' || saved.height_mode === 'manual');
			if (!valid) return defaultEntry(def);
			// Stored sizes below the current registry minimum (also of hidden widgets) are raised to it, so
			// the layout the client sends back is always one the server accepts. Overlaps are resolved below.
			const min = MIN_SIZE.get(def.id);
			const w = Math.max(saved.w, min.w);
			const h = Math.max(saved.h, min.h);
			if (saved.y + h > MAX_Y) return defaultEntry(def);
			return { id: def.id, x: Math.min(saved.x, 12 - w), y: saved.y, w, h,
				visible: saved.visible !== false, height_mode: saved.height_mode };
		});
		return separateOverlaps(merged) || separateOverlaps(DEFAULT_LAYOUT.map(defaultEntry)) || DEFAULT_LAYOUT.map(defaultEntry);
	}

	// The upper/left box keeps its place, a later overlapping one moves down. Returns null when an
	// overlap cannot be pushed down within MAX_Y, so the caller can discard the layout.
	function separateOverlaps(layout) {
		const placed = [];
		const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
		const byPosition = layout.filter((entry) => entry.visible).sort((a, b) => a.y - b.y || a.x - b.x);
		for (const entry of byPosition) {
			let blocker;
			while ((blocker = placed.find((other) => overlaps(entry, other)))) {
				const nextY = blocker.y + blocker.h;
				if (nextY <= entry.y || nextY + entry.h > MAX_Y) return null;
				entry.y = nextY;
			}
			placed.push(entry);
		}
		return layout;
	}

	// Two passes: park every widget far down first, then move it to its row, so a widget does not
	// collide with neighbours still at the template position and shove them around.
	// GridStack's own row bound: settled layout plus the reserve while editing, the layout bound otherwise.
	// Never 0: GridStack compacts the whole layout when its rows exceed the new bound; the open bound
	// also has to cover the parking rows of applyLayout.
	function limitRows(editing) {
		grid.updateOptions({ maxRow: editing ? grid.getRow() + EDIT_RESERVE_ROWS : NO_ROW_LIMIT });
	}

	function applyLayout(layout) {
		applyingLayout = true;
		limitRows(false); // parking rows and stored positions must not be clamped by the edit bound
		grid.batchUpdate();
		try {
			const shown = [];
			for (const entry of layout) {
				widgetState.set(entry.id, { ...entry });
				const el = widgetElement(entry.id);
				if (!el) continue;
				if (entry.visible) {
					el.classList.remove('d-none');
					if (!el.gridstackNode) grid.makeWidget(el);
					grid.update(el, { x: entry.x, y: entry.y + PARK_ROWS, w: entry.w, h: entry.h, ...minLimits(entry.id) });
					shown.push([el, entry]);
				} else {
					if (el.gridstackNode) grid.removeWidget(el, false);
					el.classList.add('d-none');
				}
			}
			for (const [el, entry] of shown) grid.update(el, { y: entry.y });
		} finally {
			grid.batchUpdate(false);
			applyingLayout = false;
			limitRows(editingNow);
		}
		document.dispatchEvent(new CustomEvent('dashboard:layout-applied'));
	}

	// Content height independent of the tile: the card is measured out of flow at its current width,
	// so neither the tile's stretch (flex-grow, h-100) nor a running CSS transition (.card-status
	// animates `all`) can feed the tile height back into the measure and make the tile grow forever.
	function naturalHeight(inner) {
		const saved = ['position', 'width', 'height', 'flex', 'transition'].map((name) => [
			name, inner.style.getPropertyValue(name), inner.style.getPropertyPriority(name),
		]);
		const width = inner.getBoundingClientRect().width;
		inner.style.setProperty('transition', 'none', 'important');
		inner.style.setProperty('position', 'absolute', 'important');
		inner.style.setProperty('width', `${width}px`, 'important');
		inner.style.setProperty('height', 'auto', 'important'); // Bootstrap's h-100 is !important
		inner.style.setProperty('flex', '0 0 auto', 'important');
		const natural = Math.ceil(inner.getBoundingClientRect().height) + 1; // +1 covers sub-pixel rounding
		for (const [name, value, priority] of saved) {
			if (value) inner.style.setProperty(name, value, priority);
			else inner.style.removeProperty(name);
		}
		return natural;
	}

	function neededRows(el) {
		const content = el.querySelector('.grid-stack-item-content');
		const inner = content && content.querySelector(':scope > :not(.dashboard-widget-handle)');
		if (!inner) return 1;
		const frame = el.offsetHeight - content.offsetHeight;
		// The handle band is part of the card in edit mode; outside it the fit still reserves it, so
		// entering or leaving edit mode never changes a tile's row count.
		const band = editingNow ? 0 : handleHeight;
		return Math.ceil((naturalHeight(inner) + frame + band) / cellHeight);
	}

	// Automatic heights follow content in either direction; manual heights are user-owned.
	function fitWidget(el, needed) {
		const node = el.gridstackNode;
		if (!node) return;
		const id = el.dataset.widgetId;
		const state = widgetState.get(id);
		if (!state || state.height_mode === 'manual') return;
		const min = MIN_SIZE.get(id) || { h: 1 };
		const rows = Math.max(min.h, needed, 1);
		if (node.h !== rows) grid.update(el, { h: rows });
	}

	function fitLayout() {
		fitFrame = 0;
		// Stacked below the breakpoint: tiles have natural height, rows are irrelevant there.
		if (!grid || window.matchMedia(MOBILE_QUERY).matches) return;
		// A fit batch during a drag or resize would swallow the user's own change event.
		if (document.querySelector('.ui-draggable-dragging, .ui-resizable-resizing')) return;
		fitting = true;
		try {
			limitRows(false); // content growth must not be clamped by the edit bound
			grid.batchUpdate();
			const items = [...document.querySelectorAll('.grid-stack-item[data-widget-id]')]
				.filter((el) => el.gridstackNode && !el.classList.contains('d-none'));
			const needed = new Map(items.map((el) => [el, neededRows(el)]));
			for (const el of items) fitWidget(el, needed.get(el));
		} finally {
			grid.batchUpdate(false);
			fitting = false;
			limitRows(editingNow);
		}
	}

	function scheduleFit() {
		if (!fitFrame) fitFrame = requestAnimationFrame(fitLayout);
	}

	// Anything that changes a tile's content size re-runs the fit: window width, htmx swaps, text
	// changes and the icon font arriving late.
	function observeContent() {
		window.addEventListener('resize', () => {
			const margin = cssPixels('--ju-dash-gutter', DEFAULT_GRID_MARGIN);
			if (grid && margin !== gridMargin) {
				gridMargin = margin;
				grid.margin(gridMargin);
			}
			scheduleFit();
		});
		if (document.fonts) document.fonts.ready.then(scheduleFit);
		new MutationObserver(scheduleFit).observe(root, {
			// Content swaps/text need a new fit. GridStack and edit-mode class toggles do not, and
			// reacting to those during interaction can queue redundant layout passes and flicker.
			subtree: true, childList: true, characterData: true,
		});
		if (!('ResizeObserver' in window)) return;
		const observer = new ResizeObserver(scheduleFit);
		observer.observe(root);
		root.querySelectorAll('.grid-stack-item-content > :not(.dashboard-widget-handle)').forEach((inner) => observer.observe(inner));
	}

	// Read the saved layout from the settled grid, not from change-event deltas, which can miss
	// neighbours pushed by GridStack.
	function syncStateFromGrid() {
		for (const def of DEFAULT_LAYOUT) {
			const el = widgetElement(def.id);
			const node = el && el.gridstackNode;
			const prev = widgetState.get(def.id);
			if (!node || !prev || prev.visible === false) continue;
			widgetState.set(def.id, { ...prev, id: def.id, x: node.x, y: node.y, w: node.w, h: node.h, visible: true });
		}
	}

	// Only the known structured props travel to the server; nothing from the DOM is ever sent.
	async function saveLayout() {
		syncStateFromGrid();
		const widgets = DEFAULT_LAYOUT.map((def) => {
			const state = widgetState.get(def.id);
			return { id: def.id, x: state.x, y: state.y, w: state.w, h: state.h, visible: state.visible, height_mode: state.height_mode };
		});
		try {
			await request('PUT', { version: 2, widgets });
			savedLayout = widgets.map((widget) => ({ ...widget }));
			notify('Dashboard layout saved.', true);
		} catch (error) {
			notify(messageFrom(error), false);
			// Roll back so what is shown always matches what is stored.
			if (savedLayout) {
				applyLayout(savedLayout.map((entry) => ({ ...entry })));
				renderWidgetList();
				scheduleFit();
			}
		}
	}

	function onGridChange(_event, items) {
		if (fitting || applyingLayout || !editingNow) return;
		for (const item of items || []) {
			const id = item.el && item.el.dataset && item.el.dataset.widgetId;
			if (!id) continue;
			const prev = widgetState.get(id);
			widgetState.set(id, { id, x: item.x, y: item.y, w: item.w, h: item.h, visible: true,
				height_mode: (prev && prev.height_mode) || 'auto' });
		}
	}

	// Edit mode shows every grid cell as an empty dashed slot (dashboard.css), one per cell for the
	// current grid height.
	function syncSlots() {
		if (!editingNow || !slotLayer) return;
		const wanted = grid.getRow() * grid.getColumn(); // GridStack's own row count, not a re-derived height
		while (slotLayer.childElementCount > wanted) slotLayer.lastElementChild.remove();
		const missing = wanted - slotLayer.childElementCount;
		if (missing <= 0) return;
		const slots = document.createDocumentFragment();
		for (let i = 0; i < missing; i++) {
			const slot = document.createElement('div');
			slot.className = 'dashboard-slot';
			slots.append(slot);
		}
		slotLayer.append(slots);
	}

	function setSlots(editing) {
		if (editing && !slotLayer) {
			slotLayer = document.createElement('div');
			slotLayer.className = 'dashboard-slots';
			slotLayer.setAttribute('aria-hidden', 'true');
			root.prepend(slotLayer);
			if ('ResizeObserver' in window) new ResizeObserver(syncSlots).observe(root);
		}
		if (!slotLayer) return;
		if (editing) syncSlots();
		else slotLayer.replaceChildren();
	}

	function setEditing(editing) {
		editingNow = editing;
		// Keep GridStack's transition pass out of edit-mode reflows; restore it on exit unless
		// the user requested reduced motion.
		grid.setAnimation(!editing && !window.matchMedia(REDUCED_MOTION_QUERY).matches);
		root.classList.toggle('dashboard-grid--editing', editing);
		limitRows(editing);
		setSlots(editing);
		root.querySelectorAll('.dashboard-widget-handle').forEach((handle) => { handle.hidden = !editing; });
		$('dashboard-edit-toggle').hidden = editing;
		$('dashboard-edit-widgets').hidden = !editing;
		$('dashboard-reset-layout').hidden = !editing;
		$('dashboard-edit-done').hidden = !editing;
		grid.enableMove(editing);
		grid.enableResize(editing);
		scheduleFit();
		// The focused button just got hidden; hand focus to its counterpart (not after a viewport change).
		const next = $(editing ? 'dashboard-edit-done' : 'dashboard-edit-toggle');
		if (next.offsetParent) next.focus();
	}

	function toggleWidgetVisibility(id, visible) {
		const state = widgetState.get(id) || DEFAULT_LAYOUT.find((def) => def.id === id);
		const el = widgetElement(id);
		if (!el) return;
		limitRows(false);
		grid.batchUpdate();
		try {
			if (visible) {
				el.classList.remove('d-none');
				grid.makeWidget(el);
				grid.update(el, { x: state.x, y: state.y, w: state.w, h: state.h, ...minLimits(id) });
			} else if (el.gridstackNode) {
				grid.removeWidget(el, false);
				el.classList.add('d-none');
			}
		} finally {
			grid.batchUpdate(false);
			limitRows(editingNow);
		}
		widgetState.set(id, { ...state, visible });
		el.querySelectorAll('.dashboard-widget-handle').forEach((handle) => { handle.hidden = !editingNow; });
		if (visible) document.dispatchEvent(new CustomEvent('dashboard:widget-shown'));
		document.dispatchEvent(new CustomEvent('dashboard:layout-applied'));
		scheduleFit();
		renderWidgetList();
	}

	function renderWidgetList() {
		const list = $('dashboard-widgets-list');
		list.replaceChildren(
			...DEFAULT_LAYOUT.map((def) => {
				const visible = (widgetState.get(def.id) || {}).visible !== false;
				const item = document.createElement('li');
				item.className = 'list-group-item d-flex align-items-center justify-content-between gap-2';
				const label = document.createElement('span');
				label.textContent = def.label;
				const button = document.createElement('button');
				button.type = 'button';
				button.className = visible ? 'btn btn-sm btn-outline-secondary' : 'btn btn-sm btn-primary';
				button.textContent = visible ? 'Remove' : 'Add';
				button.setAttribute('aria-label', `${visible ? 'Remove' : 'Add'} widget: ${def.label}`);
				button.addEventListener('click', () => toggleWidgetVisibility(def.id, !visible));
				item.append(label, button);
				return item;
			})
		);
	}

	function resetLayout() {
		applyLayout(DEFAULT_LAYOUT.map(defaultEntry));
		scheduleFit();
		renderWidgetList();
		announce('Dashboard layout restored to the default. Select Done to keep it.');
	}

	// Escape and leaving the desktop viewport discard unsaved edits.
	function cancelEditing() {
		setEditing(false);
		applyLayout(savedLayout.map((entry) => ({ ...entry })));
		renderWidgetList();
		scheduleFit();
	}

	// Keyboard alternative to dragging: arrows move the focused widget, Shift+arrows resize it.
	function onHandleKeydown(event) {
		const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
		if (!delta || event.ctrlKey || event.metaKey || event.altKey) return;
		const el = event.currentTarget.closest('.grid-stack-item');
		const node = el && el.gridstackNode;
		if (!node) return;
		event.preventDefault();
		const min = MIN_SIZE.get(el.dataset.widgetId) || { w: 1, h: 1 };
		const next = { x: node.x, y: node.y, w: node.w, h: node.h };
		const previousHeight = node.h;
		if (event.shiftKey) {
			next.w = Math.min(12 - node.x, Math.max(min.w, node.w + delta[0]));
			next.h = Math.min(MAX_Y - node.y, Math.max(min.h, node.h + delta[1]));
		} else {
			next.x = Math.min(12 - node.w, Math.max(0, node.x + delta[0]));
			next.y = Math.min(MAX_Y - node.h, Math.max(0, node.y + delta[1]));
		}
		grid.update(el, next);
		limitRows(true); // slide the reserve with the keyboard move, one row per key press
		const after = el.gridstackNode;
		const previous = widgetState.get(el.dataset.widgetId);
		const heightChanged = event.shiftKey && delta[1] !== 0 && after.h !== previousHeight;
		widgetState.set(el.dataset.widgetId, { id: el.dataset.widgetId, x: after.x, y: after.y, w: after.w, h: after.h,
			visible: true, height_mode: heightChanged ? 'manual' : (previous && previous.height_mode) || 'auto' });
		const def = DEFAULT_LAYOUT.find((d) => d.id === el.dataset.widgetId);
		announce(`${def ? def.label : 'Widget'}: column ${after.x + 1}, row ${after.y + 1}, ${after.w} wide, ${after.h} high.`);
	}

	// The grid starts hidden (template class) so tiles never visibly travel from their template
	// positions to the stored layout; it is revealed once positions and heights are final.
	async function revealGrid() {
		try {
			await Promise.race([
				document.fonts ? document.fonts.ready : Promise.resolve(),
				new Promise((resolve) => setTimeout(resolve, REVEAL_TIMEOUT_MS)),
			]);
			if (fitFrame) { cancelAnimationFrame(fitFrame); fitFrame = 0; }
			fitLayout();
		} finally {
			root.classList.remove('dashboard-grid--loading');
			// Animate user drags and later content changes, not the first paint nor with reduced motion.
			requestAnimationFrame(() => { if (grid) grid.setAnimation(!window.matchMedia(REDUCED_MOTION_QUERY).matches); });
		}
	}

	function buildLayout() {
		cellHeight = cssPixels('--ju-dash-cell-height', DEFAULT_CELL_HEIGHT);
		gridMargin = cssPixels('--ju-dash-gutter', DEFAULT_GRID_MARGIN);
		handleHeight = cssPixels('--ju-dash-handle-height', DEFAULT_HANDLE_HEIGHT);
		grid = window.GridStack.init({
			column: 12,
			cellHeight,
			margin: gridMargin,
			mode: 'float', // no gravity: a tile stays in the free cell it is dropped on
			animate: false,
			disableDrag: true,
			disableResize: true,
			alwaysShowResizeHandle: true,
			handle: '.dashboard-widget-handle',
		}, '#dashboard-grid');
		// The template carries no geometry; place every tile from the registry before anything is revealed.
		applyLayout(DEFAULT_LAYOUT.map(defaultEntry));

		grid.on('change', onGridChange);
		grid.on('dragstart resizestart', () => limitRows(true));
		grid.on('dragstop', () => { limitRows(editingNow); scheduleFit(); });
		grid.on('resizestart', (_event, el) => {
			if (el && el.dataset.widgetId && el.gridstackNode) resizeStartHeights.set(el.dataset.widgetId, el.gridstackNode.h);
		});
		grid.on('resizestop', (_event, el) => {
			if (el && el.dataset.widgetId && el.gridstackNode) {
				const id = el.dataset.widgetId;
				const startHeight = resizeStartHeights.get(id);
				resizeStartHeights.delete(id);
				if (startHeight !== undefined && startHeight !== el.gridstackNode.h) {
					const node = el.gridstackNode;
					const state = widgetState.get(id) || {};
					widgetState.set(id, { ...state, id, x: node.x, y: node.y, w: node.w, h: node.h, visible: true, height_mode: 'manual' });
				}
			}
			limitRows(editingNow);
			scheduleFit();
		});
		root.querySelectorAll('.dashboard-widget-handle').forEach((handle) => handle.addEventListener('keydown', onHandleKeydown));

		// Edit stays disabled until the layout is applied, so Escape/Done never see empty state.
		$('dashboard-edit-toggle').addEventListener('click', () => setEditing(true));
		$('dashboard-edit-done').addEventListener('click', async () => {
			setEditing(false);
			$('dashboard-edit-toggle').disabled = true;
			try { await saveLayout(); } finally { $('dashboard-edit-toggle').disabled = false; }
		});
		$('dashboard-reset-layout').addEventListener('click', resetLayout);
		document.addEventListener('keydown', (event) => {
			if (!editingNow || event.key !== 'Escape' || event.isComposing ||
				event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
				(event.target instanceof Element && event.target.closest('input, textarea, [contenteditable]')) ||
				document.querySelector('.modal.show')) return;
			cancelEditing();
		});
		window.matchMedia(MOBILE_QUERY).addEventListener('change', (event) => { if (event.matches && editingNow) cancelEditing(); });
	}

	async function initLayout() {
		buildLayout();
		let stored = null;
		const controller = new AbortController();
		let timeoutId;
		try {
			const result = await Promise.race([
				request('GET', undefined, controller.signal).then((value) => ({ value })),
				new Promise((resolve) => {
					timeoutId = window.setTimeout(() => resolve({ timedOut: true }), LAYOUT_LOAD_TIMEOUT_MS);
				}),
			]);
			if (result.timedOut) {
				controller.abort();
				notify('Loading saved layout timed out. Using defaults.', false);
			} else {
				stored = result.value ? result.value.layout : null;
			}
		} catch (error) {
			// A failed GET falls back to the factory default; the reveal below still runs.
			notify(messageFrom(error), false);
		} finally {
			window.clearTimeout(timeoutId);
		}
		savedLayout = mergeWithDefault(stored);
		applyLayout(savedLayout);
		$('dashboard-edit-toggle').disabled = false;
		renderWidgetList();
		fitLayout();
		observeContent();
		await revealGrid();
	}

	window.JuDashboardGrid = Object.freeze({ initLayout });
	initLayout();
})();
