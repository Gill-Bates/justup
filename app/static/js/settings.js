//
// app/static/js/settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dependencies:
//   - api.js (api function)
//   - toast.js (juToast)

document.addEventListener('DOMContentLoaded', () => {
    // General settings
    const saveSettingsBtn = document.getElementById('saveSettingsBtn');
    if (saveSettingsBtn) saveSettingsBtn.addEventListener('click', saveSettings);

    // Swagger toggle
    const swaggerToggle = document.getElementById('enable-swagger');
    if (swaggerToggle) {
        swaggerToggle.addEventListener('change', saveSwaggerSetting);
    }
    const copySwaggerBtn = document.getElementById('btn-copy-swagger-url');
    if (copySwaggerBtn) {
        copySwaggerBtn.addEventListener('click', copySwaggerUrl);
    }

    loadSettings();
});

// ─── General Settings ──────────────────────────────────────────

async function loadSettings() {
    try {
        const settings = await api('GET', '/api/settings');
        const portField = document.getElementById('setting-port');
        const intervalField = document.getElementById('setting-interval');
        const retentionField = document.getElementById('setting-retention');

        if (portField) portField.value = settings?.gui_port || '8000';
        if (intervalField) intervalField.value = settings?.check_interval_default || '60';
        if (retentionField) retentionField.value = settings?.tsdb_retention_days || '90';

        // Swagger toggle
        const swaggerToggle = document.getElementById('enable-swagger');
        if (swaggerToggle) {
            swaggerToggle.style.transition = 'none';
            swaggerToggle.checked = toBool(settings?.enable_swagger);
            swaggerToggle.offsetHeight; // force reflow
            swaggerToggle.style.transition = '';
        }
        updateSwaggerUrl();
    } catch (err) {
        console.warn('Failed to load settings:', err);
    }
}

function toBool(val) {
    return ['1', 'true', 'yes', 'on'].includes(String(val || '').trim().toLowerCase());
}

function updateSwaggerUrl() {
    const target = document.getElementById('swagger-url');
    if (!target) return;
    const port = document.getElementById('setting-port')?.value || location.port || '8000';
    const protocol = location.protocol;
    const urlHost = location.hostname;
    target.textContent = `${protocol}//${urlHost}:${port}/swagger`;
}

async function saveSettings() {
    const btn = document.getElementById('saveSettingsBtn');
    if (btn) btn.disabled = true;

    try {
        const port = parseInt(document.getElementById('setting-port')?.value, 10) || 8000;
        const interval = parseInt(document.getElementById('setting-interval')?.value, 10) || 60;
        const retention = parseInt(document.getElementById('setting-retention')?.value, 10) || 90;

        await api('PUT', '/api/settings', {
            gui_port: String(port),
            check_interval_default: String(interval),
            tsdb_retention_days: String(retention),
        });
        juToast('Settings saved', 'success');
        updateSwaggerUrl();
    } catch (err) {
        juToast(err.message, 'danger');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function saveSwaggerSetting() {
    const swaggerToggle = document.getElementById('enable-swagger');
    if (!swaggerToggle) return;
    try {
        await api('PUT', '/api/settings', {
            enable_swagger: swaggerToggle.checked ? '1' : '0',
        });
        juToast(swaggerToggle.checked ? 'Swagger UI enabled' : 'Swagger UI disabled', 'success');
    } catch (err) {
        swaggerToggle.checked = !swaggerToggle.checked;
        juToast(err.message, 'danger');
    }
}

function copySwaggerUrl() {
    const target = document.getElementById('swagger-url');
    if (!target) return;
    navigator.clipboard.writeText(target.textContent).then(() => {
        juToast('URL copied', 'info');
    }).catch(() => {
        juToast('Failed to copy URL', 'warning');
    });
}
