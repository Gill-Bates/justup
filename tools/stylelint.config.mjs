//
// tools/stylelint.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

export default {
  extends: ["stylelint-config-standard"],
  rules: {
    // Existing stylesheet uses its own naming; enforce correctness, not naming style.
    "selector-class-pattern": null,
    "custom-property-pattern": null,
    "keyframes-name-pattern": null,
    "selector-id-pattern": null,
    // Ordering/duplication cleanup is a phase-5 stylesheet refactor, not a phase-0 gate.
    "no-descending-specificity": null,
    "no-duplicate-selectors": null,
    "declaration-block-no-duplicate-properties": null,
    "declaration-property-value-keyword-no-deprecated": null,
  },
};
