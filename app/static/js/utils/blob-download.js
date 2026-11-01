//
// app/static/js/utils/blob-download.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/** Blob URL mechanics shared by client-side downloads. */
// Revoking right after click() can cancel the download in Safari, so the URL outlives the click.
const REVOKE_DELAY_MS = 10000;
/**
 * Save a blob through a temporary link and release its URL after a delay.
 * @param {Blob} blob
 * @param {string} filename
 */
export function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}
