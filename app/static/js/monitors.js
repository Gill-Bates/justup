//
// app/static/js/monitors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

let monitorsTable;
let monitorForm;
let monitorModal;
let monitorSaveBtn;
let editingMonitorId = null;

// Metrics modal state
let metricsModal;
let currentMetricsMonitorId = null;
let responseTimeChart = null;
let availabilityChart = null;

document.addEventListener('DOMContentLoaded', () => {
    monitorsTable = document.getElementById('monitors-body');
    monitorForm = document.getElementById('monitor-form');
    monitorSaveBtn = document.getElementById('saveMonitorBtn');

    const modalEl = document.getElementById('monitorModal');
    if (modalEl) monitorModal = new bootstrap.Modal(modalEl);

    const metricsModalEl = document.getElementById('metricsModal');
    if (metricsModalEl) metricsModal = new bootstrap.Modal(metricsModalEl);

    const addBtn = document.getElementById('addMonitorBtn');
    if (addBtn) addBtn.addEventListener('click', () => openMonitorModal());

    if (monitorSaveBtn) monitorSaveBtn.addEventListener('click', saveMonitor);

    const typeSelect = document.getElementById('monitor-type');
    if (typeSelect) typeSelect.addEventListener('change', toggleTypeFields);

    // Metrics hours selector
    const metricsHours = document.getElementById('metricsHours');
    if (metricsHours) {
        metricsHours.addEventListener('change', () => {
            if (currentMetricsMonitorId) loadMetrics(currentMetricsMonitorId);
        });
    }

    // Delete metrics button
    const deleteMetricsBtn = document.getElementById('deleteMetricsBtn');
    if (deleteMetricsBtn) {
        deleteMetricsBtn.addEventListener('click', deleteMetrics);
    }

    // Event delegation for edit, delete, and row click
    document.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.edit-monitor');
        if (editBtn) {
            e.preventDefault();
            e.stopPropagation();
            const id = parseInt(editBtn.dataset.id);
            if (id) editMonitor(id);
            return;
        }

        const deleteBtn = e.target.closest('.delete-monitor');
        if (deleteBtn) {
            e.preventDefault();
            e.stopPropagation();
            const id = parseInt(deleteBtn.dataset.id);
            const row = deleteBtn.closest('tr');
            const nameCell = row?.querySelector('td:nth-child(2)');
            const name = nameCell?.textContent?.trim() || 'this monitor';
            if (id) deleteMonitor(id, name);
            return;
        }

        // Row click for metrics
        const row = e.target.closest('.monitor-row');
        if (row && !e.target.closest('.monitor-actions')) {
            const id = parseInt(row.dataset.id);
            if (id) showMetrics(id, row.querySelector('td:nth-child(2)')?.textContent?.trim());
        }
    });

    // Only load monitors if the table is empty (not pre-rendered by server)
    const hasServerData = monitorsTable && monitorsTable.children.length > 0;
    if (!hasServerData) {
        loadMonitors();
    }
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

function openMonitorModal(monitor = null) {
    editingMonitorId = monitor ? monitor.id : null;
    const title = document.getElementById('monitorModalTitle');
    title.textContent = monitor ? 'Edit Monitor' : 'Add Monitor';

    document.getElementById('monitor-name').value = monitor?.name || '';
    document.getElementById('monitor-type').value = monitor?.monitor_type || 'http';
    document.getElementById('monitor-url').value = monitor?.url || '';
    document.getElementById('monitor-hostname').value = monitor?.hostname || '';
    document.getElementById('monitor-port').value = monitor?.port || '';
    document.getElementById('monitor-keyword').value = monitor?.keyword || '';
    document.getElementById('monitor-interval').value = monitor?.interval_seconds || 60;
    document.getElementById('monitor-timeout').value = monitor?.timeout_seconds || 10;
    document.getElementById('monitor-description').value = monitor?.description || '';
    document.getElementById('monitor-active').checked = monitor ? monitor.is_active : true;

    toggleTypeFields();
    monitorModal.show();
}

