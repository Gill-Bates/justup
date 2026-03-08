//
// app/static/js/modal.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

const _juModalEl = document.getElementById('juModal');
window._juModal = _juModalEl ? new bootstrap.Modal(_juModalEl) : null;

const _juIcons = {
    info: { icon: 'info', color: 'var(--ju-primary)' },
    success: { icon: 'check_circle', color: 'var(--ju-success)' },
    warning: { icon: 'warning', color: 'var(--ju-warning)' },
    danger: { icon: 'error', color: 'var(--ju-danger)' },
    confirm: { icon: 'help', color: 'var(--ju-primary)' },
    prompt: { icon: 'edit', color: 'var(--ju-primary)' },
};

function _showModal({ title, message, type, showCancel, showInput, inputDefault, inputPlaceholder, inputType }) {
    if (window._juReconnectState && window._juReconnectState.active) {
        if (showInput) return Promise.resolve(null);
        if (showCancel) return Promise.resolve(false);
        return Promise.resolve(true);
    }
    return new Promise(resolve => {
        document.querySelectorAll('.modal.show').forEach(modal => {
            const bsModal = bootstrap.Modal.getInstance(modal);
            if (bsModal && modal.id !== 'juModal' && !modal.querySelector('form')) {
                document.activeElement?.blur();
                bsModal.hide();
            }
        });

        const cfg = _juIcons[type] || _juIcons.info;
        document.getElementById('juModalTitle').textContent = title;
        document.getElementById('juModalMessage').textContent = message;
        const iconEl = document.getElementById('juModalIcon');
        iconEl.textContent = cfg.icon;
        iconEl.style.color = cfg.color;

        const cancelBtn = document.getElementById('juModalCancel');
        cancelBtn.classList.toggle('d-none', !showCancel);

        const inputWrap = document.getElementById('juModalInputWrap');
        const inputEl = document.getElementById('juModalInput');
        inputWrap.classList.toggle('d-none', !showInput);
        if (showInput) {
            inputEl.value = inputDefault || '';
            inputEl.placeholder = inputPlaceholder || '';
            inputEl.type = inputType || 'text';
        }

        const okBtn = document.getElementById('juModalOk');
        okBtn.className = 'btn ' + (type === 'danger' ? 'btn-danger' : 'btn-primary');
        okBtn.textContent = showCancel || showInput ? 'Confirm' : 'OK';

        let settled = false;
        function finish(val) {
            if (settled) return;
            settled = true;
            const modalEl = document.getElementById('juModal');

            if (!modalEl.classList.contains('show')) {
                resolve(val);
                return;
            }

            const fallback = setTimeout(() => resolve(val), 500);
            modalEl.addEventListener('hidden.bs.modal', () => {
                clearTimeout(fallback);
                resolve(val);
            }, { once: true });
            document.activeElement?.blur();
            window._juModal.hide();
        }

        okBtn.onclick = () => {
            if (showInput) finish(inputEl.value);
            else finish(true);
        };

        cancelBtn.onclick = () => finish(showInput ? null : false);
        const closeBtn = document.getElementById('juModalClose');
        if (closeBtn) closeBtn.onclick = () => finish(showInput ? null : !showCancel);

        if (showInput) {
            inputEl.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); okBtn.click(); } };
        }

        window._juModal.show();
        if (showInput) setTimeout(() => inputEl.focus(), 300);
    });
}

function juAlert(message, type = 'info') {
    const titles = { info: 'Info', success: 'Success', warning: 'Warning', danger: 'Error' };
    return _showModal({ title: titles[type] || 'Info', message, type, showCancel: false, showInput: false });
}

function juConfirm(message, type = 'confirm') {
    return _showModal({ title: 'Confirm', message, type, showCancel: true, showInput: false });
}

function juPrompt(message, { defaultValue = '', placeholder = '', inputType = 'text' } = {}) {
    return _showModal({ title: 'Input', message, type: 'prompt', showCancel: true, showInput: true, inputDefault: defaultValue, inputPlaceholder: placeholder, inputType });
}
