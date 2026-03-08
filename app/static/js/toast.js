//
// app/static/js/toast.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

function juToast(message, type = 'info', duration = 4000) {
    if (window._juReconnectState && window._juReconnectState.active) return;
    const icons = { info: 'info', success: 'check_circle', warning: 'warning', danger: 'error' };
    const bgClasses = { info: 'text-bg-primary', success: 'text-bg-success', warning: 'text-bg-warning', danger: 'text-bg-danger' };

    const container = document.getElementById('juToastContainer');
    if (!container) return;

    const existingToasts = container.querySelectorAll('.toast');
    for (let t of existingToasts) {
        const msgSpan = t.querySelector('.toast-body span:last-child');
        if (msgSpan && msgSpan.textContent === message) {
            try {
                const bsToast = bootstrap.Toast.getInstance(t);
                if (bsToast) {
                    bsToast.hide();
                } else {
                    t.remove();
                }
            } catch (e) {
                t.remove();
            }
        }
    }

    const toastEl = document.createElement('div');
    toastEl.className = `toast align-items-center border-0 ${bgClasses[type] || bgClasses.info}`;
    toastEl.setAttribute('role', 'alert');
    toastEl.setAttribute('aria-live', 'assertive');
    toastEl.setAttribute('aria-atomic', 'true');

    const toastBody = document.createElement('div');
    toastBody.className = 'toast-body d-flex align-items-center gap-2';

    const iconSpan = document.createElement('span');
    iconSpan.className = 'material-icons';
    iconSpan.textContent = icons[type] || icons.info;

    const messageSpan = document.createElement('span');
    messageSpan.textContent = message;

    toastBody.appendChild(iconSpan);
    toastBody.appendChild(messageSpan);

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn-close btn-close-white me-2 m-auto';
    closeBtn.setAttribute('data-bs-dismiss', 'toast');
    closeBtn.setAttribute('aria-label', 'Close');

    const flexDiv = document.createElement('div');
    flexDiv.className = 'd-flex';
    flexDiv.appendChild(toastBody);
    flexDiv.appendChild(closeBtn);

    toastEl.appendChild(flexDiv);

    container.appendChild(toastEl);
    const toast = new bootstrap.Toast(toastEl, { delay: duration });
    toast.show();

    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
}
