//
// app/static/js/monitor-detail.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
    'use strict';

    let monitorId;
    let monitorModal;
    let responseTimeChart = null;
    let availabilityChart = null;

    document.addEventListener('DOMContentLoaded', () => {
        monitorId = parseInt(document.querySelector('.ju-page')?.dataset?.monitorId, 10);
        if (!monitorId) return;

        const modalEl = document.getElementById('monitorModal');
        if (modalEl) monitorModal = new bootstrap.Modal(modalEl);

        // Edit button
        const editBtn = document.getElementById('editMonitorBtn');
        if (editBtn) editBtn.addEventListener('click', openEditModal);

        // Delete button
        const deleteBtn = document.getElementById('deleteMonitorBtn');
        if (deleteBtn) deleteBtn.addEventListener('click', deleteMonitor);

        // Save button
        const saveBtn = document.getElementById('saveMonitorBtn');
        if (saveBtn) saveBtn.addEventListener('click', saveMonitor);

        // Type select
        const typeSelect = document.getElementById('monitor-type');
        if (typeSelect) typeSelect.addEventListener('change', toggleTypeFields);

        // Metrics hours selector
        const metricsHours = document.getElementById('metricsHours');
        if (metricsHours) {
            metricsHours.addEventListener('change', () => loadMetrics());
        }

        // Delete metrics button
        const deleteMetricsBtn = document.getElementById('deleteMetricsBtn');
        if (deleteMetricsBtn) {
            deleteMetricsBtn.addEventListener('click', deleteMetrics);
        }

        // Load initial data
        loadMetrics();
        loadIncidents();
        loadUptime();
    });

    function toggleTypeFields() {
        const type = document.getElementById('monitor-type').value;
        const urlGroup = document.getElementById('url-group');
        const hostGroup = document.getElementById('hostname-group');
        const portGroup = document.getElementById('port-group');
        const keywordGroup = document.getElementById('keyword-group');

        urlGroup.classList.toggle('d-none', !['http', 'keyword'].includes(type));
        hostGroup.classList.toggle('d-none', !['tcp', 'ping', 'dns'].includes(type));
        portGroup.classList.toggle('d-none', type !== 'tcp');
        keywordGroup.classList.toggle('d-none', type !== 'keyword');
    }

    async function openEditModal() {
        try {
            const monitor = await api('GET', `/api/monitors/${monitorId}`);
            if (!monitor) {
                juToast('Monitor not found', 'danger');
                return;
            }

            document.getElementById('monitor-name').value = monitor.name || '';
            document.getElementById('monitor-type').value = monitor.monitor_type || 'http';
            document.getElementById('monitor-url').value = monitor.url || '';
            document.getElementById('monitor-hostname').value = monitor.hostname || '';
            document.getElementById('monitor-port').value = monitor.port || '';
            document.getElementById('monitor-keyword').value = monitor.keyword || '';
            document.getElementById('monitor-interval').value = monitor.interval_seconds || 60;
            document.getElementById('monitor-timeout').value = monitor.timeout_seconds || 10;
            document.getElementById('monitor-description').value = monitor.description || '';
            document.getElementById('monitor-active').checked = monitor.is_active !== false;

            toggleTypeFields();
            monitorModal.show();
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    async function saveMonitor() {
        const type = document.getElementById('monitor-type').value;
        const data = {
            name: document.getElementById('monitor-name').value.trim(),
            monitor_type: type,
            interval_seconds: parseInt(document.getElementById('monitor-interval').value) || 60,
            timeout_seconds: parseInt(document.getElementById('monitor-timeout').value) || 10,
            description: document.getElementById('monitor-description').value.trim() || null,
            is_active: document.getElementById('monitor-active').checked,
        };

        if (['http', 'keyword'].includes(type)) {
            data.url = document.getElementById('monitor-url').value.trim();
        }
        if (['tcp', 'ping', 'dns'].includes(type)) {
            data.hostname = document.getElementById('monitor-hostname').value.trim();
        }
        if (type === 'tcp') {
            data.port = parseInt(document.getElementById('monitor-port').value) || null;
        }
        if (type === 'keyword') {
            data.keyword = document.getElementById('monitor-keyword').value.trim();
        }

        if (!data.name) {
            juToast('Name is required', 'warning');
            return;
        }

        try {
            await api('PATCH', `/api/monitors/${monitorId}`, data);
            juToast('Monitor updated', 'success');
            monitorModal.hide();
            // Reload page to show updated data
            window.location.reload();
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    async function deleteMonitor() {
        const confirmed = await juConfirm('Delete this monitor?', 'danger');
        if (!confirmed) return;

        try {
            await api('DELETE', `/api/monitors/${monitorId}`);
            juToast('Monitor deleted', 'success');
            window.location.href = '/ui/monitors';
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    // ─── Metrics ───────────────────────────────────────────────────

    async function loadMetrics() {
        const hours = parseInt(document.getElementById('metricsHours').value) || 24;

        document.getElementById('metricsLoading').classList.remove('d-none');
        document.getElementById('metricsContent').classList.add('d-none');
        document.getElementById('metricsEmpty').classList.add('d-none');

        try {
            const data = await api('GET', `/api/monitors/${monitorId}/metrics?metric=response_time&hours=${hours}`);

            document.getElementById('metricsLoading').classList.add('d-none');

            if (!data.points || data.points.length === 0) {
                document.getElementById('metricsContent').classList.add('d-none');
                document.getElementById('metricsEmpty').classList.remove('d-none');
                return;
            }

            document.getElementById('metricsContent').classList.remove('d-none');
            document.getElementById('metricsEmpty').classList.add('d-none');

            renderResponseTimeChart(data.points);

            // Load availability data
            const availData = await api('GET', `/api/monitors/${monitorId}/metrics?metric=is_up&hours=${hours}`);
            renderAvailabilityChart(availData.points || []);

        } catch (err) {
            document.getElementById('metricsLoading').classList.add('d-none');
            document.getElementById('metricsEmpty').classList.remove('d-none');
            console.warn('Failed to load metrics:', err);
        }
    }

    function renderResponseTimeChart(points) {
        const ctx = document.getElementById('responseTimeChart').getContext('2d');

        if (responseTimeChart) {
            responseTimeChart.destroy();
        }

        const labels = points.map(p => new Date(p.ts).toLocaleString());
        const values = points.map(p => p.value);

        responseTimeChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [{
                    label: 'Response Time (ms)',
                    data: values,
                    borderColor: '#03a806',
                    backgroundColor: 'rgba(3, 168, 6, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.3,
                    pointRadius: points.length > 100 ? 0 : 3,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        display: true,
                        ticks: { maxTicksLimit: 10, maxRotation: 0 }
                    },
                    y: {
                        beginAtZero: true,
                        title: { display: true, text: 'ms' }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: { mode: 'index', intersect: false }
                },
                interaction: { mode: 'nearest', axis: 'x', intersect: false }
            }
        });
    }

    function renderAvailabilityChart(points) {
        const ctx = document.getElementById('availabilityChart').getContext('2d');

        if (availabilityChart) {
            availabilityChart.destroy();
        }

        const labels = points.map(p => new Date(p.ts).toLocaleString());
        const values = points.map(p => p.value);

        availabilityChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Status',
                    data: values,
                    backgroundColor: values.map(v => v === 1 ? '#198754' : '#dc3545'),
                    barPercentage: 1.0,
                    categoryPercentage: 1.0,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: false },
                    y: {
                        display: true,
                        min: 0,
                        max: 1,
                        ticks: {
                            stepSize: 1,
                            callback: v => v === 1 ? 'Up' : 'Down'
                        }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => ctx.raw === 1 ? 'Up' : 'Down'
                        }
                    }
                }
            }
        });
    }

    async function deleteMetrics() {
        const confirmed = await juConfirm('Delete all metrics for this monitor?', 'danger');
        if (!confirmed) return;

        try {
            await api('DELETE', `/api/monitors/${monitorId}/metrics`);
            juToast('Metrics deleted', 'success');
            await loadMetrics();
        } catch (err) {
            juToast(err.message, 'danger');
        }
    }

    // ─── Incidents ─────────────────────────────────────────────────

    async function loadIncidents() {
        const tbody = document.getElementById('incidents-body');
        if (!tbody) return;

        try {
            const incidents = await api('GET', `/api/monitors/${monitorId}/incidents`);

            if (!incidents || incidents.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-3">No incidents recorded.</td></tr>';
                return;
            }

            tbody.innerHTML = incidents.map(inc => {
                const isResolved = !!inc.resolved_at;
                const statusBadge = isResolved
                    ? '<span class="badge bg-success">Resolved</span>'
                    : '<span class="badge bg-danger">Open</span>';

                const startedAt = new Date(inc.created_at).toLocaleString();
                let duration = '–';
                if (isResolved) {
                    const start = new Date(inc.created_at);
                    const end = new Date(inc.resolved_at);
                    const diffMs = end - start;
                    duration = formatDuration(diffMs);
                } else {
                    const start = new Date(inc.created_at);
                    const diffMs = Date.now() - start;
                    duration = formatDuration(diffMs) + ' (ongoing)';
                }

                const error = escapeHtml(inc.error_message || '–');

                return `<tr>
                    <td>${statusBadge}</td>
                    <td>${startedAt}</td>
                    <td>${duration}</td>
                    <td class="text-truncate" style="max-width:300px" title="${error}">${error}</td>
                </tr>`;
            }).join('');
        } catch (err) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center text-danger py-3">Failed to load incidents.</td></tr>';
            console.warn('Failed to load incidents:', err);
        }
    }

    function formatDuration(ms) {
        const seconds = Math.floor(ms / 1000);
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ${minutes % 60}m`;
        const days = Math.floor(hours / 24);
        return `${days}d ${hours % 24}h`;
    }

    // ─── Uptime ────────────────────────────────────────────────────

    async function loadUptime() {
        try {
            const data = await api('GET', `/api/monitors/${monitorId}/metrics?metric=is_up&hours=24`);
            const points = data.points || [];

            if (points.length === 0) {
                document.getElementById('stat-uptime').textContent = '–';
                return;
            }

            const upCount = points.filter(p => p.value === 1).length;
            const uptime = ((upCount / points.length) * 100).toFixed(1);
            document.getElementById('stat-uptime').textContent = `${uptime}%`;
        } catch (err) {
            document.getElementById('stat-uptime').textContent = '–';
        }
    }

    function escapeHtml(text) {
        const el = document.createElement('span');
        el.textContent = text;
        return el.innerHTML;
    }

})();
