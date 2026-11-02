//
// app/static/js/dashboard-monitoring.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
	'use strict';
	const POLL_MS = 30000;
	const HISTORY_POLL_MS = 300000;
	const BACKLOG_MS = 300000;
	const CRITICAL_MS = 1800000;
	const DAY_MS = 86400000;
	const count = (value) => Number.isInteger(value) && value >= 0;
	const timestamp = (value) => {
		if (value === null || value === undefined || value === '') return NaN;
		if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
		const text = String(value);
		return Date.parse(/(?:Z|[+-]\d\d:\d\d)$/i.test(text) ? text : text.replace(' ', 'T') + 'Z');
	};
	// Failed deliveries that still count as current: the server's 24 h window, else every stored failure.
	const failedCount = (data) => (count(data.failed_recent) ? data.failed_recent : data.failed);
	function queueState(data, now = Date.now()) {
		if (!data || !['pending', 'processing', 'failed', 'stuck_processing'].every((key) => count(data[key])) ||
			!Number.isFinite(data.processing_timeout_seconds) || data.processing_timeout_seconds <= 0 ||
			(data.pending > 0 && !Number.isFinite(timestamp(data.oldest_pending))) ||
			(data.next_retry_at !== null && !Number.isFinite(timestamp(data.next_retry_at)))) return ['Status unavailable', 'secondary'];
		const scheduler = data.services && data.services.monitor_scheduler;
		if (!scheduler || typeof scheduler.state !== 'string') return ['Status unavailable', 'secondary'];
		const age = data.pending ? now - timestamp(data.oldest_pending) : 0;
		if (age < 0) return ['Status unavailable', 'secondary'];
		if (data.stuck_processing > 0 || age >= CRITICAL_MS) return ['Stalled', 'danger'];
		if (failedCount(data) > 0) return ['Handoff failed', 'danger'];
		if (scheduler.state !== 'running') return ['Worker degraded', 'warning'];
		if (age >= BACKLOG_MS) return ['Backlog', 'warning'];
		return data.pending + data.processing > 0 ? ['Processing', 'info'] : ['Queue clear', 'success'];
	}
	window.JuDashboardMonitoring = Object.freeze({ queueState, timestamp });
	const root = document.getElementById('dashboard-grid');
	if (!root) return;
	const tiles = [...root.querySelectorAll('.grid-stack-item')].filter((tile) => tile.querySelector('.dashboard-monitor-body'));
	const pending = new Map();
	const nextLoad = new Map();
	let timer = null;
	let stopped = false;
	let chart = null;
	const visible = (tile) => !stopped && document.visibilityState !== 'hidden' && !tile.classList.contains('d-none');
	const node = (tag, text, className) => {
		const element = document.createElement(tag);
		if (text !== undefined) element.textContent = text;
		if (className) element.className = className;
		return element;
	};
	const number = (body, value, label) => {
		const element = node('div', String(value), 'dashboard-monitor-number');
		element.setAttribute('aria-label', label); element.title = label; body.append(element);
	};
	function row(body, name, value, symbol = 'schedule', tone = 'body-secondary') {
		const element = node('div', undefined, `dashboard-monitor-row text-${tone}`);
		element.title = name;
		element.setAttribute('aria-label', `${name}: ${value}`);
		const icon = node('span', symbol, 'material-icons');
		icon.setAttribute('aria-hidden', 'true');
		element.append(icon, node('span', name), node('span', value)); body.append(element);
	}
	function unavailable(body, label = 'Status unavailable') {
		row(body, label, '–', 'help_outline');
	}
	async function json(url, signal) {
		const response = await window.apiFetch(url, { signal, headers: { Accept: 'application/json' } });
		if (!response.ok) throw new Error('Request failed');
		return response.json();
	}
	function queue(body, data) {
		const [label, tone] = queueState(data);
		const status = node('div', undefined, `d-flex align-items-center gap-1 text-${tone}`);
		const icon = node('span', label === 'Queue clear' ? 'check_circle' : label === 'Status unavailable' ? 'help_outline' : label === 'Processing' ? 'sync' : 'warning', 'material-icons');
		icon.setAttribute('aria-hidden', 'true'); status.append(icon, node('span', label)); body.append(status);
		if (label !== 'Status unavailable') {
			number(body, data.pending + data.processing, 'outstanding messages');
			const metrics = node('div', undefined, 'dashboard-monitor-metrics');
			const names = { pending: 'Pending', processing: 'Processing', failed: 'Failed (24 h)' };
			for (const key of ['pending', 'processing', 'failed']) {
				const value = key === 'failed' ? failedCount(data) : data[key];
				const cell = node('div'); cell.title = names[key];
				cell.setAttribute('aria-label', `${names[key]}: ${value}`);
				cell.append(node('span', names[key]), node('strong', String(value))); metrics.append(cell);
			}
			body.append(metrics);
		} else number(body, '–', 'Delivery queue status unavailable');
	}
	function alerts(body, data) {
		if (!count(data.total) || !Array.isArray(data.items)) throw new Error('Invalid alerts');
		number(body, data.total, 'open monitoring alerts');
	}
	function monitors(body, data, kind) {
		if (!Array.isArray(data.targets)) throw new Error('Invalid monitors');
		const enabled = data.targets.filter((target) => target.is_enabled);
		if (kind === 'cert-expiry') {
			const certificates = enabled.filter((target) => target.enable_cert_expiration && Number.isFinite(target.status.cert_days_left)).sort((left, right) => left.status.cert_days_left - right.status.cert_days_left);
			if (certificates.length) number(body, certificates.filter((target) => target.status.cert_days_left <= 30).length, 'certificates expiring within 30 days');
			certificates.slice(0, 4).forEach((target) => {
				const days = target.status.cert_days_left;
				const checked = timestamp(target.status.cert_checked_at);
				const stale = !Number.isFinite(checked) || Date.now() - checked > DAY_MS || checked > Date.now();
				const tone = stale ? 'warning' : days <= 7 ? 'danger' : days <= 30 ? 'warning' : 'success';
				row(body, target.name + (stale ? ': stale result' : ''), `${days} d`, stale ? 'history' : days <= 7 ? 'error_outline' : 'verified_user', tone);
			});
			if (!certificates.length) unavailable(body, 'No certificate measurements');
		} else if (kind === 'slowest-monitors') {
			const targets = enabled.filter((target) => target.enable_http_check && Number.isFinite(target.status.http_latency_ms)).sort((left, right) => right.status.http_latency_ms - left.status.http_latency_ms).slice(0, 5);
			const maximum = targets.length ? Math.max(1, targets[0].status.http_latency_ms) : 1;
			for (const target of targets) {
				row(body, target.name, `${Math.round(target.status.http_latency_ms)} ms`, 'speed');
				const bar = node('meter'); bar.min = 0; bar.max = maximum; bar.value = target.status.http_latency_ms;
				bar.setAttribute('aria-label', `${target.name}: latest HTTP latency`); body.append(bar);
			}
			if (!targets.length) unavailable(body, 'No HTTP measurements');
		} else {
			const unmeasured = enabled.filter((target) => !Number.isFinite(timestamp(target.status.updated_at)));
			const stale = enabled.filter((target) => Number.isFinite(timestamp(target.status.updated_at)) && Date.now() - timestamp(target.status.updated_at) > Math.max(60000, target.interval_seconds * 3000));
			number(body, stale.length, 'overdue monitors');
			stale.slice(0, 3).forEach((target) => row(body, target.name, `${Math.floor((Date.now() - timestamp(target.status.updated_at)) / 60000)} min`, 'schedule', 'warning'));
			row(body, 'Awaiting first check', String(unmeasured.length), 'hourglass_empty');
			row(body, 'Disabled monitors', String(data.targets.length - enabled.length), 'pause_circle_outline');
		}
	}
	function quality(body, data) {
		if (!Array.isArray(data)) throw new Error('Invalid quality history');
		const points = data.filter((point) => Number.isFinite(timestamp(point.ts))).sort((left, right) => timestamp(left.ts) - timestamp(right.ts));
		if (!points.some((point) => Number.isFinite(point.score))) {
			if (chart) { chart.destroy(); chart = null; }
			body.replaceChildren(); unavailable(body, 'No quality measurements'); return;
		}
		const latest = [...points].reverse().find((point) => Number.isFinite(point.score));
		const samples = [];
		points.forEach((point, index) => {
			if (index && timestamp(point.ts) - timestamp(points[index - 1].ts) > 300000) samples.push({ ts: point.ts, score: null });
			samples.push(point);
		});
		const labels = samples.map((point) => new Date(timestamp(point.ts)).toLocaleTimeString());
		const values = samples.map((point) => Number.isFinite(point.score) ? point.score : null);
		const color = getComputedStyle(root).getPropertyValue('--bs-primary').trim();
		if (chart) {
			body.querySelector('.dashboard-monitor-number').textContent = String(latest.score);
			chart.data.labels = labels;
			chart.data.datasets[0].data = values;
			chart.data.datasets[0].borderColor = color;
			chart.update('none');
		} else {
			body.replaceChildren(); number(body, latest.score, 'Latest quality score');
			const wrapper = node('div', undefined, 'dashboard-monitor-chart'); const canvas = node('canvas');
			canvas.setAttribute('role', 'img'); wrapper.append(canvas); body.append(wrapper);
		chart = new window.Chart(canvas, {
			type: 'line',
			data: { labels, datasets: [{ label: 'Quality score', data: values, borderColor: color, borderWidth: 2, pointRadius: 0, spanGaps: false }] },
			options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { y: { min: 0, max: 100 } } },
		});
		}
		chart.canvas.setAttribute('aria-label', `Quality score over the last 24 hours. Latest: ${latest.score}. Missing measurements are gaps.`);
	}
	function uptime(body, data) {
		if (!Number.isFinite(data.coverage_percent) || data.coverage_percent < 0 || data.coverage_percent > 100) throw new Error('Invalid uptime coverage');
		const percent = data.uptime_percent;
		if (data.coverage_percent < 95 || !Number.isFinite(percent) || percent < 0 || percent > 100) unavailable(body, 'Not enough uptime data');
		else {
			number(body, `${percent.toFixed(2)}%`, 'Uptime during observed time over the last 24 hours');
			const bar = node('progress'); bar.max = 100; bar.value = percent; bar.setAttribute('aria-label', 'Uptime over the last 24 hours'); body.append(bar);
		}
		row(body, 'Measurement coverage over the last 24 hours', `${data.coverage_percent.toFixed(1)}%`, 'data_usage');
	}
	async function load(tile, force = false) {
		if (!visible(tile)) return;
		if (pending.has(tile)) { if (!force) return; pending.get(tile).abort(); }
		const controller = new AbortController(); pending.set(tile, controller);
		const id = tile.dataset.widgetId; const body = tile.querySelector('.dashboard-monitor-body');
		try {
			let data;
			if (id === 'delivery-queue') data = await json('/settings/notification-queue/stats', controller.signal);
			else if (id === 'alerts-open') data = await json('/alerts?status=open&page_size=5', controller.signal);
			else if (id === 'quality-trend') data = await json('/quality/history?hours=24', controller.signal);
			else if (id === 'uptime-day') data = await json('/me/dashboard-layout/uptime', controller.signal);
			else data = await json('/me/dashboard-layout/monitors', controller.signal);
			if (pending.get(tile) !== controller || !visible(tile)) return;
			if (id !== 'quality-trend') body.replaceChildren();
			if (id === 'delivery-queue') queue(body, data);
			else if (id === 'alerts-open') alerts(body, data);
			else if (id === 'quality-trend') quality(body, data);
			else if (id === 'uptime-day') uptime(body, data);
			else monitors(body, data, id);
		} catch (error) {
			if (controller.signal.aborted || pending.get(tile) !== controller || !visible(tile)) return;
			if (id === 'quality-trend' && chart) { chart.destroy(); chart = null; }
			body.replaceChildren(); unavailable(body);
		} finally { if (pending.get(tile) === controller) pending.delete(tile); }
	}
	function refresh(force = false) {
		clearTimeout(timer); timer = null;
		for (const tile of tiles) {
			if (visible(tile)) {
				if (force || Date.now() >= (nextLoad.get(tile) || 0)) {
					nextLoad.set(tile, Date.now() + (['uptime-day', 'quality-trend'].includes(tile.dataset.widgetId) ? HISTORY_POLL_MS : POLL_MS));
					load(tile, force);
				}
			} else {
				nextLoad.delete(tile);
				if (pending.has(tile)) { pending.get(tile).abort(); pending.delete(tile); }
				if (tile.dataset.widgetId === 'quality-trend' && chart) { chart.destroy(); chart = null; }
			}
		}
		if (!stopped && tiles.some(visible)) timer = setTimeout(refresh, POLL_MS);
	}
	const visibilityChanged = () => refresh(true); const layoutChanged = () => refresh(false);
	document.addEventListener('visibilitychange', visibilityChanged);
	document.addEventListener('dashboard:layout-applied', layoutChanged);
	document.addEventListener('dashboard:widget-shown', layoutChanged);
	const beforeSwap = (event) => { if (event.detail.target && event.detail.target.contains(root)) cleanup(); };
	function cleanup() {
		stopped = true; clearTimeout(timer);
		for (const controller of pending.values()) controller.abort();
		pending.clear(); if (chart) chart.destroy();
		document.removeEventListener('visibilitychange', visibilityChanged);
		document.removeEventListener('dashboard:layout-applied', layoutChanged);
		document.removeEventListener('dashboard:widget-shown', layoutChanged);
		document.body.removeEventListener('htmx:beforeSwap', beforeSwap);
		window.removeEventListener('pagehide', cleanup);
	}
	document.body.addEventListener('htmx:beforeSwap', beforeSwap);
	window.addEventListener('pagehide', cleanup, { once: true });
	refresh();
})();