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
    monitorsTable = document.getElementById('monitors-tbody');
    monitorForm = document.getElementById('monitor-form');
    monitorSaveBtn = document.getElementById('saveMonitorBtn');

    const modalEl = document.getElementById('monitorModal');
    if (modalEl) monitorModal = new bootstrap.Modal(modalEl);

    const addBtn = document.getElementById('addMonitorBtn');
    if (addBtn) addBtn.addEventListener('click', () => openMonitorModal());

    if (monitorSaveBtn) monitorSaveBtn.addEventListener('click', saveMonitor);

    const typeSelect = document.getElementById('monitor-type');
    if (typeSelect) typeSelect.addEventListener('change', toggleTypeFields);

    loadMonitors();
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
    document.getElementById('monitor-type').value = monitor?.type || 'http';
    document.getElementById('monitor-url').value = monitor?.url || '';
    document.getElementById('monitor-hostname').value = monitor?.hostname || '';
    document.getElementById('monitor-port').value = monitor?.port || '';
    document.getElementById('monitor-keyword').value = monitor?.keyword || '';
    document.getElementById('monitor-interval').value = monitor?.interval || 60;
    document.getElementById('monitor-timeout').value = monitor?.timeout || 10;
    document.getElementById('monitor-description').value = monitor?.description || '';
    document.getElementById('monitor-active').checked = monitor ? monitor.active : true;

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
        monitorsTable.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No monitors configured yet.</td></tr>';
        return;
    }

    monitorsTable.innerHTML = monitors.map(m => {
        const statusBadge = m.status === 'up'
            ? '<span class="badge bg-success">Up</span>'
            : m.status === 'down'
                ? '<span class="badge bg-danger">Down</span>'
                : '<span class="badge bg-secondary">Paused</span>';

        const typeLabel = (m.type || 'http').toUpperCase();

        return `<tr>
            <td>${statusBadge}</td>
            <td>${escapeHtml(m.name)}</td>
            <td><span class="badge bg-dark">${typeLabel}</span></td>
            <td>${escapeHtml(m.url || m.hostname || '-')}</td>
            <td>${m.interval || 60}s</td>
            <td>
                <button class="btn btn-sm btn-outline-primary me-1" onclick="editMonitor(${m.id})" title="Edit">
                    <span class="material-icons" style="font-size:16px">edit</span>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="deleteMonitor(${m.id}, '${escapeHtml(m.name)}')" title="Delete">
                    <span class="material-icons" style="font-size:16px">delete</span>
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
            await api('PUT', `/api/monitors/${editingMonitorId}`, data);
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
