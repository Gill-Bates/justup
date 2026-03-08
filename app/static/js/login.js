//
// app/static/js/login.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Login Page - Main Logic and Utilities
 */

let errorAlert;
let loginForm;
let submitBtn;
let usernameField;
let passwordField;

function getCsrfToken() {
    return document.body.dataset.csrfToken;
}

async function apiCall(url, body) {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': getCsrfToken(),
        },
        body: JSON.stringify(body),
        credentials: 'same-origin',
    });

    let data = {};
    try {
        data = await response.json();
    } catch (jsonError) {
        throw new Error('Invalid response from server');
    }

    if (!response.ok) {
        throw new Error(data.detail || 'Request failed');
    }

    return data;
}

function setBusy(buttonEl, busyText) {
    if (!buttonEl.dataset.originalHtml) {
        buttonEl.dataset.originalHtml = buttonEl.innerHTML;
    }
    buttonEl.disabled = true;
    buttonEl.innerHTML = `<span class="spinner-border spinner-border-sm me-2"></span>${busyText}`;
    buttonEl.setAttribute('aria-busy', 'true');
}

function clearBusy(buttonEl, idleText) {
    buttonEl.disabled = false;
    if (buttonEl.dataset.originalHtml) {
        buttonEl.innerHTML = buttonEl.dataset.originalHtml;
        delete buttonEl.dataset.originalHtml;
    } else {
        buttonEl.textContent = idleText;
    }
    buttonEl.removeAttribute('aria-busy');
}

function showError(message) {
    errorAlert.textContent = message;
    errorAlert.classList.remove('d-none');
    if (usernameField) usernameField.setAttribute('aria-invalid', 'true');
    if (passwordField) passwordField.setAttribute('aria-invalid', 'true');
    errorAlert.setAttribute('tabindex', '-1');
    errorAlert.focus();
}

function hideError() {
    errorAlert.classList.add('d-none');
    if (usernameField) usernameField.removeAttribute('aria-invalid');
    if (passwordField) passwordField.removeAttribute('aria-invalid');
    errorAlert.removeAttribute('tabindex');
}

async function handleLogin(e) {
    e.preventDefault();

    const username = usernameField.value;
    const password = passwordField.value;

    hideError();
    setBusy(submitBtn, 'Signing in...');

    try {
        const data = await apiCall('/api/auth/login', { username, password });

        if (data?.data?.mfa_required) {
            showMfaForm(username, data.data.mfa_token);
            return;
        }

        window.location.href = '/ui/dashboard';

    } catch (error) {
        showError(error.message);
    } finally {
        clearBusy(submitBtn, 'Sign In');
    }
}

function initThemeToggle() {
    const themeToggleBtn = document.getElementById('theme-toggle-btn');
    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', toggleTheme);
    }
}

function initLoginPage() {
    errorAlert = document.getElementById('error-alert');
    loginForm = document.getElementById('login-form');
    submitBtn = document.getElementById('submit-btn');
    usernameField = document.getElementById('username');
    passwordField = document.getElementById('password');

    if (loginForm) {
        loginForm.addEventListener('submit', handleLogin);
    }

    initThemeToggle();
    initMfa();
    initPasskeys();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLoginPage);
} else {
    initLoginPage();
}
