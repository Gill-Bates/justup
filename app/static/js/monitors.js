//
// app/static/js/monitors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

let monitorsTable;
let monitorForm;
let monitorModal;
let monitorSaveBtn;
let editingMonitorId = null;

document.addEventListener('DOMContentLoaded', () => {
    monitorsTable = document.getElementById('monitors-body');
    monitorForm = document.getElementById('monitor-form');
    monitorSaveBtn = document.getElementById('saveMonitorBtn');

    const modalEl = document.getElementById('monitorModal');
    if (modalEl) monitorModal = new bootstrap.Modal(modalEl);

    const addBtn = document.getElementById('addMonitorBtn');
    if (addBtn) addBtn.addEventListener('click', () => openMonitorModal());

    if (monitorSaveBtn) monitorSaveBtn.addEventListener('click', saveMonitor);

    const typeSelect = document.getElementById('monitor-type');
    if (typeSelect) typeSelect.addEventListener('change', toggleTypeFields);

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

        // Row click navigates to monitor detail page
        const row = e.target.closest('.monitor-row');
        if (row && !e.target.closest('.monitor-actions')) {
            const id = parseInt(row.dataset.id);
            if (id) window.location.href = `/ui/monitors/${id}`;
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