async function loadMonitors() {
    try {
        const monitors = await api('GET', '/api/monitors');
        renderMonitors(monitors || []);
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

function renderMonitors(monitors) {
    if (!monitorsTable) return;

    if (monitors.length === 0) {
        monitorsTable.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4"><span class="material-icons d-block mb-2" style="font-size:2rem">monitor_heart</span>No monitors yet. Click "Add Monitor" to get started.</td></tr>';
        return;
    }

    monitorsTable.innerHTML = monitors.map(m => {
        const statusBadge = m.status === 'up'
            ? '<span class="badge bg-success">UP</span>'
            : m.status === 'down'
                ? '<span class="badge bg-danger">DOWN</span>'
                : m.status === 'paused'
                    ? '<span class="badge bg-secondary">PAUSED</span>'
                    : '<span class="badge bg-secondary">PENDING</span>';

        const typeLabel = (m.monitor_type || 'http').toUpperCase();
        const urlOrHost = escapeHtml(m.url || m.hostname || '–');
        const responseTime = m.last_response_time_ms ? Math.round(m.last_response_time_ms) : 0;

        return `<tr data-id="${m.id}" class="monitor-row" style="cursor:pointer">
            <td>${statusBadge}</td>
            <td>${escapeHtml(m.name)}</td>
            <td><span class="badge bg-secondary">${typeLabel}</span></td>
            <td class="text-truncate" style="max-width:250px">${urlOrHost}</td>
            <td>${m.interval_seconds || 60}s</td>
            <td>${responseTime} ms</td>
            <td class="monitor-actions" onclick="event.stopPropagation()">
                <button class="btn btn-sm btn-outline-primary edit-monitor" data-id="${m.id}" title="Edit">
                    <span class="material-icons icon-sm">edit</span>
                </button>
                <button class="btn btn-sm btn-outline-danger delete-monitor" data-id="${m.id}" title="Delete">
                    <span class="material-icons icon-sm">delete</span>
                </button>
            </td>
        </tr>`;
    }).join('');
}

function escapeHtml(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
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
        if (editingMonitorId) {
            await api('PATCH', `/api/monitors/${editingMonitorId}`, data);
            juToast('Monitor updated', 'success');
        } else {
            await api('POST', '/api/monitors', data);
            juToast('Monitor created', 'success');
        }
        monitorModal.hide();
        await loadMonitors();
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

async function editMonitor(id) {
    try {
        const monitors = await api('GET', '/api/monitors');
        const monitor = (monitors || []).find(m => m.id === id);
        if (!monitor) {
            juToast('Monitor not found', 'danger');
            return;
        }
        openMonitorModal(monitor);
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

async function deleteMonitor(id, name) {
    const confirmed = await juConfirm(`Delete monitor "${name}"?`, 'danger');
    if (!confirmed) return;

    try {
        await api('DELETE', `/api/monitors/${id}`);
        juToast('Monitor deleted', 'success');
        await loadMonitors();
    } catch (err) {
        juToast(err.message, 'danger');
    }
}

// ============================================================================
// Metrics Visualization
// ============================================================================

async function showMetrics(monitorId, monitorName) {
    currentMetricsMonitorId = monitorId;
    document.getElementById('metricsModalTitle').textContent = `Metrics: ${monitorName || 'Monitor'}`;

    // Show loading state
    document.getElementById('metricsLoading').classList.remove('d-none');
    document.getElementById('metricsContent').classList.add('d-none');
    document.getElementById('metricsEmpty').classList.add('d-none');

    metricsModal.show();
    await loadMetrics(monitorId);
}

async function loadMetrics(monitorId) {
    const hours = parseInt(document.getElementById('metricsHours').value) || 24;

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

        // Render response time chart
        renderResponseTimeChart(data.points);

        // Load availability data
        const availData = await api('GET', `/api/monitors/${monitorId}/metrics?metric=is_up&hours=${hours}`);
        renderAvailabilityChart(availData.points || []);

    } catch (err) {
        document.getElementById('metricsLoading').classList.add('d-none');
        document.getElementById('metricsEmpty').classList.remove('d-none');
        juToast(err.message, 'danger');
    }
}

function renderResponseTimeChart(points) {
    const ctx = document.getElementById('responseTimeChart').getContext('2d');

    // Destroy existing chart
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
                    ticks: {
                        maxTicksLimit: 10,
                        maxRotation: 0
                    }
                },
                y: {
                    beginAtZero: true,
                    title: {
                        display: true,
                        text: 'ms'
                    }
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    mode: 'index',
                    intersect: false
                }
            },
            interaction: {
                mode: 'nearest',
                axis: 'x',
                intersect: false
            }
        }
    });
}

function renderAvailabilityChart(points) {
    const ctx = document.getElementById('availabilityChart').getContext('2d');

    // Destroy existing chart
    if (availabilityChart) {
        availabilityChart.destroy();
    }

    if (points.length === 0) {
        return;
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
                backgroundColor: values.map(v => v === 1 ? 'rgba(25, 135, 84, 0.8)' : 'rgba(220, 53, 69, 0.8)'),
                borderWidth: 0,
                barPercentage: 1.0,
                categoryPercentage: 1.0,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    display: true,
                    ticks: {
                        maxTicksLimit: 10,
                        maxRotation: 0
                    }
                },
                y: {
                    display: false,
                    min: 0,
                    max: 1
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: (ctx) => ctx.raw === 1 ? 'UP' : 'DOWN'
                    }
                }
            }
        }
    });
}

async function deleteMetrics() {
    if (!currentMetricsMonitorId) return;

    const confirmed = await juConfirm('Delete all metrics for this monitor? This cannot be undone.', 'danger');
    if (!confirmed) return;

    try {
        await api('DELETE', `/api/monitors/${currentMetricsMonitorId}/metrics`);
        juToast('Metrics deleted', 'success');

        // Refresh the metrics view
        document.getElementById('metricsContent').classList.add('d-none');
        document.getElementById('metricsEmpty').classList.remove('d-none');

        // Destroy charts
        if (responseTimeChart) {
            responseTimeChart.destroy();
            responseTimeChart = null;
        }
        if (availabilityChart) {
            availabilityChart.destroy();
            availabilityChart = null;
        }
    } catch (err) {
        juToast(err.message, 'danger');
    }
}
