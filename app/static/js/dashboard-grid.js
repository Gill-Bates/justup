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
	const CELL_HEIGHT = 72;
	// Half the tile gutter (--ju-dash-gap in dashboard.css): GridStack insets every tile by it.
	const GRID_MARGIN = 8;
	// Mirrors DASHBOARD_MAX_Y on the server; no widget may cross this row bound (y + h <= MAX_Y).
	const MAX_Y = 1000;
	const REVEAL_TIMEOUT_MS = 2500; // below the CSS failsafe (4s)
	// Keep in sync with the stacked block in dashboard.css (Bootstrap lg breakpoint).
	const MOBILE_QUERY = '(max-width: 991.98px)';
	const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
	const PARK_ROWS = 10000;

	const { registry } = window.JuDashboardRegistry;
	const DEFAULT_LAYOUT = registry.map((def) => ({
		id: def.id, x: def.defaultX, y: def.defaultY,
		w: def.defaultSize.w, h: def.defaultSize.h,
		visible: def.defaultVisible, label: def.label,
	}));
	const MIN_SIZE = new Map(registry.map((def) => [def.id, { w: def.minW, h: def.minH }]));
	const defaultEntry = (def) => ({ id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: def.visible });

	let grid = null;
	const widgetState = new Map(); // id -> { id, x, y, w, h, visible }
	let savedLayout = null;
	let fitFrame = 0;
	const fitRows = new Map(); // id -> rows the last fit set; such a height is content-driven, not stored
	let fitting = false;
	let applyingLayout = false;
	let editingNow = false;
	let slotLayer = null;

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

	async function request(method, body) {
		const options = { method, headers: { Accept: 'application/json' } };
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
		if (stored && stored.version === 1 && Array.isArray(stored.widgets)) {
			for (const widget of stored.widgets) {
				if (widget && typeof widget.id === 'string') byId.set(widget.id, widget);
			}
		}
		const inRange = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
		const merged = DEFAULT_LAYOUT.map((def) => {
			const saved = byId.get(def.id);
			if (!saved) return defaultEntry(def);
			const valid = inRange(saved.x, 11) && inRange(saved.y, MAX_Y) &&
				Number.isInteger(saved.w) && saved.w >= 1 && saved.w <= 12 &&
				Number.isInteger(saved.h) && saved.h >= 1 && saved.h <= 100 &&
				saved.x + saved.w <= 12 && saved.y + saved.h <= MAX_Y;
			if (!valid) return defaultEntry(def);
			return { id: def.id, x: saved.x, y: saved.y, w: saved.w, h: saved.h, visible: saved.visible !== false };
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
	function applyLayout(layout) {
		applyingLayout = true;
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
					grid.update(el, { x: entry.x, y: entry.y + PARK_ROWS, w: entry.w, h: entry.h });
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
		}
	}

	// Content height without the tile's stretch (cards use h-100), so the measure can shrink again.
	function naturalHeight(inner) {
		const { height, alignSelf } = inner.style;
		inner.style.setProperty('height', 'auto', 'important'); // Bootstrap's h-100 is !important
		inner.style.alignSelf = 'flex-start';
		const natural = inner.offsetHeight + 1; // +1 covers the rounding of offsetHeight
		inner.style.height = height;
		inner.style.alignSelf = alignSelf;
		return natural;
	}

	function neededRows(el) {
		const content = el.querySelector('.grid-stack-item-content');
		const inner = content && content.querySelector(':scope > :not(.dashboard-widget-header)');
		if (!inner) return 1;
		const frame = el.offsetHeight - content.offsetHeight;
		return Math.ceil((naturalHeight(inner) + frame) / CELL_HEIGHT);
	}

	// The stored height is a floor: the fit only grows a tile past it, never below.
	function fitWidget(el, needed) {
		const node = el.gridstackNode;
		if (!node) return;
		const id = el.dataset.widgetId;
		const stored = (widgetState.get(id) || {}).h || 1;
		const rows = Math.max(stored, needed, 1);
		fitRows.set(id, rows);
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
			grid.batchUpdate();
			const items = [...document.querySelectorAll('.grid-stack-item[data-widget-id]')]
				.filter((el) => el.gridstackNode && !el.classList.contains('d-none'));
			const needed = new Map(items.map((el) => [el, neededRows(el)]));
			for (const el of items) fitWidget(el, needed.get(el));
		} finally {
			grid.batchUpdate(false);
			fitting = false;
		}
	}

	function scheduleFit() {
		if (!fitFrame) fitFrame = requestAnimationFrame(fitLayout);
	}

	// Anything that changes a tile's content size re-runs the fit: window width, htmx swaps, text
	// changes and the icon font arriving late.
	function observeContent() {
		window.addEventListener('resize', scheduleFit);
		if (document.fonts) document.fonts.ready.then(scheduleFit);
		new MutationObserver(scheduleFit).observe(root, {
			// Content swaps/text need a new fit. GridStack and edit-mode class toggles do not, and
			// reacting to those during interaction can queue redundant layout passes and flicker.
			subtree: true, childList: true, characterData: true,
		});
		if (!('ResizeObserver' in window)) return;
		const observer = new ResizeObserver(scheduleFit);
		observer.observe(root);
		root.querySelectorAll('.grid-stack-item-content > :not(.dashboard-widget-header)').forEach((inner) => observer.observe(inner));
	}

	// Read the saved layout from the settled grid, not from change-event deltas, which can miss
	// neighbours pushed by GridStack.
	function syncStateFromGrid() {
		for (const def of DEFAULT_LAYOUT) {
			const el = widgetElement(def.id);
			const node = el && el.gridstackNode;
			const prev = widgetState.get(def.id);
			if (!node || !prev || prev.visible === false) continue;
			const h = fitRows.get(def.id) === node.h ? prev.h : node.h;
			widgetState.set(def.id, { id: def.id, x: node.x, y: node.y, w: node.w, h, visible: true });
		}
	}

	// Only the known structured props travel to the server; nothing from the DOM is ever sent.
	async function saveLayout() {
		syncStateFromGrid();
		const widgets = DEFAULT_LAYOUT.map((def) => {
			const state = widgetState.get(def.id);
			return { id: def.id, x: state.x, y: state.y, w: state.w, h: state.h, visible: state.visible };
		});
		try {
			await request('PUT', { version: 1, widgets });
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
			const h = prev && fitRows.get(id) === item.h ? prev.h : item.h;
			widgetState.set(id, { id, x: item.x, y: item.y, w: item.w, h, visible: true });
		}
	}

	// Edit mode shows every grid cell as an empty dashed slot (dashboard.css), one per cell for the
	// current grid height.
	function syncSlots() {
		if (!editingNow || !slotLayer) return;
		const wanted = Math.max(0, Math.round(root.clientHeight / CELL_HEIGHT)) * grid.getColumn();
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
		setSlots(editing);
		root.querySelectorAll('.dashboard-widget-header').forEach((header) => { header.hidden = !editing; });
		$('dashboard-edit-toggle').hidden = editing;
		$('dashboard-edit-widgets').hidden = !editing;
		$('dashboard-reset-layout').hidden = !editing;
		$('dashboard-edit-done').hidden = !editing;
		grid.enableMove(editing);
		grid.enableResize(editing);
		// The focused button just got hidden; hand focus to its counterpart (not after a viewport change).
		const next = $(editing ? 'dashboard-edit-done' : 'dashboard-edit-toggle');
		if (next.offsetParent) next.focus();
	}

	function toggleWidgetVisibility(id, visible) {
		const state = widgetState.get(id) || DEFAULT_LAYOUT.find((def) => def.id === id);
		const el = widgetElement(id);
		if (!el) return;
		grid.batchUpdate();
		try {
			if (visible) {
				el.classList.remove('d-none');
				grid.makeWidget(el);
				grid.update(el, { x: state.x, y: state.y, w: state.w, h: state.h });
			} else if (el.gridstackNode) {
				grid.removeWidget(el, false);
				el.classList.add('d-none');
			}
		} finally {
			grid.batchUpdate(false);
		}
		widgetState.set(id, { ...state, visible });
		el.querySelectorAll('.dashboard-widget-header').forEach((header) => { header.hidden = !editingNow; });
		if (visible) document.dispatchEvent(new CustomEvent('dashboard:widget-shown'));
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
		if (event.shiftKey) {
			next.w = Math.min(12 - node.x, Math.max(min.w, node.w + delta[0]));
			next.h = Math.min(MAX_Y - node.y, Math.max(min.h, node.h + delta[1]));
		} else {
			next.x = Math.min(12 - node.w, Math.max(0, node.x + delta[0]));
			next.y = Math.min(MAX_Y - node.h, Math.max(0, node.y + delta[1]));
		}
		grid.update(el, next);
		const after = el.gridstackNode;
		widgetState.set(el.dataset.widgetId, { id: el.dataset.widgetId, x: after.x, y: after.y, w: after.w, h: after.h, visible: true });
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
		grid = window.GridStack.init({
			column: 12,
			cellHeight: CELL_HEIGHT,
			margin: GRID_MARGIN,
			float: true, // no gravity: a tile stays in the free cell it is dropped on
			animate: false,
			disableDrag: true,
			disableResize: true,
			alwaysShowResizeHandle: true,
			handle: '.dashboard-widget-header',
		}, '#dashboard-grid');

		grid.on('change', onGridChange);
		grid.on('dragstop', scheduleFit);
		grid.on('resizestop', scheduleFit);
		root.querySelectorAll('.dashboard-widget-header').forEach((header) => header.addEventListener('keydown', onHandleKeydown));

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
		try {
			const result = await request('GET');
			stored = result ? result.layout : null;
		} catch (error) {
			// A failed GET falls back to the factory default; the reveal below still runs.
			notify(messageFrom(error), false);
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
