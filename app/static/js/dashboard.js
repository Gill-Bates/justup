//
// app/static/js/dashboard.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

let refreshInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    // Only load dashboard if the table is empty (not pre-rendered by server)
    const tbody = document.getElementById('monitors-table-body');
    const hasServerData = tbody && tbody.children.length > 0 && !tbody.querySelector('.text-muted');

    if (!hasServerData) {
        loadDashboard();
    }

    refreshInterval = setInterval(loadDashboard, 30000);

    window.addEventListener('ju:reconnect:stop', loadDashboard);
});

async function loadDashboard() {
    try {
        const monitors = await api('GET', '/api/monitors');
        updateStats(monitors || []);
        renderMonitorTable(monitors || []);
    } catch (err) {
        // Silently fail — reconnect logic handles display
    }

    try {
        const incidents = await api('GET', '/api/monitors/incidents/recent');
        renderIncidents(incidents || []);
    } catch (_) { }
}

function updateStats(monitors) {
    const up = monitors.filter(m => m.is_active && m.status === 'up').length;
    const down = monitors.filter(m => m.is_active && m.status === 'down').length;
    const paused = monitors.filter(m => !m.is_active || m.status === 'paused').length;
    const total = monitors.length;

    setText('stat-up', up);
    setText('stat-down', down);
    setText('stat-paused', paused);
    setText('stat-total', total);
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function escapeHtml(text) {
    const el = document.createElement('span');
    el.textContent = text;
    return el.innerHTML;
}

function renderMonitorTable(monitors) {
    const tbody = document.getElementById('monitors-table-body');
    if (!tbody) return;

    if (monitors.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-4"><span class="material-icons d-block mb-2" style="font-size:2rem">monitor_heart</span>No monitors configured yet. <a href="/ui/monitors">Add your first monitor</a></td></tr>';
        return;
    }

    tbody.innerHTML = monitors.map(m => {
        const badge = m.status === 'up'
            ? '<span class="badge bg-success">UP</span>'
            : m.status === 'down'
                ? '<span class="badge bg-danger">DOWN</span>'
                : m.status === 'paused'
                    ? '<span class="badge bg-secondary">PAUSED</span>'
                    : '<span class="badge bg-secondary">PENDING</span>';

        const typeLabel = (m.monitor_type || 'http').toUpperCase();
        const responseTime = m.last_response_time_ms != null
            ? `${Math.round(m.last_response_time_ms)} ms`
            : '–';

        return `<tr>
            <td>${badge}</td>
            <td>${escapeHtml(m.name)}</td>
            <td><span class="badge bg-secondary">${typeLabel}</span></td>
            <td>${responseTime}</td>
            <td>–</td>
        </tr>`;
    }).join('');
}

function renderIncidents(incidents) {
    const tbody = document.getElementById('dashboard-incidents-tbody');
    if (!tbody) return;

    if (incidents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">No recent incidents.</td></tr>';
        return;
    }

    tbody.innerHTML = incidents.map(inc => {
        const resolved = inc.resolved_at
            ? '<span class="badge bg-success">Resolved</span>'
            : '<span class="badge bg-danger">Ongoing</span>';

        return `<tr>
            <td>${escapeHtml(inc.monitor_name || '-')}</td>
            <td>${resolved}</td>
            <td>${inc.started_at || '-'}</td>
            <td>${inc.duration || '-'}</td>
        </tr>`;
    }).join('');
}
