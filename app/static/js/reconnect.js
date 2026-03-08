//
// app/static/js/reconnect.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

const _juReconnectEl = document.getElementById('juReconnectModal');
const _juReconnectModal = _juReconnectEl ? new bootstrap.Modal(_juReconnectEl) : null;

const _juReconnectState = {
    active: false,
    timer: null,
    delayMs: 2000,
    pingUrl: '/api/monitors',
    startupGraceMs: 8000,
    startedAt: Date.now(),
    failCount: 0,
    lastFailAt: 0,
    failThreshold: 3,
};

function _clearReconnectTimer() {
    if (_juReconnectState.timer) {
        clearTimeout(_juReconnectState.timer);
        _juReconnectState.timer = null;
    }
}

function _stopReconnectMode() {
    _clearReconnectTimer();
    _juReconnectState.active = false;
    _juReconnectState.failCount = 0;
    document.body.classList.remove('ju-reconnecting');
    if (_juReconnectModal) {
        document.activeElement?.blur();
        _juReconnectModal.hide();
    }
    window.dispatchEvent(new CustomEvent('ju:reconnect:stop'));
}

async function _probeReconnect() {
    if (!_juReconnectState.active) return;
    try {
        const res = await fetch(_juReconnectState.pingUrl, {
            method: 'GET',
            headers: {
                'X-CSRF-Token': getCsrfToken(),
                'Cache-Control': 'no-cache',
            },
            credentials: 'same-origin',
            cache: 'no-store',
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return;
        }
        if (res.ok) {
            _stopReconnectMode();
            return;
        }
    } catch (_) {
        // Keep polling
    }

    _juReconnectState.timer = setTimeout(_probeReconnect, _juReconnectState.delayMs);
}

function _startReconnectMode() {
    if (_juReconnectState.active) return;

    const now = Date.now();

    if (now - (_juReconnectState.lastFailAt || 0) > 500) {
        _juReconnectState.failCount++;
    }
    _juReconnectState.lastFailAt = now;

    const timeSinceStart = now - _juReconnectState.startedAt;
    if (timeSinceStart < _juReconnectState.startupGraceMs &&
        _juReconnectState.failCount < _juReconnectState.failThreshold) {
        return;
    }

    _juReconnectState.active = true;
    document.body.classList.add('ju-reconnecting');
    if (window._juModal) {
        document.activeElement?.blur();
        window._juModal.hide();
    }

    const toastContainer = document.getElementById('juToastContainer');
    if (toastContainer) toastContainer.innerHTML = '';

    if (_juReconnectModal) _juReconnectModal.show();
    window.dispatchEvent(new CustomEvent('ju:reconnect:start'));
    _probeReconnect();
}
