//
// tools/eslint.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import js from "@eslint/js";
import globals from "globals";

export default [
  // Vendored libraries and minified bundles are not ours to lint.
  { ignores: ["app/static/js/**/*.min.js", "app/static/js/leaflet.js"] },
  js.configs.recommended,
  {
    files: ["app/static/js/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "script", globals: { ...globals.browser } },
    // Scripts share globals across files (no module system yet, DEC-04/phase 5), so
    // cross-file unused/undefined checks would only produce noise.
    rules: { "no-unused-vars": "off", "no-undef": "off", "no-empty": ["error", { allowEmptyCatch: true }] },
  },
  {
    files: ["app/static/js/settings.signal-actions.js", "app/static/js/settings.storage.js", "app/static/js/settings.signal.js", "app/static/js/settings.signal-core.js", "app/static/js/settings.signal-status.js", "app/static/js/settings.signal-registration.js", "app/static/js/settings.main.js", "app/static/js/settings.core.js", "app/static/js/utils/tag-input.js", "app/static/js/utils/tag-input-core.js"],
    languageOptions: { sourceType: "module" },
  },
];
