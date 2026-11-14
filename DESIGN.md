# Design

## 1 Scope, status and normative language

Status: Active. All ten parts are consolidated; open implementation requirements are listed in 10.6. Updated 2026-11-08. This contract governs the justUp! UI: the shared shell, dashboard, login and password-change screens, targets (list, wizard, editor, detail), notifications, groups, settings, About, shared components and every future UI route. It stabilizes the existing visual and interaction language and is not a redesign. A change to a shared rule or component updates this document and its owning check in the same change.

### 1.1 Normative language

| Word | Meaning |
| --- | --- |
| MUST, MUST NOT | A verifiable requirement. It names a check, or states why it cannot be automated. |
| SHOULD, SHOULD NOT | The default. A deviation needs a reason in the change that deviates. |
| MAY | A permitted variation. Choosing it needs no exception. |

- **DC-NORM-1** Only a sentence with one of these words, or a row of a rule table, is a requirement. Other text describes how something works, or gives rationale, and never adds a requirement.
- **DC-NORM-2** Rules have stable IDs (`DC-<AREA>-<n>`). A test, a code comment or a later change references the ID. An ID is never reused for a different rule.
- **DC-NORM-3** Absolute words (always, every, never, only, exactly) hold inside the scope the sentence names. Where no scope is named, the scope is the section that contains the rule. An absolute rule has no exception unless an exception row (DC-EXC-1) exists.
- **DC-NORM-4** A numeric requirement states unit, scope and measurement condition. It states a tolerance only where the measurement needs one; the tolerance is a named constant in the owning test, not contract text.

### 1.2 Measurement vocabulary

These terms are separate. A claim in this document names the configuration that proves it.

| Term | Definition |
| --- | --- |
| CSS viewport | Width x height in CSS pixels, for example 320x640, 390x844, 1280x720. |
| Effective browser zoom Z | CSS viewport (W/Z)x(H/Z) with device pixel ratio Z, written "effective 200 % zoom (640x360 CSS px at DPR 2)". It is a viewport proxy for reflow, not the browser zoom feature; real zoom coverage is reported separately. |
| Device pixel ratio (DPR) | Pixel density only. DPR alone proves no reflow. |
| Text enlargement | Root font size scaled to 200 % at an unchanged CSS viewport. |
| Reflow | No horizontal page scroll at a CSS viewport 320 px wide. |
| Evidence level E-ENGINE | A desktop or emulated-device run in Chromium, WebKit or Firefox. |
| Evidence level E-IOS | A run on a real iPhone or iPad, or in the iOS Simulator. Playwright WebKit is E-ENGINE. |

- **DC-MEAS-1** A zoom or reflow check MUST use a CSS viewport reduced by Z (or 320 CSS px for reflow). A label such as "200 % zoom" MUST NOT name a run that only changes DPR or only changes the viewport.
- **DC-MEAS-2** The shell audit and the modal checks MUST also run once with text enlargement at 1280x720 and 390x844 and lose no content or function. The shared profile helper `textEnlargement` and `applyTextEnlargement` (`tests/layout/test-support.mjs`) scales the root font size; `tests/layout/targets-search.spec.mjs` uses it and passes; the shell audit runs it on request (`JUSTUP_LAYOUT_TEXT=1`, viewports `css-1280x720-text-200pct` and `css-390x844-text-200pct`). Measured 2026-11-08 in Chromium: 10 of 26 pages at 1280x720 and 25 of 26 at 390x844 report findings (`exceeds-container-x`, `document-scrolls`, `doc-overflow-x`, `clipped-x`, `focus-obscured`), so the requirement is NOT met and the profile is not a default gate (open, 10.6). Re-measured 2026-11-10: the same counts (10 of 26 at 1280x720, 25 of 26 at 390x844). The modal checks use the profile (`tests/layout/modal.spec.mjs`): at 1280x720 all dialogs lose no content or action; at 390x844 the dialog stays inside the viewport and can be closed, but the title is clipped and the footer actions leave the dialog (open).
- **DC-MEAS-3** Behaviour specific to iOS (keyboard, browser chrome, safe areas, gestures) MUST be reported as "unverified" until an E-IOS run is recorded. E-ENGINE results do not change that status.

## 2 Precedence and exceptions

- **DC-PREC-1** When rules conflict, the higher level wins:
  1. Unchangeable product decisions and security boundaries (section 3).
  2. Global invariants (sections 4 and 5: tokens, shell and scrolling, focus; the state taxonomy of section 6).
  3. Component contracts (section 6). They refine a global invariant and MUST NOT silently contradict it.
  4. Feature contracts (section 9) and exception rows.
  5. Implementation, tests and code comments. They are evidence, not norms.
- **DC-PREC-2** A conflict is resolved in the same change by correcting the lower item or by adding an exception row. Existing code or a passing test is never a reason to keep a contradiction.
- **DC-PREC-3** A level-1 rule changes only by an explicit product decision. It is never relaxed through an exception row.

**Exception procedure.**

- **DC-EXC-1** An exception is a row in the exception table of the rule it modifies, with: ID (`DC-EX-<AREA>-<n>`), scope (surface and selector), rationale, owner (file) and the test that proves it. A row without a test is invalid.
- **DC-EXC-2** An exception is bounded by its scope and sets no precedent. A second surface with the same need extends the row or adds a shared variant (DC-EXT-2).
- **DC-EXC-3** Section 9 indexes all exception rows. The row stays with its rule, so each rule has one home.

## 3 Design principles and unchangeable product decisions

justUp! is a clear, dependable monitoring console. Health and failures are easy to scan. WireBuddy's documentation site (Material for MkDocs) is a visual reference for restraint (simple type, white space, flat outlined surfaces); it is not copied. Operators scan the dashboard and investigate targets, mostly on desktop; administrators manage targets, recipients and settings; a phone may be used to check status or dismiss an alert (assumption from the current routes).

### 3.1 Principles

- **DC-PRIN-1** The UI uses one shared token system (section 4), visible status labels next to colour, and a compact navigation hierarchy.
- **DC-PRIN-2** Colour MUST NOT be the only carrier of status, selection or series identity. A second cue (text, shape, position, marker) is required (taxonomy and charts: section 6).
- **DC-PRIN-3** Light, dark and system themes use the same structure. Theme changes update the wordmark and shared surfaces immediately; system mode tracks the operating-system preference.
- **DC-PRIN-4** Decorative effects MUST NOT compete with status information. Radii are modest, borders thin, shadows follow the elevation scale (DC-TOK-5).
- **DC-PRIN-5** Content width suits the application: monitoring grids get more room than document prose. The reference's clear hierarchy, compact header, thin borders and quiet typography are adopted without copying its documentation structure or text.
- **DC-PRIN-6** Labels are short, concrete English terms already established in the product (for example Dashboard, Targets). WireBuddy product copy is not imported.

### 3.2 Unchangeable product decisions

A change to one of these needs a new product decision (DC-PREC-3).

| ID | Decision | Scope |
| --- | --- | --- |
| DC-PD-1 | Teal accent `#0e7a65` (light) and `#1fc7a6` (dark) for links, primary actions and active states. | All themes and pages. The only exception is the rendered mail content inside the isolated preview frame (`settings.smtp-template.js`, link colour `#0d6efd`); editor controls and app UI keep the accent. |
| DC-PD-2 | IBM Plex Sans (local asset), Material Icons (local set), local CSS. No external font, icon or CSS request at runtime. | Application pages. |
| DC-PD-3 | Navigation: Dashboard; Manage (Targets, Notifications, Groups); Settings (admin only); About (all users). Desktop sidebar rail and mobile drawer stay. No new top-level navigation pattern as a side effect of a page. | Application shell. |
| DC-PD-4 | The full wordmark appears once, in the content top bar to the right of the sidebar; the sidebar shows the compact mark. Both link to the dashboard. | Application shell. |
| DC-PD-5 | Cards and tiles have no collapse handle (DC-CARD-1). | `.card` tiles, `.ui-card`, dashboard widget cards. |
| DC-PD-6 | Dashboard layouts use the V2 JSON contract. | Dashboard persistence (section 10). |
| DC-PD-7 | Frames that show user-defined HTML are sandboxed without `allow-scripts`. This is a security boundary. | SMTP template editor and preview (section 10). |
| DC-PD-8 | Mail and PDF metadata (check types, target details) is neutral grey. Status colours belong to the status banner and status text. The default mail header is the logo alone with the status accent line below. | Mail and PDF adapters. |
| DC-PD-9 | Every user-visible message of warning severity starts with the warning sign, as on Settings > Backup ("Do not rename the backup file."). Rule DC-WARN-1 to DC-WARN-4 below. | Inline hints, form-level warnings, banners, warning toasts, modal warnings, warning state messages. |

- **DC-WARN-1** The sign is the Material Icon `warning` (the existing Backup implementation: `material-icons`, `aria-hidden="true"`, inline before the text, warning token colour). It is decorative; the message text carries the meaning. A template or script MUST NOT write the glyph or the icon markup by hand: one shared component (owner `ui.css`, class `ju-warning`) produces it, through a template macro for server-rendered messages and a JS helper for script-built ones (both render paths exist today; neither writes markup of its own), and the message contains no sign of its own, so the sign appears exactly once.
- **DC-WARN-2** Scope is warning severity only. Info, success and error messages keep their own markers (taxonomy, section 6). A destructive confirmation whose consequence text is danger severity is not a warning message.
- **DC-WARN-3** Exceptions are exception rows (DC-EXC-1); none exists yet.
- **DC-WARN-4** Check: a markup or layout test over the settings and main pages fails on a warning-styled element (`text-warning`, `alert-warning`, warning toast) that lacks the sign or has it twice, plus a unit test of the helper. The component and these tests are not yet implemented (open).

### 3.3 Cards, disclosures and optional field groups

Three different things exist. Each has one rule set; none is a variant of another.

- **DC-CARD-1 Card.** A card or tile (`.card`, `.ui-card`, a dashboard widget card) MUST have an always present frame and body region, and a plain header (title, optional status and actions). The header MUST NOT carry a chevron, handle, `<details>`/`<summary>`, `data-bs-toggle="collapse"`, `aria-expanded` or `aria-controls`, and MUST NOT be clickable as a whole. The one `aria-controls` allowed in a card header is on the enable switch of an optional field group (DC-CARD-3). A card that would need to collapse is first specified here for all cards, then built once as a shared component.
- **DC-CARD-2 Disclosure.** A disclosure (`<details>` or accordion) MAY exist inside or between cards only where this document names it: target groups on Targets, the HTTP option sections of the target editor, the Nmap disclosures of the port picker, the in-card disclosures of Settings > Signal and SMTP (Device Actions, message templates, available placeholders; the "More options" voice disclosure of the Signal recipient form), the "Advanced settings" disclosure of the target wizard (`target_wizard.html`), the custom date range disclosure of the target detail time range selector (`time_range_selector.html`) and the hop-group toggles (`aria-expanded`) of the traceroute map. A new disclosure is added to this list in the same change. A disclosure follows the disclosure pattern (button with `aria-expanded`, or native `<details>`).
- **DC-CARD-3 Optional field group.** A body that an enable switch hides (recipient channels, check cards of the target editor, the wizard, the Signal panel) is a field group, not a collapsible card. The switch is a `role="switch"` checkbox in the same header or row and MAY carry `aria-controls`; the group uses the `hidden` attribute through `utils/optional-groups.js`. A hidden group leaves the tab order and the accessibility tree, and its value is not submitted as active.
- **DC-CARD-4 Equal height.** Cards in the same content-grid row have equal outer height set by the tallest card; the shorter one stretches without a fixed height. Cards stacked in one column keep their natural height. Dashboard widgets follow their own GridStack sizing contract (section 9).
- **DC-CARD-5 Corners.** A card keeps its rounded frame without clipping content: the inner radius is `--bs-card-inner-border-radius` (= `--bs-card-border-radius` minus the border width) and the header, body, footer or table that touches a corner takes it. A card header with no visible content below it has no divider line. A component MUST NOT set its own card radius or `overflow` to repair this; it changes `--bs-card-border-radius`.
- **DC-CARD-6 Overflow.** `overflow: hidden` is allowed on `.ui-card`, dashboard widget cards and modals. Other cards let menus, suggestions and tooltips leave the card.
- Checks: `tests/layout/card-corners.spec.mjs`, `tests/layout/card-overlays.spec.mjs`, `tests/test_ui_accessibility.py` (`test_card_headers_have_no_collapse_toggle`), the header assertions in `tests/layout/public-egress.spec.mjs`.
- Layout invariants for frame, header and content: DC-CARD-7 to DC-CARD-9 in 6.5.

### 3.4 Extension rules

These rules apply to every page, fragment and reusable component, including surfaces without a dedicated section.

- **DC-EXT-1** Extend the nearest existing page or component pattern. Do not introduce a parallel design system or restyle a feature to make it look new. Application pages use the `base.html` shell and the shared `ui-page`, page-heading, card, grid, stack, form, table and tab patterns. Login and password change keep their standalone layouts and use the shared type, theme, controls and tokens.
- **DC-EXT-2** Reuse an existing component or variant first. A shared variant is added only for an intentional, reusable difference. A one-off colour, spacing, radius, shadow or type variant for a single page is not allowed.
- **DC-EXT-3** A page arranges its own content but MUST NOT redefine the application palette, common controls, page spacing or the appearance of a shared component. Domain content and layout stay in the feature surface.
- **DC-EXT-4** A visual change MUST NOT change a route, permission boundary, saved preference, API contract, monitoring behaviour, keyboard support, element IDs or Bootstrap hooks.
- **DC-EXT-5** An interactive component defines each applicable state: default, hover, focus, active, disabled, loading, empty, success, error. A state that does not apply is stated as not applicable in the component's spec or test. Status distinctions follow the taxonomy of section 6.
- **DC-EXT-6** New presentation rules go to the shared or feature stylesheet, not to inline styles. When an existing component is touched, obsolete local overrides are removed; unrelated legacy markup is not refactored.

## 4 Design tokens and technical owners

### 4.1 Token rules

- **DC-TOK-1** Colour tokens (`--ju-*`, Bootstrap theme variables) are defined in `gentelella-layout.css`, once for light and once for dark under `data-bs-theme`. Component sizing and spacing tokens (`--ui-*`) are defined in the `:root` block of `ui.css`. A feature stylesheet defines only tokens for its own geometry.
- **DC-TOK-2** A component or feature token that carries a shared meaning MUST alias the shared token (`--td-shadow: var(--ju-shadow)`), not define a second value. A raw colour value (hex, `rgb()`) appears only in token definitions, in the status palette adapters (DC-TOK-7) and in `forced-colors` or `print` rules that use system colours. A component never writes its own shadow colour.
- **DC-TOK-3** A value that CSS and JavaScript both need (breakpoint, row height, reserved rows, geometry of an interactive widget) has one source. The other side reads it (a CSS custom property read with `getComputedStyle`, or a constant exported once), or a named test asserts that both are equal. JavaScript MUST NOT calibrate geometry independently of the CSS that draws it.
- **DC-TOK-4** `!important` is allowed only to override third-party inline or utility styles (Bootstrap utilities, GridStack or Chart.js inline styles), with a comment that names the target. It MUST NOT be used for tabs, modals or focus. Existing uses are reviewed when their rule is touched; no sweep.
- **DC-TOK-5** Elevation has three levels: `--ui-shadow-subtle` (inline surfaces), `--ju-shadow` (cards) and `--ju-shadow-raised` (floating panels such as popovers and modals; `--ui-modal-shadow` is the modal alias). Each is defined for light and dark. A component MUST NOT introduce a fourth level.
- **DC-TOK-6** Non-essential animation and transition MUST be disabled or reduced under `prefers-reduced-motion: reduce`. Shared motion is guarded in `ui.css` (section "Accessibility"); a feature stylesheet guards its own animations. Motion is otherwise limited to the existing navigation transitions.
- **DC-TOK-7** Render adapters stay separate: web (CSS tokens), PDF reports and e-mail (`app/utils/status_palette.py`; mail clients need inline colours). An adapter MUST NOT read the CSS. Equal semantic colours (success, warning, danger, neutral) map one to one by name; the web side is `--ju-*` and `--ju-text-*`, the PDF and e-mail side is `status_palette.py`. Tests: `tests/test_status_palette.py`, `tests/test_status_dot_parity.py`, `tests/test_email_template.py`. The palette in 6.1 (Status palette) is the record of the mapping.
- **DC-TOK-8** Fonts: `font-display: fallback` for IBM Plex Sans; no second or engine-specific font. Icons are local (DC-PD-2). Check: `tests/layout/load-stability.spec.mjs`.

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-TOK-1 | DC-TOK-1 (colour tokens in `gentelella-layout.css`) | `--ju-text-success`, `--ju-text-warning` and the other `--ju-text-*` tokens | Text shades of the status components live next to the badge and dot rules they serve. | `ui.css` | `tests/test_status_dot_parity.py` |

### 4.2 Owners

- **DC-OWN-1** Stylesheet responsibilities: `gentelella-layout.css` owns the application palette, shell, navigation and the baseline buttons, forms, cards and tables. `ui.css` owns shared page layout, spacing, page primitives, modal presentation and component refinements. `dashboard.css` owns widget presentation; the widget registry owns widget identities, labels, defaults and minimum sizes; `dashboard-grid.js` owns layout interaction and persistence. `target-detail.css`, `target-wizard.css`, `notifications.css` and `traceroute-map.css` own their feature layout and presentation. New feature rules use these owners and the shared tokens and MUST NOT reproduce shared palette, type, spacing, control or surface rules locally.
- **DC-OWN-2** Each topic below has one normative section and one owner per layer. A change edits the owner, not a copy.

| Topic | Normative rule | CSS owner | JS / state owner | Template owner | Test owner |
| --- | --- | --- | --- | --- | --- |
| Palette, shadows, focus tokens | 4.1, 5.5 | `gentelella-layout.css` | none | none | `tests/test_ui_accessibility.py` |
| Shell, viewport, scroll ownership | 5.1 to 5.3 | `gentelella-layout.css` | none | `base.html` | `tests/layout/audit.js`, `tests/test_ui_accessibility.py` |
| Sidebar and mobile drawer | 5.4 | `gentelella-layout.css` | `sidebar.js` | `base.html` | `tests/layout/sidebar-transition.mjs` |
| Focus | 5.5 | `gentelella-layout.css` (tokens), `ui.css` "Accessibility" (rule group) | none | none | `tests/layout/focus-style.spec.mjs`, `tests/test_ui_accessibility.py` |
| Cards | 3.3, 6.5 | `gentelella-layout.css` | none | page templates | `tests/layout/card-corners.spec.mjs` |
| Optional field groups | 3.3, 6.2 | `ui.css` | `utils/optional-groups.js` | page and modal templates | `tests/layout/recipient-channels.spec.mjs` |
| Tabs | 6.4 | `ui.css` "TABS" | `utils/tabs-overflow.js`, `utils/settings-tabs.js` | page templates | `tests/layout/tab-style.spec.mjs` |
| Modals | 6.3 | `ui.css` "Modal dialogs" | `app.js` (defaults), `utils/confirm-dialog.js` | `settings/modals.html` and page templates | `tests/layout/modal.spec.mjs`, `tests/test_modal_dismissal.py` |
| Field errors | 6.2 | `ui.css` (`.ju-field-error`) | `utils/field-errors.js` (`JuFieldErrors`) | form and modal templates | `tests/test_ui_accessibility.py` |
| Status dots and badges | 6.1 | `ui.css` | `utils/status-badge.js` | `fragments/_state_dot.html`, `fragments/target_status.html` | `tests/test_status_badge_no_icon.py`, `tests/test_status_dot_parity.py` |
| Warning message | 3.2 (DC-WARN), 6.1 | `ui.css` (`.ju-warning`) | `utils/warning.js` (`JuWarning`) | `fragments/_warning.html` (`ju_warning`) | open (DC-WARN-4 automated coverage not added or run) |
| Status palette (PDF, e-mail) | DC-TOK-7 | none | `app/utils/status_palette.py` | mail templates | `tests/test_status_palette.py`, `tests/test_email_template.py` |
| Charts | 6.7 | `target-detail.css`, palette tokens | `js/charts/*` | `target_detail.html` | `tests/js/*`, `tests/layout/combined-*.spec.mjs` |
| Dashboard widgets | 9 | `dashboard.css` | widget registry, `dashboard-grid.js` | `dashboard.html` | `tests/layout/harness.mjs` |
| Targets live update | 8.3 | none | `targets-live-update.js` | `targets.html`, `fragments/targets_table.html` | `tests/layout/targets-live-update.spec.mjs` |
| List search | 8.2 | `ui.css` (`.ui-search`) | `utils/list-filter.js` | list templates | `tests/js/list-filter.test.mjs` |

## 5 Shell, viewport, scrolling and navigation

### 5.1 Shell and viewport

- **DC-SHELL-1** Every page that extends `base.html` is viewport-bound. There is no page-level opt-in and no per-page height or scroll fix. Standalone authentication screens have their own layout.
- **DC-SHELL-2** `.ju-shell` is one dynamic viewport high (`100vh` fallback, then `100dvh`). The top bar and the application footer are non-shrinking shell rows; the footer stays in flex flow, ends at the visible bottom edge and never overlays content. `<main>` takes the remaining height and is the single vertical scroll region of page content (DC-SCROLL-1).
- **DC-SHELL-3** Content keeps its natural size, reflows at narrow widths (DC-MEAS-1) and stays reachable at the end of `<main>`. A feature area MAY keep a registered local scroll region (DC-SCROLL-2) but MUST NOT push the shell beyond the viewport.
- **DC-SHELL-4** Primary form actions of long editors stay reachable: at the end of the content in `<main>`, or in a persistent action bar (`position: sticky` inside `<main>`, for example `.sticky-actions`) that stays between the top bar and the footer. An action bar is never placed outside `<main>` or under the footer.

**Viewport invariant (DC-VP-1).** With the on-screen keyboard closed, in every supported engine: the document does not scroll vertically; the top bar and footer are fully inside the visible viewport; the footer ends at the visible bottom edge.

**Special cases.** `100dvh` alone does not guarantee DC-VP-1 under an on-screen keyboard or a collapsing browser toolbar. These cases are binding and justified:

| ID | Case | Rule |
| --- | --- | --- |
| DC-VP-2 | On-screen keyboard open | The focused control MUST remain visible by scrolling inside `<main>` or the modal body. The footer MAY be covered by the keyboard. |
| DC-VP-3 | Visual Viewport API | MUST NOT be used unless a failure is reproduced on a device and recorded with device, OS, browser version and the E-IOS result. A script then handles that case only and carries a comment naming the record. |
| DC-VP-4 | Browser toolbar collapsing | The shell MAY resize while the toolbar moves. No layout depends on a fixed pixel offset from the viewport edge. |
| DC-VP-5 | Safe areas | `viewport-fit` stays at its default (no `cover`). `env(safe-area-inset-*)` appears only as an additive term inside `max()` or `calc()`. If `viewport-fit=cover` is adopted, every fixed or sticky element (top bar, footer, drawer, action bar, toast, modal) MUST add the insets, verified at E-IOS. |
| DC-VP-6 | Standalone web app, iPad with pointer | Out of scope until verified at E-IOS. |

- **DC-VP-7** The behaviour of DC-VP-2 to DC-VP-5 on iOS is "unverified" until an E-IOS record exists (DC-MEAS-3).
- **DC-VP-8** Check: `tests/layout/audit.js` runs on every shell page at the top and bottom of `<main>`, including the target editor, at 1280x720, 390x844, 320x640 and effective 200 % zoom (640x360 CSS px at DPR 2) in Chromium and WebKit. It reports shell overflow, document scrolling, top bar or footer leaving the viewport, a footer ending above the visible bottom edge, an unscrollable main region, footer overlap and unreachable form actions (persistent bars at every position, scrolling actions at the scroll end). Its self-test proves the audit fails on a document-flow shell and passes on the shared shell. A failed check fails the UI run.

### 5.2 Scroll ownership

The document is level 0, `<main>` is level 1, registered regions are level 2.

- **DC-SCROLL-1** The document MUST NOT scroll vertically (DC-VP-1). `<main>` is the only vertical scroller of page content.
- **DC-SCROLL-2** A level-2 region MUST be listed in the registry below. The shell audit MUST inventory scroll regions and fail on an unregistered one (`tests/layout/audit.js`, check 3d, fails on an unregistered vertical region, a missing `overscroll-behavior`, a missing keyboard route and a nested region).
- **DC-SCROLL-3** Level-2 regions MUST NOT nest inside each other. Ordinary cards have no inner vertical scroll.
- **DC-SCROLL-4** A vertical level-2 region MUST set `overscroll-behavior: contain` so that wheel and touch at its end do not scroll `<main>`. A horizontal region sets `overscroll-behavior-x: contain`.
- **DC-SCROLL-5** The height of a region comes from the layout chain (`min-height: 0`) or a `max-height`, not from a fixed `height`. A region without focusable content MUST be keyboard-scrollable (`tabindex="0"`) and have an accessible name.
- **DC-SCROLL-6** While a modal dialog or the mobile drawer is open, `<main>` MUST NOT scroll by wheel, touch or keyboard; closing restores the previous scroll position. Bootstrap locks `body`, which is not the scroller here, so the observable behaviour is the norm and the mechanism is chosen and proven separately: `overscroll-behavior: contain` only stops scroll chaining and is not a lock for an independently scrollable `<main>`. A test records the `main` scroll position, attempts wheel, touch and keyboard scrolling over the overlay, closes it and asserts the position unchanged; a test that only asserts `body.modal-open` does not prove this. iOS Safari behaviour stays unverified until an E-IOS record exists (DC-VP-7).
- **DC-SCROLL-7** A dynamic update (live refresh, filter, pagination, async fragment) MUST NOT change the scroll position of a region the user is reading. Case (a), no structural change: DOM identity, focus and scroll anchor are kept (in-place patching is the default). Case (b), structural change: the visible scroll anchor is kept as far as possible and focus moves to the logically matching successor, else to a defined fallback. The transaction rules, including competing responses, are in 8.3.

| Region | Axis | Where it is allowed | Owner |
| --- | --- | --- | --- |
| Target wizard step | vertical | Active step of the wizard. At heights up to 520 px, `<main>` scrolls instead (landscape phone; registered alternative). | `target-wizard.css` |
| Downtime list `.downtime-scroll` | vertical | Target detail, downtime card. | `target-detail.css` |
| Traceroute hop table `.tr-table-scroll` | vertical | Height-bound traceroute panel. The map frame has no scroll area. | `traceroute-map.css` |
| Dashboard widget body | vertical | Only when the manual widget height is below the content height. | `dashboard.css` |
| Settings editor fill card body | vertical | Viewports at least 992 px wide and 600 px high. | `ui.css` |
| About page changelog card body (992 px and wider), `.dependencies-scroll` | vertical | About page. The changelog body is `role="region"`, `tabindex="0"`, named. | `about.html` |
| `.alert-recipients-picker` (max 260 px), `.ju-scroll-520` (Signal table) | vertical | Bounded lists inside a card. | `ui.css` |
| Tab strip, `.ju-table-viewport`, `.table-responsive`, `pre` | horizontal | Never makes the page scroll sideways. | `ui.css` |
| Traceroute hop track `[data-tr-track]`, map container `.leaflet-container` | horizontal | Traceroute panel; the map is a two-dimensional content exception. | `traceroute-map.css` |

**Overlays.** An overlay is not nested in `<main>`; it is a separate layer above it and has its own scroll behaviour. DC-SCROLL-3 (no nesting) and DC-SCROLL-4 apply inside an overlay, and DC-SCROLL-6 applies to `<main>` behind it.

| Overlay | Axis | Rule | Owner |
| --- | --- | --- | --- |
| Modal body | vertical | Scrolls inside the dialog; header and footer stay visible. | `ui.css` |
| Mobile drawer | vertical | Sidebar content on narrow viewports. | `gentelella-layout.css` |
| Notification popover | vertical | Floating panel. | `notifications.css` |
| Server-offline overlay `#server-offline-overlay` | vertical | Full-screen notice; scrolls only when the card exceeds the viewport. | `ui.css` |

### 5.3 Breakpoints

- **DC-BP-1** Viewport breakpoints in use: 992 px (sidebar rail to drawer) and 576 px (compact header); Bootstrap grid breakpoints (768, 1200, 1400 px) only where a Bootstrap grid or utility needs them; the combined queries 1200x800 (target detail fill mode) and 992x800 (SMTP editor fill in `ui.css`); 480 and 360 px for narrow-phone refinements already in `ui.css`; a height query of 620 px in `login.html` keeps the form reachable when the card is taller than the viewport; a height query of 800 px in `target-wizard.css` drops the breadcrumb and eyebrow on short viewports so the wizard step keeps its room; a height query of 520 px in `target-wizard.css` lets the wizard page scroll on very short viewports; a height query of 400 px (and the combined 400x400 query) in `ui.css` keeps the persistent action bar of the editors (`.sticky-actions--always`) sticky only while the form still has room above it, and narrows its primary button on phone-width viewports. A new viewport breakpoint is a reusable layout decision: it is added to this list with its owner file and reason in the same change; a one-off need uses a container query.
- **DC-BP-2** Feature layouts that depend on component width use container queries (`ju-page` thresholds 40rem and 60rem, `ju-table`, feature containers) and add no competing viewport breakpoint for the same content. The stacking container query of a widget MUST NOT trigger at its registry minimum width.

### 5.4 Navigation

- **DC-NAV-1** The navigation hierarchy and labels follow DC-PD-3. The sidebar has a desktop rail mode and a mobile drawer. The active item has text and a visual marker.
- **DC-NAV-2** On narrow screens the top bar keeps the menu toggle, brand, notifications, theme switch and logout reachable.
- **DC-NAV-3** The mobile drawer follows the disclosure pattern: Escape closes it, tabbing out of it closes it, leaving the mobile layout removes its state. It has no focus trap. While it is open, `<main>` does not scroll (DC-SCROLL-6).
- **DC-NAV-4** Page titles and actions sit in the content area. The wordmark follows `data-bs-theme`; the login and password-change screens use the same theme and tokens.
- **DC-NAV-5** Existing page families keep their established navigation, tabs and workflows.

### 5.5 Focus policy

One policy covers every focusable element. Component sections reference it and do not restate it.

- **DC-FOCUS-1** The focus indicator is neutral: colour `--ju-focus-ring` (the text colour), width `--ju-focus-outline-width` (2 px), offset `--ju-focus-outline-offset`. There is no blue or primary-coloured ring, no `box-shadow` ring and no primary fill. A component MUST NOT set its own focus colour or width; a new form component inherits the rule group in `ui.css` (section "Accessibility").
- **DC-FOCUS-2** The indicator is at least 3:1 against the control and its surface, in light and dark, and uses `:focus-visible`. A pointer click on a checkbox or switch draws nothing.
- **DC-FOCUS-3** `forced-colors: active` keeps a 2 px `Highlight` outline on every variant.
- **DC-FOCUS-4** A focus transition, if any, is disabled under `prefers-reduced-motion: reduce` (DC-TOK-6).
- **DC-FOCUS-5** A selected or active state may use its own fill but is not a focus indicator; a focused and selected element shows both.

| Variant | Applies to | Indicator |
| --- | --- | --- |
| V1 text-like field | `.form-control`, `.form-select`, `.signal-code-box` | The 1 px border changes to `--ju-focus-field-border`. No outline, no shadow, no size change. V1 is a separate form variant: `--ju-focus-field-border` MUST reach 3:1 against the unfocused border and the surface, checked in both themes (DC-FOCUS-2). |
| V2 outline | Buttons, links, tabs, switches, checkboxes, radios, `summary`, range thumbs | `outline` with the three tokens at the positive offset. |
| V3 inset | Elements whose outline would be clipped or overlapped by an ancestor: notification rows, stretched-link rows, focusable scroll regions | Same colour and width, offset `calc(-1 * var(--ju-focus-outline-width))`. |
| V4 composite | A wrapper around an inner input (tag input, type card) | V2 on the wrapper through `:focus-within`; the inner input draws nothing. |

- **DC-FOCUS-6** A feature picks the variant by control type, not by taste. V3 is allowed only where clipping or overlap makes V2 invisible. A need outside V1 to V4 requires an exception row (DC-EXC-1) and still uses `--ju-focus-*` tokens for colour.
- **DC-FOCUS-7** `outline: none` is allowed only on a programmatic focus target that is not an interactive control, or with an equivalent replacement.

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-FOCUS-1 | DC-FOCUS-7 | `main` and `#server-offline-overlay` | Focus is moved to them programmatically (skip link, offline notice) and they are not controls. | `ui.css` | `tests/test_ui_accessibility.py` (`test_outline_none_is_limited_to_the_documented_focus_cases`) |
| DC-EX-FOCUS-2 | DC-FOCUS-1 (colour) | `.ju-sidebar` | The sidebar is a dark surface in both themes; the neutral text colour would not reach 3:1 on it. The ring reads `--ju-sidebar-ring` (alias of `--ju-sidebar-fg`); width and offset stay the shared tokens (V3). | `gentelella-layout.css` | `tests/layout/sidebar-transition.spec.mjs` |

- **DC-FOCUS-8** Evidence: `tests/layout/focus-style.spec.mjs` (`@focus`) in Chromium and WebKit, light and dark, with keyboard navigation and forced colors (computed ring, contrast, switch geometry, cursors on the real pages); `tests/test_ui_accessibility.py` (tokens in both themes; no primary colour in focus rules). `test_focus_rules_use_the_shared_tokens_not_the_primary_colour` rejects any focus rule that reads a variable other than the focus tokens, a literal colour or a literal length.

## 6 Shared components and binding variants

Component contracts refine the global invariants of sections 4 and 5 (DC-PREC-1). Each component has one normative subsection; its owner table names the CSS, JavaScript, template and test owner. A change edits the owner, not a copy (DC-OWN-2).

### 6.1 State taxonomy, status dots and badges

A state kind is what a piece of UI expresses. This table is the only place that assigns a component to a state kind. Other sections and feature contracts reference it.

| Kind | Examples | Component | Never |
| --- | --- | --- | --- |
| S1 Monitoring state of a check, endpoint or certificate | UP, DOWN, UNKNOWN; certificate ok, warning, critical, expired, unknown | Status dot and state word | Badge, icon |
| S2 Overall target state | Online, Offline, Disabled, Unknown | Target state icon in lists, labelled pill in the detail header (exception DC-EX-STATE-1) | Dot, status badge |
| S3 Connection, integration, queue and quality state | Signal, SMTP, public egress, Delivery queue, Monitor Quality, spooler states, traceroute hop result; the status in a dashboard widget header | Status dot and state word | Badge (also in the widget header) |
| S4 Event severity | Notification rows | Status dot and severity word | Badge |
| S5 Result classification | A port-scan result (Open, Closed, Unclear) | Status badge with the result word (exception DC-EX-STATE-2) | Icon |
| S6 Loading or busy | A list that loads, a running test, `Checking...` | Spinner and word, or `aria-busy` on the region or control | Dot, badge |
| S7 Channel or type identity | Email, Signal, check type | Icon with accessible name | A status colour; the icon is not a state |
| S8 Count or metadata | Group counts, interval values, downtime count | Count badge | A state word, `ju-status-badge--*` |
| S9 Message severity | Warning, error and success text | Message marker by place (DC-STATE-7) | Dot, badge |

- **DC-STATE-1** A state kind MUST use exactly the component of its row on every surface: page markup, fragments, JavaScript-built markup, server-rendered HTML and toasts. A surface MUST NOT show the same kind with another component. Server-rendered and JavaScript-built markup repeats the classes of the `status_dot` macro; it does not invent a variant. Hand-written markup is allowed only where a JavaScript hook forces it (the dashboard queue indicator with `id`, `role` and `aria-live`, the `data-egress-*` rows of `settings/network.html`).
- **DC-STATE-2** A status dot is `.ju-status-dot` inside `.ju-status-item`. The dot is `aria-hidden`. The state word is visible, or `visually-hidden` after a label (`HTTP: Down`); an optional `title` repeats it. The shape repeats the tone: filled circle success, square danger, diamond warning, hollow ring neutral. In `forced-colors: active` the dot uses `CanvasText`.
- **DC-STATE-3** Dot tones are success, warning, danger and neutral. `info` is not a dot tone; information is neutral. The dot colour comes from the Bootstrap semantic tokens; a page MUST NOT set a dot colour.
- **DC-STATE-4** The hollow ring means "no confirmed state" for S1 and S3 (UNKNOWN) and "information, no severity" for S4. It never signals DOWN. One surface never mixes the two meanings.
- **DC-STATE-5** The Monitor Quality score takes the state text colour of the palette (`quality-score--success|warning|danger`; UNKNOWN stays neutral) next to its dot and state word. `QualityState.describe` produces it.
- **DC-STATE-6** A new state kind, or a kind shown in a new place with another component, needs a row in the table or an exception row (DC-EXC-1). Product exceptions:

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-STATE-1 | S2: dot | Target state icon in list columns and in the dashboard status heroes (DC-DASH-2); `.target-status-pill` (icon and word) in the target detail header | The overall target state is one glyph in dense lists. The glyph is the shape cue and carries an accessible name; the header pill adds the word. Both come from one fragment. | `fragments/target_status.html`, `target-detail.css` (`.target-status-pill`) | `tests/test_unknown_status_display.py` |
| DC-EX-STATE-2 | S5: badge allowlist | Result badges of the network scan picker (`network-scan-picker.js`) | The word is the Nmap result. An unclear result adds a dashed edge. | `JuStatusBadge` | `tests/test_status_badge_no_icon.py`, `tests/layout/port-picker.spec.mjs` |

- **DC-STATE-7** Message severity (S9) uses these markers: a warning message starts with the warning sign (DC-WARN-1, class `ju-warning`); an error message starts with the `error` icon (`.ju-field-error`, `.ju-modal-error`, form alerts); a success message in a result region starts with the `check_circle` icon; an information message has no marker. The text carries the meaning; the marker is `aria-hidden`. Where each message appears is decided in 6.2.
- **DC-STATE-8** The unread marker of Notifications is an item marker, not a state: it MUST NOT use `.ju-status-dot` or `.ju-status-item`. Its rule belongs to the Notification list contract (section 9).

Implementation owners:

| Component | CSS | JS | Template | Test |
| --- | --- | --- | --- | --- |
| Status dot (S1, S3, S4) | `ui.css` (`.ju-status-item`, `.ju-status-dot`) | `utils/status-badge.js` (`JuStatusDot`) | `fragments/_state_dot.html` (`state_dot`, `cert_dot`, `status_dot`) | `tests/test_status_dot_parity.py`, `tests/test_endpoint_view.py`, `tests/layout/endpoint-state.spec.mjs` |
| Target state (S2) | `target-detail.css` | none | `fragments/target_status.html` | `tests/test_unknown_status_display.py` |
| Notification severity (S4) | `notifications.css`, `ui.css` | `notifications.js` | `notifications.html` | `tests/layout/notifications.mjs` |
| Status badge (S5) | `ui.css` (`.ju-status-badge--*`) | `utils/status-badge.js` (`JuStatusBadge`) | none | `tests/test_status_badge_no_icon.py` |
| Count badge (S8) | `ui.css` | none | page templates | `tests/layout/audit.js` |
| Warning message (S9) | `ui.css` (`.ju-warning`) | `utils/warning.js` (`JuWarning`) | `fragments/_warning.html` (`ju_warning`) | open (DC-WARN-4 automated coverage not added or run) |

The deviations of the earlier audit are implemented: the Alerts panel shows a failed service as a DOWN dot and the others as count badges, the Signal fragments render `status_dot` markup (`_html_status_dot` in `api/frontend.py`, no `icon` parameter), the `info` dot tone is removed, and a loading state (`Loading`, `Checking…`) is the word without a dot (S6).
#### Badges

- **DC-BADGE-1** Every badge is Bootstrap `.badge`, refined in `ui.css`: inline-flex, label centred in its box, size and radius from the badge tokens.
- **DC-BADGE-2** A status badge composes `.badge` with exactly one of `ju-status-badge--success`, `--warning`, `--danger`, `--info`, `--neutral`. These classes use the matching Bootstrap semantic background token with a legible white or black foreground. A status badge MUST NOT use `bg-*` or `text-bg-*` utilities.
- **DC-BADGE-3** A status badge MUST NOT contain an icon. The dashed edge is the modifier `ju-state-badge--unknown`, which `JuStatusBadge` adds to any variant; it is not a second variant system. The variant colour and the always present word carry the state; an UNKNOWN-like result adds a dashed edge. The rule holds for the web, PDF and e-mail adapters. `JuStatusBadge` (`classes`, `html`) is the only producer and takes no icon parameter. Channel and type icons, icon-only buttons, copy buttons and spinners are not status badges.
- **DC-BADGE-4** Status badges exist only where the exception table lists them (DC-EX-STATE-2). `tests/test_status_badge_no_icon.py` MUST scan templates, JavaScript and Python and fail on a status badge outside that list.
- **DC-BADGE-5** A count badge is `.badge` with `badge-muted`, `badge-interval`, `bg-secondary`, `bg-danger` or `tr-badge`. Bootstrap `bg-*` utilities are allowed on it. The number or label is its content; it carries no state word and no `ju-status-badge--*` class.
- **DC-BADGE-6** Two or more badges or state dots in a row or cluster sit in a group: the container carries `ju-badges` (or `data-ju-badges`) and the children are `.badge` or `data-ju-badge`, or `.ju-status-item`. The class name is historical and also groups dots. The group owns wrapping, the gap (`--ju-badges-gap`, default `--ui-stat-gap`) and the vertical alignment. `ju-badges--fill` takes the space left in the parent and centres the rows in it. Widgets, tables and headers MUST NOT add their own centring, `top`, `transform` or pixel offsets for badges.
- **DC-BADGE-7** `tests/layout/audit.js` measures every badge group on every audited page, viewport and widget size (`.ju-badges, [data-ju-badges]`; new groups are covered automatically) and reports `badge-group-not-centered`, `badge-text-not-centered`, `badge-overlap`, `badge-clipped-by-group` and `badge-text-clipped`. Its self-test injects each defect. `tests/layout/run.mjs` measures the quality widget at its default height, with three rows, manually enlarged and narrow.

#### Status palette

The cross-channel status palette (web dots and badges, PDF, e-mail) is fixed: success `#198754` / white foreground / `#146c43` text; warning `#ffc107` / black foreground / `#8a5a00` text; danger `#dc3545` / white foreground / `#b02a37` text; info `#0dcaf0` / black foreground / `#055160` text; neutral `#6c757d` / white foreground / `#5b6b7b` text. The web badge variants use the Bootstrap semantic background tokens for these values. PDF status badges and email notification accents/fills use the same background and foreground mapping; text on light email/PDF surfaces uses the mapped text color for contrast. Email status colors come from `app/utils/status_palette.py`; `status.color` remains a text-color alias for saved custom templates, while new template code uses `status.accent_color`, `status.foreground_color`, and `status.text_color` explicitly. Metadata (check type, group, host, port) in emails and PDF is always neutral: check-type badges use the `neutral` palette entry (email: `CHECK_BADGE_COLORS` in `email_template.py`, PDF: `COLORS["neutral"]`) in every mail state, never a status color or a per-type color; status colors appear only for the status of the message (banner, status text, status badge). Pinned by `tests/test_email_template.py` (neutral in every state, banner keeps its state color, contrast ≥ 4.5:1 text / ≥ 3:1 edge).

### 6.2 Forms and field errors

Applies to dialogs, the target wizard, the target editor, the port picker and Settings pages.

#### Fields

- **DC-FORM-1** Every field MUST have a visible `<label for>`. A field whose purpose is given by a neighbouring switch label uses a `visually-hidden` label with the same words. A placeholder never replaces a label.
- **DC-FORM-2** A required field carries the native `required` attribute. When a form also has optional fields, required labels end in `<span class="ju-required" aria-hidden="true">*</span>` and the form starts with the `ju-modal-note` "Fields marked * are required."; a single-field confirmation form marks the field by `required` only.
- **DC-FORM-3** Help text sits directly below its field in `.form-text` and is tied to the control with `aria-describedby`.
- **DC-FORM-4** A field uses the `type`, `inputmode` and `autocomplete` that match its data (`tel`, `email`, `current-password`, `new-password`). A password confirmation form keeps the hidden username field for password managers, hidden with `visually-hidden`. Groups of related controls (notification channels, change password) use `fieldset` with `legend`, not a loose heading and not `role="group"`.
- **DC-FORM-5** A switch is a real checkbox with `role="switch"` and a `<label>` inside `.ju-switch`; the label is the large target. Track and label have a gap of at least `--ui-control-gap` (0.5 rem) on all pointers. A switch has help text only when it is not self-explanatory; "Recipient Enabled" and "Enable for all Targets" have none (product decision). A switch with a destructive side effect needs a safeguard through the shared confirmation (DC-MODAL-14); the recipient case is specified in section 9.

#### Field errors

- **DC-FORM-6** One pattern covers every field-level problem in dialogs, wizard, port picker, editor and normal forms, and `JuFieldErrors` (`utils/field-errors.js`) is its only producer, for a single field and for a control group (`setGroup`). A page or component MUST NOT create, show or clear a field error by other means, and MUST NOT add a helper next to it.
- **DC-FORM-7** `JuFieldErrors` renders the error as `<id>-field-error` directly after the control (after `.ju-switch` for a switch): the `error` icon and the message text. The control gets `aria-invalid="true"` and `.is-invalid`, and the error id is added to `aria-describedby` next to the help ids. The error element has no `role` and no `aria-live`. A group of controls that together form one value (the Signal verification code boxes) is a `<fieldset data-ju-field-group>` with a `<legend>`; its error is one persistent `<id>-field-error` directly after the fieldset, linked by `aria-describedby` from the fieldset and every control, and the controls get `aria-invalid`. A timed marker (`is-invalid` removed by a timeout) is not an error display. Native constraint validation (`required`, `type`, `pattern`, range) stays the first line; its custom message is rendered through the same pattern.
- **DC-FORM-8** After a failed submit, step change or commit action, focus moves to the first invalid field (`JuFieldErrors.show`); for a control group (DC-FORM-7 group form) it moves to the first empty control of the group. A validation that runs while the user types or leaves a field does not move focus.
- **DC-FORM-9** An error clears when the field (for a group: any control of it) is edited, when its optional group is hidden, and when the dialog opens or closes. Clearing removes `aria-invalid` and the id from `aria-describedby`.
- **DC-FORM-10** When an error arises while focus already sits on the control (Enter in the port picker, validation on blur), the same message is also written to the polite status region (`role="status"`) of the form or component, so the announcement does not depend on a per-field live region. A group error after a failed commit is announced by the focus move onto the first control, whose `aria-describedby` carries the message. A form without such a region adds one.
- **DC-FORM-11** `role="alert"` and `aria-live="assertive"` MUST NOT be set on a field error element. They are reserved for the request-level and form-level failures of DC-FORM-12. Checks: the markup tests assert no `role` on `-field-error`, `.invalid-feedback` and the port picker's manual error.

#### Where a message appears

- **DC-FORM-12** Each kind of problem has one place:

| Kind | Where | Semantics |
| --- | --- | --- |
| Field-level problem (empty, invalid value) | Directly below the field (`.ju-field-error`) | DC-FORM-7 to DC-FORM-11. |
| Request-level or asynchronous failure (API, network) | The form or dialog error region: `ju-modal-error` at the end of a dialog body, `#wizard-error` in the wizard, the form alert on a page | `role="alert"`, shown and hidden with `d-none`, never takes focus, input is preserved. A failure that names one field MAY also use `JuFieldErrors`. |
| Result of a test action | `ju-test-result` next to the action | `role="status"`, `aria-live="polite"`, one reserved line, icon and text, no focus. |
| Result of an action outside a dialog | Toast | The existing toast live region. |
| Progress and results of the port picker | One polite status line; one alert line for scan failures | Never both at once. |
| Validation failure of a Settings autosave field | The field error and the toast | The toast announces, the field error persists until the field is edited (open: `settings.storage.js`, scheduled after the Settings task that edits it). |

- **DC-FORM-13** The target wizard (`#wizard-address`, the port step, `#wizard-manual-ports`) and the port picker (`<prefix>-manual-ports`) follow DC-FORM-6 to DC-FORM-11. Invalid port tokens are named in the message; nothing is added and the text stays. No exception row exists.
- **DC-FORM-14** Feedback that is neither a field error nor a failure (a hint, a count, a conflict list) uses a polite region and never `role="alert"`. Loading, disabled, warning and success states are distinguishable without colour. Help, error and result text reserves its space so that it causes no layout jump.

#### Optional field groups

- **DC-FORM-15** A body that an enable switch hides (DC-CARD-3) is implemented once, in `utils/optional-groups.js` (`JuOptionalGroups`). An off group carries the `hidden` attribute, so its input, help text, field error and test action leave layout, tab order and accessibility tree; the input MAY additionally be `disabled`, never only that. The owning switch has `role="switch"` and `aria-controls` naming the group; no `aria-expanded`, no extra live message. Focus stays on the switch when a group appears or disappears; if focus sat inside a group that is hidden, it moves to the switch.
- **DC-FORM-16** A hidden group is not validated, its field errors are removed when it is hidden, and typed values stay while the dialog is open. "At least one channel" stays mandatory and is reported at the channel switches. Controls shared by all groups (saved-values hint, test result) show only while one group is visible. Showing or hiding a group changes only the height of its container; width, header and footer do not move.
- **DC-FORM-17** Consumers: the recipient dialogs and the wizard check groups (`utils/check-groups.js`) use `JuOptionalGroups`. The check cards of the target editor and the Settings > Signal panel use the same mechanism (`hidden` plus `aria-controls` on the switch).
- Checks: `tests/js/optional-groups.test.mjs` (switch combinations, error removal, focus), `tests/test_ui_accessibility.py` (`aria-controls` markup), `tests/layout/recipient-channels.spec.mjs` (`@modal`).

#### Channel option row

- **DC-FORM-18** Signal and Email use one row, `.ju-channel`, inside the channel `fieldset`: the `ju-switch` (icon and name), then `.ju-channel-body` with the value input, its help text and, in the edit dialog only, one `.ju-channel-action` with the single test trigger. Rows are flat and separated by a rule, never nested cards. A switched-off channel shows only its switch row.
- **DC-FORM-19** There is one test pattern: a secondary `btn-sm btn-outline-secondary` button below the field, visible text `Send test`, accessible name `Send test message to <channel>`, `aria-describedby` on the shared saved-values hint. There is no icon-only test button, no input-group adornment and no "test all" button. The hint states that tests use saved values and updates when edits are unsaved; the result goes to `.ju-test-result`. The add dialog has no test trigger.
- **DC-FORM-20** The Add and Edit Recipient dialogs keep recipient-wide options in a second native `fieldset` with the legend `Recipient Settings` (`Recipient Enabled`, `Enable for all Targets`) after the `Notification Channels` fieldset, with the shared modal border and spacing tokens and no card or per-dialog styling.

Owners:

| Part | CSS | JS | Template | Test |
| --- | --- | --- | --- | --- |
| Field error | `ui.css` (`.ju-field-error`) | `utils/field-errors.js` | form, modal and wizard templates, `fragments/network_scan_picker.html` | `tests/test_ui_accessibility.py`, `tests/test_port_picker_markup.py`, `tests/layout/wizard.spec.mjs`, `tests/layout/modal.spec.mjs`, `tests/layout/smtp-link-field.spec.mjs` |
| Optional field group | `ui.css` | `utils/optional-groups.js`, `utils/check-groups.js` | `settings/modals.html`, `target_wizard.html` | see checks above |
| Channel row | `ui.css` (`.ju-channel`) | `settings.recipients.js` | `settings/modals.html` | `tests/layout/recipient-channels.spec.mjs` |

### 6.3 Modal dialogs

#### Scope and structure

- **DC-MODAL-1** All application modals follow one contract: the Settings dialogs (`settings/modals.html`), the group dialog, the dashboard widget dialog, the shared confirmation dialog (`window.confirmAction`, `utils/confirm-dialog.js`) and every future dialog. Content may differ; presentation rules per dialog MUST NOT. Bootstrap 5.3 supplies behaviour and accessibility primitives; `ui.css` (section "Modal dialogs") owns presentation and reads only shared tokens.
- **DC-MODAL-2** Structure: `.modal > .modal-dialog > .modal-content` with `.modal-header`, `.modal-body` and, for an actionable dialog, `.modal-footer`. A dialog has a stable ID, a visible title (`h2.ju-modal-title`) referenced by `aria-labelledby`, a native close button where dismissal is allowed, and native forms and controls. Shared classes and Bootstrap primitives are preferred over modal-ID selectors, inline styles and repeated overrides.
- **DC-MODAL-3** No generic JavaScript modal framework is added. The existing modules (`app.js` defaults, `utils/modal-focus.js`, `utils/confirm-dialog.js`, `utils/htmx-confirm.js`) are the extension points; a new one needs a proven need recorded in the change.

#### Intent and size

Intent and size are two independent axes. Intent is semantic; size is layout.

- **DC-MODAL-4** Every `.modal` root MUST carry exactly one intent class:

| Intent | Class | Use | Action treatment |
| --- | --- | --- | --- |
| Form | `ju-modal--form` | Add or edit a recipient, user or configuration; selection and information dialogs | Neutral header; Cancel (secondary) and one clearly labelled primary save or create action. |
| Confirmation | `ju-modal--confirm` | A reversible, routine discrete action | Neutral header, concise explanation, one labelled confirm action. |
| Critical | `ju-modal--critical` | Delete, purge, unregister, restore, security-sensitive change | Neutral header; the consequence is stated in the body with `.ju-modal-consequence` (icon and text). The confirm button is `btn-danger` when data or access is removed irreversibly and `btn-warning` for a reversible but impactful security change. Required authentication stays. |

  A dialog whose changes apply immediately, or that only informs (the dashboard widget list), keeps `ju-modal--form`, has a footer with a single `Close` action and never gets an artificial Save button; its header close button and `Close` both dismiss it. `window.confirmAction` selects the intent by argument; today every confirmation is critical.
- **DC-MODAL-5** A `.modal` root MAY carry at most one size class: `ju-modal-size--compact`, `ju-modal-size--standard` or `ju-modal-size--wide`. Without a size class the size follows the intent: standard for form, compact for confirmation and critical. Wide is a size, never an intent. Widths come from the tokens `--ui-modal-width-compact` (28 rem), `--ui-modal-width-standard` (32 rem) and `--ui-modal-width-wide` (40 rem); only these tokens set a dialog width. Height follows content up to the available viewport and adds no empty space to match another dialog.
- **DC-MODAL-6** Header, body and footer padding, title typography, border, radius and shadow are identical for every intent and size. A full `bg-danger` or `bg-warning` header MUST NOT distinguish intent; the body explains the consequence and the button expresses the severity. An optional title icon uses the shared icon size, and the title stays the strongest header text.
- **DC-MODAL-7** A dialog MUST NOT use inline `style`, hard-coded colours, `!important`, modal-ID selectors, `bg-*` or `text-white` header classes, or `card` markup in the body. Colours come from `--bs-*` theme tokens or `--ju-*` tokens; light and dark follow `data-bs-theme` without per-dialog branches. Obsolete modal-specific CSS is removed when a dialog adopts the shared classes.

#### Layout, actions and dismissal

- **DC-MODAL-8** Vertical rhythm: label above control, help or error text immediately below, consistent spacing, one padding contract. `form-control-sm` is not mixed with regular controls in one form unless compact density is a documented dialog-wide variant. The primary task dominates; a secondary action (channel test) is recognisable as secondary and has a text label.
- **DC-MODAL-9** The footer holds Cancel or Close and one primary action, aligned consistently on desktop and stacked consistently on narrow screens. Labels name the action (no `OK`, no unlabeled icon). Async submission shows a busy state, prevents duplicate commits, preserves input on failure and reports the result per DC-FORM-12. Cancel, close, Escape and backdrop handling never submit the form. Confirmation and password guards survive presentation changes.
- **DC-MODAL-10** Dismissal policy: a click or tap outside the dialog never dismisses it (`Modal.Default.backdrop = 'static'` in `app.js`, repeated by `confirm-dialog.js`). Escape closes a dialog that has a close or cancel control (`Modal.Default.keyboard = true`); a dialog that must not close on Escape sets `data-bs-keyboard="false"` and states why. Closing discards the draft.
- **DC-MODAL-11** Field and form errors follow DC-FORM-12: field problems below the field, request failures in `ju-modal-error` (`role="alert"`, no focus), test results in `ju-test-result`.

#### Focus, semantics and scrolling

- **DC-MODAL-12** Bootstrap supplies `role="dialog"` and `aria-modal="true"`; a dialog MUST NOT override them. The page behind an open dialog MUST NOT be operable (no focus, click or keyboard reach); a test proves it, Bootstrap is not assumed. Every dialog has `aria-labelledby` and, where a message explains the action, `aria-describedby`. The close button has `aria-label`; decorative icons are `aria-hidden`; every icon-only button has an accessible name. Bootstrap's focus trap and Escape handling keep working. Focus indicators (DC-FOCUS), touch targets (DC-RESP-6), reduced motion and forced colours (7.3) apply to every intent and size.
- **DC-MODAL-13** `utils/modal-focus.js` is the only focus lifecycle, including dialogs created at runtime. The `autofocus` attribute MUST NOT be used. After `shown.bs.modal`, focus moves to the first match of: a control with `data-ju-initial-focus`; in a critical dialog the first required field, else the safe Cancel or Close; in other dialogs the first enabled, visible, editable field, else the first focusable body control, else Cancel or Close. After `hidden.bs.modal`, focus returns to the invoking element while it is in the DOM and visible, otherwise to `main`.
- **DC-MODAL-14** At most one Bootstrap dialog is visible at any time; dialogs are never stacked. A dialog that needs `window.confirmAction` is hidden first (draft and switch states stay in the DOM), the confirmation is shown alone, and the original dialog is shown again after either answer with focus on the control that triggered the confirmation. Controls stay enabled during the confirmation; the busy state starts afterwards. Exactly one confirmation is active; a concurrent call resolves `false` without touching it. Backdrop and scroll lock stay single and nothing is left behind after the last dialog closes.
- **DC-MODAL-15** A button that needs a confirmation never uses `hx-confirm`. It carries `data-ju-confirm`, `data-confirm-title`, `data-confirm-message` (names the target and the consequence) and `data-confirm-label`; `utils/htmx-confirm.js` shows `confirmAction` on `htmx:confirm` and sends the unchanged request once after the answer. Checks: `tests/layout/reset-confirm.spec.mjs`, `tests/test_reset_confirm_markup.py`.
- **DC-MODAL-16** While a dialog is open `<main>` does not scroll and closing restores its position (DC-SCROLL-6). The dialog body is a registered scroll region: it scrolls inside the dialog while header and footer stay visible.

#### Responsive behaviour and themes

- **DC-MODAL-17** Modal layout follows its own available width and height; the shell breakpoints do not change. At 320 CSS px, at effective 200 % zoom and with text enlargement (section 7), titles, fields, buttons, help text and close control stay usable without page-level horizontal scrolling. The footer MAY stack its actions; an input action MUST NOT squeeze a field below a usable width. A fixed-height dialog MUST NOT make the footer or close control unreachable. With the on-screen keyboard open the focused control stays visible (DC-VP-2); safe areas follow DC-VP-5.
- **DC-MODAL-18** Light, dark and system modes use the same structure and tokens; a dialog hardcodes no second background, border, warning or primary-action palette.

#### Checks

- Static: `tests/test_ui_accessibility.py` (intent, size, shared shell, no inline style or nested cards, label and ARIA references, fieldset and legend, channel and test pattern, token-only CSS) and `tests/test_modal_dismissal.py`.
- Browser: `tests/layout/modal.spec.mjs` (Chromium; 320 px and 1280 px; light and dark) asserts role, `aria-modal`, resolving `aria-labelledby`, initial focus, focus return, containment, Escape, target size, overflow, control overlap, body scrolling and footer reachability at 320 px, and field-error semantics. `tests/layout/run.mjs` (`modalAudit`) audits the application pages. Its scroll-lock case MUST record `main.scrollTop`, attempt scroll gestures and assert it unchanged, because `body.modal-open` alone does not prove DC-SCROLL-6 (open).
- Each representative intent (form, confirmation, critical) is checked at desktop width, at 320 to 576 px, on short viewports, with text enlargement and in light and dark: accessible names, focus containment and return, the dismissal policy, scrolling and keyboard submission. Browser checks assert geometry: no control or text overlap, horizontal overflow, clipped footer, obscured close button or unreachable validation result.
- A rule added here is added to a check in the same change. Existing dialog IDs, form field IDs, Bootstrap hooks, confirmation flows and backend contracts are preserved; shared presentation is refactored before an individual dialog; an intentional behaviour change is documented and tested.
- Where existing modal markup or CSS contradicts this subsection, this subsection wins; a local CSS patch is not a substitute (DC-PREC-2).

Owners:

| Part | CSS | JS | Template | Test |
| --- | --- | --- | --- | --- |
| Intent, size, shell | `ui.css` "Modal dialogs", `--ui-modal-*` tokens | none | `settings/modals.html`, `groups.html`, `dashboard.html` | `tests/test_ui_accessibility.py`, `tests/layout/modal.spec.mjs` |
| Defaults, dismissal | none | `app.js` | none | `tests/test_modal_dismissal.py` |
| Focus lifecycle | none | `utils/modal-focus.js` | none | `tests/layout/modal.spec.mjs` |
| Confirmation | `ui.css` (`.ju-modal-consequence`) | `utils/confirm-dialog.js`, `utils/htmx-confirm.js` | page templates (`data-ju-confirm`) | `tests/layout/reset-confirm.spec.mjs` |

### 6.4 Tabs

One visual component serves two behaviours. Every tab strip (Notifications categories, Edit Target, Target detail, Settings, every future one) is `<ul class="nav nav-pills ju-tabs">`, defined once in `ui.css` section "TABS". The Notifications strip is the reference look. Markup MUST NOT use `nav-tabs`.

| Behaviour | Markup | Used by |
| --- | --- | --- |
| Real tabs | Bootstrap tab plugin: `role="tablist"`, `role="tab"`, `aria-selected`, `aria-controls`, `tab-content` panels | Edit Target, Target detail, Settings |
| Link strip | `<a class="nav-link">` inside `<nav aria-label>`, `aria-current="page"`, no tab roles | Notifications categories |

#### Shared visual rules

- **DC-TAB-1** Tokens are `--ui-tab-gap`, `--ui-tab-min-height` (2.75 rem, a 44 px target at every width) and `--ui-tab-pad-x` in the `:root` block of `ui.css`. Colours are semantic tokens only: the inactive label uses the link colour (`--bs-primary` family), the active tab is filled with `--bs-primary` and labelled with `--ju-primary-contrast`, hover uses `--ui-muted-bg`. No hex value, no `!important`.
- **DC-TAB-2** States: default (link colour, transparent 1 px border), hover, active (the primary fill is the shape cue next to the colour; in `forced-colors: active` the active tab also gets a `Highlight` border and an underline), focus (focus policy V2, DC-FOCUS-1), disabled (per behaviour below). Light and dark follow `data-bs-theme` through the tokens.
- **DC-TAB-3** Overflow: one row (`flex-wrap: nowrap`), scrolled horizontally inside the strip (`overflow: auto hidden`, `overscroll-behavior-x: contain`), never scrolling the page sideways. The strip is a registered horizontal region (DC-SCROLL-2). The scrollbar is the shared thin accent scrollbar (`scrollbar-width: thin`, `scrollbar-color: var(--bs-primary) transparent`; `::-webkit-scrollbar` only for engines without `scrollbar-color`), declared once for `.ju-tabs` and `.ju-scrollbar-accent`; it is not hidden, faded or recoloured per page.
- **DC-TAB-4** Hidden tabs get a visual cue (there is no separate announcement; the tablist or link list is fully present in the accessibility tree and keyboard focus scrolls to each tab): `utils/tabs-overflow.js` (loaded by `base.html`, bound to every `.ju-tabs`) sets `has-overflow`, `is-at-start` and `is-at-end`, and the shared rule fades the scrollable edge (`--ui-tab-fade-start`, `--ui-tab-fade-end`). The active tab scrolls into view on load, after `shown.bs.tab` and on keyboard focus (not on a mouse press), smoothly only without `prefers-reduced-motion`. There is no "More" button or menu.
- **DC-TAB-5** Forbidden: a page-specific tab colour, size, border, hover, fill or scrollbar; a rule that targets `.nav-link`, `.nav-pills`, `.nav-tabs`, `.ju-tabs` or `[role="tab"]` outside `ui.css` and `gentelella-layout.css` (the palette owner), in an inline `<style>` or by element id; a second tab class that copies these rules; new `!important` for tabs. A different look is a named variant added here, not a page override.

#### Real tabs

- **DC-TAB-6** Real tabs use roving tabindex: only the active tab is in the Tab sequence (inactive tabs have `tabindex="-1"`); Left, Right, Home and End move focus and activate the tab (automatic activation, Bootstrap 5.3 behaviour). Tab from the tablist reaches the panel content. A panel with no focusable content SHOULD be `tabindex="0"`.
- **DC-TAB-7** A disabled real tab is a native `<button disabled>`: it is out of the Tab sequence, cannot activate and is skipped by the arrow keys. `aria-disabled` is not used on tabs. No tab is disabled today; the first consumer adds a test.

#### Link strip

- **DC-TAB-8** The `<ul>` sits in a `<nav aria-label="…">`; the current page link has `aria-current="page"`. Every link is in the Tab sequence and arrow keys are not remapped.
- **DC-TAB-9** A disabled link has no `href`, `aria-disabled="true"`, `tabindex="-1"` and `.disabled`. No link is disabled today; the first consumer adds a test.

Checks: `tests/test_ui_accessibility.py` (every strip is `ju-tabs`, no tab selector outside the owners or in inline CSS, tokens only), `tests/layout/tab-style.spec.mjs` (`@tab-style`: computed tab and scrollbar colours identical on Notifications, Edit Target, Target detail and Settings), `tests/layout/settings-tab-press.spec.mjs`, `tests/layout/edit-target-tabs.spec.mjs`, `tests/js/settings-tabs.test.mjs`. Arrow-key, Home/End, roving-tabindex and native-disabled cases for real tabs and the Tab order of the link strip are covered by the two `@tab-style` tests in `tests/layout/tab-style.spec.mjs`.

Owners: CSS `ui.css` "TABS" (palette in `gentelella-layout.css`); JS `utils/tabs-overflow.js`, `utils/settings-tabs.js`; templates `settings.html`, `target_detail.html`, `target_form.html`, `notifications.html`.

### 6.5 Cards: layout invariants

Terms and the collapse decision are in 3.3 (DC-CARD-1 to DC-CARD-6: no collapse handle, disclosures, optional field groups, equal height, corners, overflow). These invariants describe results that hold in every state of the view; measurements belong to the owning test.

- **DC-CARD-7 Frame.** Cards and tiles of one kind in a view share border, radius, surface and elevation tokens (`--bs-card-*`, DC-TOK-5). A page MUST NOT restyle them.
- **DC-CARD-8 Header.** The header shows the title, an optional status (kind S3, a dot) and actions in one block. At every CSS viewport of 320 px and wider they do not overlap. A long title wraps or truncates with the full text available; it never pushes the actions out of the card. A status appears once in the header.
- **DC-CARD-9 Content.** The body fills the card and shows its content, its empty state or its error state; no empty frame remains around nothing. Content is not clipped (DC-CARD-5, DC-CARD-6). Ordinary cards have no inner vertical scroll (DC-SCROLL-3); long text wraps, or truncates with the full value in `title`.

Owners: CSS `gentelella-layout.css` (`--bs-card-*`), `ui.css`, `dashboard.css` (widget cards); JS none; templates page templates; tests `tests/layout/card-corners.spec.mjs`, `tests/layout/card-overlays.spec.mjs`, `tests/test_ui_accessibility.py` (`test_card_headers_have_no_collapse_toggle`), `tests/layout/public-egress.spec.mjs`.

### 6.6 Bounded content in summaries, lists and cards

- **DC-LIST-1** An overview card shows a bounded set of items. Critical and unknown states stay visible in the summary even when individual items are hidden. A `+N` control counts only hidden items, reveals the complete set by keyboard and touch, and keeps focus and its expanded state across live updates (DC-LIVE-3). Ordinary cards use the page scroll (DC-SCROLL-3). Detail and editing views MAY show the full set. Text truncation addresses a long label, not an unbounded number of items. The presentation never changes check data or status definitions.

### 6.7 Charts and data visualisation

Applies to the combined response-time chart of the target detail page and every future metric chart (`combined-chart.js`, `target-detail.css`; tokens in `gentelella-layout.css`).

#### Clipping and reset

- **DC-CHART-1** Series are clipped at the chart area. They are never drawn over the axes, their labels or the card edge, at any zoom or after a reset; a step segment that starts or ends outside the visible range ends at its edge. Chart.js semantics: a dataset `clip` of `0` clips exactly at the chart area, `false` disables clipping, a positive number lets the drawing extend by that many pixels, a negative number clips inside the area. Every line, step and peak dataset MUST set `clip: 0`; the chart-level default MAY stay `false` because the dataset option overrides it. The annotation plugin takes `clip: true`.
- **DC-CHART-2** Double-click and the `Reset zoom` button call one function (`charts/chart-reset.js`, `resetChartView`). It restores the load state completely: time axis, response-time and status scales (limits captured at build; automatic axes stay automatic), series, points, down bands and peaks, and it cancels a detail request in flight. The time axis spans the selected range even when measurements cover only part of it. Reset is idempotent.
- Checks: `tests/js/availability-chart-clip.test.mjs`, `tests/layout/availability-clip.spec.mjs`, `tests/layout/combined-clip.spec.mjs` (also asserts `clip === 0` on every line, step and peak dataset of both charts), `tests/js/chart-reset.test.mjs`, `tests/layout/combined-zoom-reset.spec.mjs`.

#### Colour, identity and non-colour differentiator

- **DC-CHART-3** A chart with one plotted response-time metric uses `--ju-st-online` in both themes. With several, series take categorical colours from `--ju-dv-1` to `--ju-dv-8`, read at runtime from the active theme; chart scripts hold no hex values for series. Each colour has at least 3:1 contrast on `--ju-panel-bg` and the colours are pairwise distinguishable.
- **DC-CHART-4** A series keeps its colour for the plotted data: HTTP and Ping have fixed slots, TCP ports follow in ascending port order. Hiding other series in the legend does not recolour a line. Colour slot = series index modulo 8.
- **DC-CHART-5** The non-colour differentiator of a series is its vertex marker shape. The ordered shape list is circle, rect, rotated rect (diamond), cross, rotated cross, star; the triangle is reserved for peaks. Shape index = (colour slot + lap) modulo 6, where lap = series index divided by 8 (rounded down). Consequences: the first six series differ by shape alone; for up to 48 series the pair (colour, shape) is unique; beyond six series shape and colour together are the cue, and a series may then share its shape with another series of another colour. A marker radius is at least 3 px so the shape is recognisable. The legend swatch draws the same shape in the series colour; the tooltip names the series first.
- **DC-CHART-6** All metric series share one stroke width (one token) and the solid line style; no series is dashed, dotted or thicker. Dash patterns are reserved for estimated values and outage bands. The status palette and its HTTP status classes are reserved for status and outage cues, except the single-metric online line of DC-CHART-3.
- **DC-CHART-7** Proof: for N plotted series the pairs (`borderColor`, `pointStyle`) are pairwise distinct for N up to 48; the first six `pointStyle` values are pairwise distinct; the legend swatch shape equals the dataset `pointStyle`; every measurement dataset is solid. `tests/js/chart-series-colors.test.mjs` asserts the shape list and the pair uniqueness of `markerStyle` for 48 series; `tests/layout/combined-legend.spec.mjs` (Chromium and WebKit, light and dark, 1280x720, 390x844, 320x640) asserts the dataset pairs, the swatch shape, the 3 px radius and the solid style. Open: a chart screenshot review in light and dark.

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-CHART-1 | DC-CHART-6 (solid lines) | UDP series of the availability step chart | The availability chart is a status chart; the dash is a protocol cue repeated in its legend swatch. | `charts/chart-builder.js` | `tests/js/availability-chart-clip.test.mjs` |

#### Interaction and accessibility

- **DC-CHART-8** Each measured response-time vertex is a visible marker on the line, with a larger hover marker and a touch hit area of at least 24 CSS px; hover and tap reveal the value. The focused chart accepts Left and Right to inspect visible points, Home and End for the first and last, shows their tooltip and announces time, metric name and value. The chart keeps a text summary for assistive technology. Legend entries are buttons with `aria-pressed`. A legend button MAY emphasise its series on hover and focus without recolouring.
- **DC-CHART-9** At 320 px and 390 px, markers, tooltip, legend and peak switch stay usable inside the chart card without horizontal page overflow; the chart keeps its compact axis layout and does not depend on hover alone.

#### Peaks

- **DC-CHART-10** Peaks are not legend entries. One shared "Show peaks" switch (`role="switch"`, visible label, `ju-switch` target size) shows or hides the peaks of all visible series; the default is on and the choice is not persisted. A notable peak (as in the PDF report: above the Tukey fence of all maxima, at least 1.5x and 1 ms above its interval median) is a stand-alone red upward triangle (`--ju-peak`, light and dark) with a thin solid red stem down to the median of its interval, on the shared response-time axis. There is no peak connecting curve and no second axis; peaks are not joined to each other and are clipped at the chart area (DC-CHART-1). A peak is drawn at most 3x the highest median of its series so one outlier cannot flatten the curve; the tooltip gives the real maximum, the median and the note that the marker sits at the axis limit. A series hidden in the legend shows no peak. Toggling keeps the zoom range.

Owners: CSS `target-detail.css`, palette tokens in `gentelella-layout.css`; JS `js/charts/*` (`combined-chart.js`, `combined-chart-data.js`, `chart-builder.js`, `chart-reset.js`); template `target_detail.html`; tests as named above.
## 7 Responsive behaviour and accessibility

### 7.1 Responsive behaviour

Terms are defined in 1.2; breakpoints, container queries and the drawer are in 5.3 and 5.4. This subsection decides what each configuration may claim.

- **DC-RESP-1** Page content MUST stay usable at a CSS viewport of 320 px width and with text enlargement of 200 % without page-level horizontal scrolling. Content whose meaning needs two dimensions (a wide data table in `.ju-table-viewport`, `pre`, a map) MAY scroll inside its own region (DC-SCROLL-2).
- **DC-RESP-2** CSS viewport, browser zoom, text enlargement and device pixel ratio are four different things. A test claims only what its configuration proves:

| Configuration | What it changes | A test MAY claim | A test MUST NOT claim |
| --- | --- | --- | --- |
| CSS viewport | Layout width and height in CSS px | Layout, breakpoint and container-query behaviour at that size; reflow at 320 px (DC-MEAS-1) | Zoom, text scaling, pixel density |
| Effective browser zoom Z | Viewport (W/Z)x(H/Z) at DPR Z | Layout at an effective zoom of Z, named "effective 200 % zoom (640x360 CSS px at DPR 2)" | The browser zoom feature, its scrollbar widths, or text enlargement |
| Text enlargement | Root font size at an unchanged viewport | No loss of content or function when text grows (DC-MEAS-2) | Zoom, reflow at a narrow viewport |
| Device pixel ratio | Pixel density only | Canvas and image sharpness, `resolution` media queries | Any reflow, zoom or text claim |
| Real browser zoom, iOS text size, keyboard, safe areas, browser toolbar | The product behaviour itself | Only when run headed in the named browser or at E-IOS and recorded | Anything else; unrecorded runs stay "unverified" (DC-MEAS-3) |

- **DC-RESP-3** A test profile or title names its configuration: `viewport-640x360`, `zoom-200-effective`, `text-200`, `dpr-2`. The label "200 % zoom" is allowed only for the effective-zoom construction of DC-MEAS-1. The helpers `cssViewport`, `effectiveZoom` and `textEnlargement` in `tests/layout/test-support.mjs` build the profiles (names `css-640x360`, `1280x720-effective-zoom-200pct`, `css-1280x720-text-200pct`); a run that only changes the viewport at DPR 1 is named `css-WxH`.
- **DC-RESP-4** Test layers: static pytest checks prove markup, CSS rules and tokens; `node --test` unit checks prove JavaScript logic; Playwright layout specs prove computed layout and behaviour in Chromium and WebKit (Firefox per project policy) at E-ENGINE; accessibility-tree, screen-reader and iOS behaviour need manual or E-IOS evidence. A layer MUST NOT report a claim of a higher layer as passed.
- **DC-RESP-5** GridStack keeps its existing mobile flow and MUST NOT overwrite saved desktop layout intent.
- **DC-RESP-6** Pointer targets are at least 24x24 CSS px and 44 px on coarse pointers (`--ui-target-min`) unless a WCAG 2.5.8 exception applies.

### 7.2 Accessibility

- **DC-A11Y-1** The target is WCAG 2.2 AA: text contrast at least 4.5:1, UI components, state indicators and focus indicators at least 3:1, in both themes and in every state a component defines.
- **DC-A11Y-2** Native links and buttons, labels, the skip link and status text are preserved. Colour is never the only carrier of status, selection or series identity (DC-PRIN-2).
- **DC-A11Y-3** A state attribute (`aria-expanded`, `aria-selected`, `aria-checked`, `aria-current`, `aria-pressed`) MUST follow the visible state at every update, not only at render. Every id an `aria-*` reference names exists once in the document. A hidden or inactive panel is out of the tab order and the accessibility tree (`hidden`, `inert` or `display: none`). A control marked `aria-disabled="true"` also ignores activation in script.
- **DC-A11Y-4** Dynamic status text reaches assistive technology through a live region: `role="status"` for progress and results, `role="alert"` only for request-level and form-level failures (DC-FORM-11, DC-FORM-12). Statuses are announced, never moved to by focus.
- **DC-A11Y-5** Automated checks prove structure, geometry and computed style. They do not replace a screen-reader pass or expert review. A manual or device check that was not run MUST NOT appear as passed in a report.

### 7.3 Themes, forced colours and reduced motion

- **DC-THEME-1** Light, dark and system modes use one structure and the same tokens, switched by `data-bs-theme`; no component branches per theme (DC-PRIN-3, DC-TOK-1). System mode tracks the operating-system preference. A theme switch updates the wordmark and every visible component immediately, without reload; a layout check switches the theme while components are visible.
- **DC-THEME-2** In `forced-colors: active` a boundary or state that depends on colour, shadow or background has a system-colour replacement: dots use `CanvasText`, the active tab gets a `Highlight` border and underline, every focus variant keeps a 2 px `Highlight` outline (DC-FOCUS-3).
- **DC-THEME-3** Reduced motion follows DC-TOK-6.
- Evidence: layout specs run in light and dark; `tests/layout/focus-style.spec.mjs` covers forced colours and keyboard navigation; `tests/test_ui_accessibility.py` covers tokens in both themes. The forced-colours case for dots and the active tab is `tests/layout/contract-invariants.spec.mjs` (`@contract-invariants`). Open: the result of the text-enlargement audit profile of DC-MEAS-2 (see OR-14).
## 8 Interaction, state and content conventions

### 8.1 Interaction states

- **DC-INT-1** Loading, empty, error, offline and disabled states keep their established labels and messages. An interactive component defines its states per DC-EXT-5. An empty list or card shows an explicit empty state (`.ui-empty`), never an empty frame.
- **DC-INT-2** An action that needs an unavailable backend capability (for example **Scan ports** without the Nmap worker) is disabled with the native `disabled` attribute. The reason is visible next to it as text in a polite live region, linked with `aria-describedby`, and says what is missing and what to do instead. The state is read when the form loads and re-checked on focus, on a slow timer and by an explicit **Check again** link. Related inputs and manual alternatives stay usable; saving never depends on the disabled action.
- **DC-INT-3** A control that starts a request and must keep keyboard focus (Re-check Signal, Refresh public IPs) uses `aria-disabled="true"` and `aria-busy="true"` while the request runs, ignores a second activation, and is never natively `disabled`.
- **DC-INT-4** Warnings carry the warning sign (DC-WARN-1). Tone and wording of labels follow 8.4.

#### Controls: cursor and switch geometry

- **DC-INT-5** A switch has the same track and thumb size in both states. The off state uses `--ju-control-edge` for the track edge and `--ju-switch-thumb-off` for the thumb, at least 3:1 against the surface, and the thumb position carries the state.
- **DC-INT-6** In application pages an operable control shows `pointer`: buttons, native selects, links with `href`, `summary`, tabs, switches, checkboxes, radios and their `.form-check-label`. A disabled toggle and its label show `not-allowed`; a disabled native select shows `not-allowed`. Text fields keep the text cursor; drag handles keep `grab`, `grabbing` and resize cursors.
- Checks: `tests/layout/focus-style.spec.mjs` (`@focus`: switch geometry and cursors on the real pages).

### 8.2 Filter-as-you-type search

Applies to every list page with a search: Targets (`targets.html`, `targets-search.js`) and Settings > Recipients (`settings/recipients.html`, `settings.recipients.js`). Both use `utils/list-filter.js` (`normalizeQuery`, `filterRows`, `statusMessage(count, noun)`); a page MUST NOT carry its own matching. Presentation is the shared `.ui-search` in `ui.css`.

- **DC-SEARCH-1** The field is `<input type="search">` inside `.ui-search[role="search"]`, between page header and list, with a visually hidden `<label>`, a placeholder that only repeats it, `autocomplete="off"`, `maxlength="100"` and the shared `.form-control` focus. It is full width on narrow containers and at most 28 rem wide on wide ones; its height and the clear button use `--ui-target-min`. Icons are local Material Icons.
- **DC-SEARCH-2** Filtering is client-side on rendered rows while typing; no Enter, no request per keystroke. Matching is a plain substring test after trim, whitespace collapse, lower-casing and diacritic folding. The query is never a pattern and never inserted as HTML. Targets search name, host, HTTP URL, TCP ports (only with the TCP check on), UDP ports (only with the UDP scan on) and group name from `data-search`.
- **DC-SEARCH-3** All items MUST be in the DOM: a paginated list requests its unpaginated fragment once when a search starts (`all=1`) and the paged fragment once when it ends. Polling keeps the query and evaluates new rows against it.
- **DC-SEARCH-4** Groups without a hit are hidden. Groups with hits open for the duration of the search by class change only (no Bootstrap collapse event, so the stored manual state is untouched); clearing restores the earlier state, and a manual toggle during the search becomes that state. The group count shows hits against the total (`2 / 5`).
- **DC-SEARCH-5** No hit: the list is replaced by `.ui-empty` `No targets match "…"` (the term set as text) with a `Show all targets` button; no empty frame remains. A visually hidden `role="status"` region announces `n targets found` or `No targets found`.
- **DC-SEARCH-6** The clear button (shown only with a value; the browser's own is hidden) and Escape empty the field and keep focus in it. The query is not persisted. No `/` shortcut exists.
- **DC-SEARCH-7** Recipients: the field sits in the card between header and list, outside the reloaded list. It searches name, email address and Signal number (`data-search`); these are personal data, so filtering stays in the browser and nothing goes to URL, storage or logs. Rows without a hit are `hidden`; with no hit the table is hidden in favour of `.ui-empty` with `Show all recipients`. Row actions work on filtered rows. A list reload re-applies the query; creating a recipient clears it.
- Checks: `tests/js/list-filter.test.mjs`, `tests/test_targets_search.py`, `tests/layout/targets-search.spec.mjs` (Chromium and WebKit, light and dark, 1280x720, 390x844, 320x640, effective 200 % zoom), `tests/test_recipients_search.py`, `tests/layout/recipients-search.spec.mjs`.

### 8.3 Load stability and live updates

Applies to pages that refresh themselves; the Targets list (`targets.html`, `targets-live-update.js`) is the reference. Another self-refreshing list follows these rules before it ships.

- **DC-LIVE-1** A live refresh MUST NOT replace a list that did not change structurally. The answer is compared with the page; with the same groups and rows (matched by `data-target-id`) only cells whose shown value changed are rewritten. Rows, self-updating status icons, focus, open groups, the search state and the scroll position stay untouched. Unchanged text is not rewritten, because a repeated write replaces the text node and repeats a live-region announcement.
- **DC-LIVE-2** A structural change (target added or removed, other group, page or scope) uses the normal swap. Around the swap, `<main>` keeps its scroll position (the same `scrollTop`; when the list height changed, the first visible `data-target-id` row keeps its offset), and focus stays on the control of the same `data-target-id`. If that row was removed, focus moves to the control of the next row, else the previous row, else the search field. A page change that the user requests through the pager is not a live update and starts at the top of the list.
- **DC-LIVE-3** The following survive both paths: search query and hits, stored and search-temporary group state, `+N more` expansion by target id, the page and scope, and focus.
- **DC-LIVE-4** A response applies only when the page and scope parameters of its request equal the current state on arrival. A superseded or late response, including one that arrives in another order than the user actions that caused the requests, is dropped and the current state is refreshed. The mechanism (`hx-sync`, a request token) is free; a test with delayed responses proves it.
- **DC-LIVE-5** Refresh events inside the throttle window collapse into one trailing refresh; none is lost. No timer waits for DOM state.
- **DC-LIVE-6** Swap-time scroll handling has one owner, `targets-live-update.js`, as one before and after pair. It is a fallback for the structural path only and MUST NOT hide an unnecessary replacement: DC-LIVE-1 is the default and a test asserts that an unchanged answer leaves DOM nodes and `scrollTop` untouched. `targets-search.js` carries no scroll restore.
- **DC-LIVE-7** Fonts: IBM Plex Sans uses `font-display: fallback` (DC-TOK-8). `tests/layout/load-stability.spec.mjs` waits for `document.fonts.ready`, requires Plex loaded and in use, forbids horizontal overflow in every frame, and compares landmark positions in WebKit from the first Plex frame on (Chromium from the first frame).
- Checks: `tests/layout/targets-live-update.spec.mjs` (Chromium and WebKit, 1280x720 and 390x844: row and status-icon identity, removed nodes, focus, groups, filter, layout shift in Chromium) and `tests/test_targets_search.py` (row ids). The same spec proves, per case, DC-LIVE-1 to DC-LIVE-5: an identical answer writes nothing, a changed cell, a removed or added target with the scroll anchor and focus successor, a shorter list near the end, a changed group with filter and group state, a superseded or late response, the pager starting at the top, and the trailing refresh (Chromium and WebKit, 1280x360 and 390x520).

### 8.4 Content voice

- **DC-VOICE-1** Labels are short, concrete English terms already established in justUp! (Dashboard, Targets). Technical status values stay precise. WireBuddy product copy is not imported. A warning message follows DC-WARN-1.

## 9 Feature-specific contracts and justified exceptions

A feature contract refines the global invariants and component contracts for one surface (DC-PREC-1, level 4). It MUST NOT restate a rule of sections 4 to 8; it references the rule ID. Each contract below is the single normative owner of its surface (DC-OWN-2). The checks named at the end of a contract are the proof; tolerances and timings belong to those tests (DC-NORM-4).

- **DC-FEAT-1** A feature surface MUST follow the state taxonomy (DC-STATE), the focus policy (DC-FOCUS), the field-error pattern (DC-FORM-6 to DC-FORM-11), the scroll registry (DC-SCROLL) and the warning marker (DC-WARN-1) unless an exception row in this part or in sections 4 to 6 says otherwise. A feature MUST NOT define a local focus colour, a local status component or a local scroll container.
- **DC-FEAT-2** A feature exception MUST have an exception row (DC-EXC-1) with scope, rationale, owner and an existing test. A test that does not exist yet makes the row invalid; the need is then listed in 10.6 as an open requirement instead.
- **DC-FEAT-3** The Notifications focus is governed by the global focus policy (variant V3, DC-NOTIF-4). No exception row exists for it.

### Exception index

Exception rows live with the rule they modify (DC-EXC-3). This index lists all of them.

| ID | Modifies | Scope | Home |
| --- | --- | --- | --- |
| DC-EX-TOK-1 | DC-TOK-1 | `--ju-text-*` tokens | 4.1 |
| DC-EX-FOCUS-1 | DC-FOCUS-7 | `main`, `#server-offline-overlay` | 5.5 |
| DC-EX-FOCUS-2 | DC-FOCUS-1 | `.ju-sidebar` focus colour | 5.5 |
| DC-EX-STATE-1 | S2 (dot) | Target state icon in lists and dashboard status heroes; `.target-status-pill` in the detail header | 6.1 |
| DC-EX-STATE-2 | S5 (badge allowlist) | Port-scan result badges | 6.1 |
| DC-EX-CHART-1 | DC-CHART-6 | Availability step chart | 6.7 |
| DC-EX-DOWNTIME-1 | DC-STATE-2 (dot shapes) | Event-kind markers of the downtime list | below |

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-DOWNTIME-1 | DC-STATE-2 (shape mapping of dots) | The three event-kind markers in `.downtime-scroll` | They identify the kind of a past event (downtime, data gap, unknown), not the current state of a check. Filled circle, dashed circle and diamond differ in shape without colour, and each is paired with its word. | `target-detail.css` | `tests/layout/downtime-history.spec.mjs` (`tests/layout/endpoint-state.spec.mjs` asserts the marker shapes; `downtime-history.spec.mjs` covers the list layout) |

### Targets list: check states

- **DC-TLIST-1** The Targets list MUST show check states with the `state_dot` and `cert_dot` macros of the target detail (S1, DC-STATE-1). The label identifies the check (`HTTP`, `Cert`, `TCP:25`, `UDP:53`); word, colour and, for UNKNOWN, the hollow ring distinguish UP, DOWN and UNKNOWN. A check state MUST NOT carry an icon. A certificate warning keeps its own certificate state. The overall target state stays a separate cell (S2).
- **DC-TLIST-2** At mobile widths the list MUST show the total number of checks and the DOWN and UNKNOWN counts, then at most three check dots in the order DOWN, UNKNOWN, certificate alerts, UP (stable order inside a category). Further counters (`Cert alert`, `Passed`) MAY be shown while they stay non-redundant and do not crowd the row at 320 and 390 px; `Passed` counts successful checks, not reachable targets (`title` "Successful checks"). Certificate alerts are counted separately from DOWN. `+N more` MUST reveal every dot of that row, `Show fewer` MUST restore the preview, and the expanded state MUST follow the target id through live updates.
- **DC-TLIST-3** Checks: `tests/layout/endpoint-state.spec.mjs`, `tests/layout/targets-search.spec.mjs`, `tests/test_endpoint_view.py`.

### Targets list: live-update transactions

Refines 8.3 (DC-LIVE-1 to DC-LIVE-7) for the Targets list. The generic rules stay in 8.3; this table fixes the outcome per case. DOM identity, scroll anchor, open groups, filter and focus are one transaction: every case below MUST hold at the same time, not one at a time.

- **DC-TLIVE-1** A structurally unchanged list MUST NOT be replaced. This takes precedence over any scroll restore: a `scrollTop` restore is at most the documented fallback for the structural path (DC-LIVE-6) and MUST NOT be used to hide a replacement that did not need to happen.
- **DC-TLIVE-2** The cases of the table MUST give the stated outcome.

| Case | Required outcome |
| --- | --- |
| Identical polling response | No DOM write at all: row, cell and status-icon nodes, text nodes, focus, `scrollTop`, open groups and filter state keep their identity. No live-region announcement. |
| Same rows, changed cells | Only the changed cells are rewritten; the rows stay the same nodes. Scroll position, focus and open groups are unchanged. |
| Target added or removed, other group, other scope | Normal swap (DC-LIVE-2). The first visible `data-target-id` row keeps its offset in `<main>`; focus moves to the same id, else the next row, else the previous row, else the search field. |
| Group membership or order changes | Structural path. A group the user opened or closed keeps that state by group key; a group that disappears drops its state. |
| Filter active, filter changes, filter cleared | The query and hit marks survive both paths. Rows hidden by the filter stay hidden after a refresh. The search-temporary group state is restored when the filter is cleared. |
| Pagination | A page change the user requests starts at the top of the list and is not a live update. A live refresh keeps the current page and scope. |
| Scroll near the end of the list | The anchor row keeps its offset. A shorter list clamps to the new end and MUST NOT jump to the top. |
| Keyboard focus inside the list | Focus stays on the same control, or follows the successor rule above. A refresh MUST NOT move focus to `body`. |
| Late or superseded response | Dropped when its page or scope differ from the current state (DC-LIVE-4). The current state is refreshed. |
| Two competing requests (poll and user action, poll and poll) | Only the response that matches the latest user action is applied, even when it arrives first or last. A user action wins over a poll that started earlier. |
| Refresh events inside the throttle window | One trailing refresh (DC-LIVE-5). |

- **DC-TLIVE-3** Owner: `targets-live-update.js` carries the comparison, the patching and the single before-and-after scroll pair. `targets-search.js` MUST NOT carry a scroll restore. Checks: `tests/layout/targets-live-update.spec.mjs`, `tests/layout/targets-search.spec.mjs`, `tests/test_targets_search.py`.

### Settings > Delivery queue: labels, log and channel status

- **DC-SPOOL-1** Status labels are defined once, in `settings.spooler.js` (`_statusBadge`, which builds a status dot through `JuStatusDot.html`, S3, no icon), and mirrored by the static filter and summary labels of `settings/spooler.html`: `pending` Waiting for handoff, `processing` Handing over, `sent` Sent, `failed` Handoff failed. Stored values MUST NOT change. `Sent` has no tooltip; its meaning (accepted by the SMTP server or signal-cli, delivery to the recipient not verifiable) lives in `docs/features/notifications.md`.
- **DC-SPOOL-2** Channel and type columns MUST show an icon with an accessible name (`title` plus `visually-hidden` text, icon `aria-hidden`), use `ju-cell-icon` and distinguish values by icon shape, not by colour (S7).
- **DC-SPOOL-3** The log is an `.rtable`. In a narrow container each entry MUST be a card with label and value rows (labels from `data-label`); the channel row shows the icon and the visible name. The generic `.rtable` cell rules stay unchanged.
- **DC-SPOOL-4** The toolbar (`.spooler-toolbar`, a container of its own) MUST be grouped by function: a title row (title left, Refresh right), a filter row (Status and Channel as two equal-width selects with visible labels) and a management row (`Clear log`, `Flush queue`, right-aligned, always with a visible label). From a container width of 40 rem the filters and management actions share one row.
- **DC-SPOOL-5** Loading and empty rows use `.spooler-log__state`: one centred message (`No entries`, or `No matching entries` while a filter is set). In table mode (container at least 40 rem) the state cell MUST be a real table cell spanning all columns; in card mode it is a block.
- **DC-SPOOL-6** The Channel status card MUST be one responsive list with separate Email and Signal rows, each with icon, name, waiting count and handoff-failed count at every width. Next attempt, Oldest and Cooldown until MUST appear only when their values exist; a missing time has no placeholder. The rows wrap inside the card without an inner scroll area and without a second desktop and mobile data model. A zero count MUST NOT imply successful delivery. The SMTP handoff and recipient delivery distinction is explained in `docs/features/notifications.md`, not in a Settings note.
- **DC-SPOOL-7** Checks: `tests/layout/spooler-log.spec.mjs` (both modes), `tests/js/spooler.test.mjs`.

### Recipients

- **DC-RCPT-1** Saving an existing recipient with "Enable for all Targets" switched off MUST ask for confirmation through the shared confirmation (DC-MODAL-14) when assigned targets, manual assignments included, would be removed. Cancel MUST keep the edit dialog and its draft open and send no request.
- **DC-RCPT-2** The row actions of the recipients list MUST carry an icon-only `btn-icon` `.recheck-signal-btn` (icon `sync`) for rows with a Signal number; its `aria-label` is `Re-check Signal for <name>`. While the request runs the button is `aria-disabled="true"`, `aria-busy="true"` and `.disabled`, never natively `disabled`, so focus stays on it; a second activation is ignored.
- **DC-RCPT-3** The result is the Signal status of the row, an S3 state (DC-STATE): status dot and state word (`Registered` success, `Not registered` warning, `No account` neutral, `Check failed` neutral; the full text is the `title`). While `Checking...` runs it is S6: spinner and word with `aria-busy`, no dot. The status carries no icon. A toast keeps the announcement. The list shows no explanatory text. The request MUST carry the recipient id only, never the number.
- **DC-RCPT-4** Checks: `tests/js/recipients-signal-recheck.test.mjs`, `tests/layout/recipient-signal-recheck.spec.mjs`, `tests/layout/recipient-channels.spec.mjs`.

### Settings > Network: Public outbound IPs

A read-only card that discloses externally observed network information shows the current value, state and action first; provider, privacy and routing explanations live in linked documentation (`docs/features/network-monitoring.md`).

- **DC-EGRESS-1** The card (`.egress-card`, classes `.egress-*` in `ui.css`) is the fourth card of the Network grid, after Geolocation, titled `Public outbound IPs`. It MUST be read-only: no `input`, no `name`, nothing the settings autosave could pick up. The footer MUST link to the Public outbound IPs documentation.
- **DC-EGRESS-2** One `egress-row` per family (`IPv4`, `IPv6`, in that order) MUST show a label, the address in a `<code>` (`—` when none), a status dot with word (`.ju-status-item`, S3) and an icon-only copy button `btn btn-icon btn-outline-secondary` (`aria-label` and `title` `Copy IPv4 address` / `Copy IPv6 address`). From a list width of 28 rem (container query) the columns are address, status, copy; narrower, both families use a two-line pattern (status on top, address and copy below).
- **DC-EGRESS-3** A detail line appears only when a state needs explanation; Available has none. A message MUST be one short line (about 40 characters, for example `Resolved to private address.`), single-line with an ellipsis and the long explanation in `title`. Technical wording (echo service, not contacted) is `title` only.
- **DC-EGRESS-4** A muted `.egress-geo` line (0.75 rem, one line, ellipsis, full text in `title`) MAY follow the address with flag, ASN with operator and place; each part appears only if known, and no line and no gap appear otherwise. The flag (`flag-icons` SVG, code text if it does not load) is decorative. The copy button copies the address only. Values MUST reach the DOM through `textContent` only.
- **DC-EGRESS-5** Long IPv6 values MUST wrap inside the card. The card has no inner scroll region and the page no horizontal overflow at 1280x720, 390x844, 320x640 and effective 200 % zoom (DC-MEAS-1).
- **DC-EGRESS-6** States (dot tones of 6.1): Available (success), Inconsistent (warning), Unavailable (neutral), Lookup failed (danger); Checking is S6 (the word without a dot). Each family has its own state; one failing family MUST NOT hide the other. An unknown status string, an inherited object key (`constructor`, `__proto__`, ...) or a missing or non-string address for Available MUST be Lookup failed. There is no Stale state: the server caches the last confirmed address (persisted, replaced only by a different confirmed address) and shows it as Available when a refresh fails.
- **DC-EGRESS-7** Only Available MUST be copyable; a cached address is copyable like a fresh one. Inconsistent lists the observed values as text without copy. Copy confirms through the `role="status"` live region and a brief `check` icon.
- **DC-EGRESS-8** Refresh: one request when the fragment first appears and one per **Refresh** click, no polling. The button uses `aria-disabled` and `aria-busy`, never `disabled`; a second activation is ignored. While running, the previous addresses stay visible (not copyable) and the card carries `aria-busy`. If the request fails (HTTP error, 429, timeout), the last shown address stays Available; without an earlier value the row shows Lookup failed. The footer shows `Last checked` with `checked_at` and a visible `Cached` marker when the API reports `cached`; it MUST NOT imply a new measurement.
- **DC-EGRESS-9** When the API reports `proxy_env`, a brief warning hint says the addresses are measured directly. It is a warning message and follows DC-WARN-1.
- **DC-EGRESS-10** `initPublicEgress` MUST bind once per card element (`data-egress-bound`); a card re-rendered by HTMX is bound again.
- **DC-EGRESS-11** Checks: `tests/test_public_egress.py`, `tests/js/public-egress.test.mjs`, `tests/layout/public-egress.spec.mjs`.

### Settings form fields

- **DC-SETF-1** A settings text field that offers a helper action (for example "Public hostname" with auto-detect) MUST use a Bootstrap `input-group` with one icon-only `btn-icon` after the input. The button has an `aria-label` and a matching `title`; the field has a visible `<label>` and `aria-describedby` for its help text (DC-FORM-1, DC-FORM-3).
- **DC-SETF-2** Settings fields save on `change` or blur. The server's validation message (`422` detail) is shown in the toast and, for the field concerned, as a field error (DC-FORM-12, last row). A discreet `form-text` hint SHOULD explain the consequence of an empty value.
- **DC-SETF-3** Checks: `tests/layout/public-hostname.spec.mjs`, `tests/js/settings-autosave-retry.test.mjs`.

### Notification list

Applies to the rows of the Notifications page (`notifications.html`, `notifications.css`). Rows reuse the shared tokens; there is no notification palette and no second component system.

- **DC-NOTIF-1** Rows MUST sit on the shared panel surface `--ju-panel-bg`. Hover and keyboard focus MUST use `--ju-panel-hover` (defined for light and dark in `gentelella-layout.css`), the same as other interactive panel rows. `--bs-tertiary-bg` MUST NOT be the row hover colour.
- **DC-NOTIF-2** Hover MUST NOT be expressed by a warning or error colour; no yellow, orange or brown hover surface exists. Hover changes only the background colour. The severity dot, unread marker, error tint and left borders survive hover and focus.
- **DC-NOTIF-3** The unread marker (ringed dot, bold title, primary left border) and the event severity (S4: status dot and word, error tint and left border) are separate cues. The unread marker is an item marker and MUST NOT use `.ju-status-dot` (DC-STATE-8).
- **DC-NOTIF-4** Keyboard focus MUST stay visible without hover and uses focus variant V3 (DC-FOCUS-6). The delete button keeps focus variant V2 and reveals the delete action. No feature-local colour or width is allowed (DC-FEAT-3).
- **DC-NOTIF-5** Layout roles: the primary line is category icon, title and severity dot with word; the timestamp has its own right-hand column; the secondary slot is the description (`.ju-notif-context`, clamped, full text in `title`), then the resource status line. Wide rows keep the time in the title line; below 992 px the time sits beside the description and the status line takes its own row. A margin, absolute position or transform correction per notification type MUST NOT be used.
- **DC-NOTIF-6** The coloured error bar (left border and tint) has a different meaning by place, on purpose. In the popover only unread state is marked: an unread error row is "new". On the notifications page the bar shows the event severity (S4). The popover MUST NOT be changed to show severity through the bar, and the page MUST NOT drop it. Independently of the bar, every popover row MUST show its severity as status dot plus the visible word `Error`, `Warning`, `Success` or `Info` (`notifications.js`, checked by `tests/layout/notif-popover-severity.spec.mjs`); unread and severity stay independent. `info` keeps the neutral ring with the visible word "Info" in both places (DC-STATE-3, DC-STATE-4).
- **DC-NOTIF-12** A resource that is gone (`Report no longer available`, `Report expired`, `Target no longer exists`, `Alert no longer available`) MUST render as one muted status line (`.ju-notif-unavailable`: icon `link_off` plus the label from `action.label`, real text, no generated content, no link, no fill) directly under the description. All resource types share this class. An available report download and a target link MUST stay native links; a missing resource MUST NOT offer a dead link.
- **DC-NOTIF-7** Resource availability MUST NOT change the original event: title, severity and description of a successful PDF stay as they were.
- **DC-NOTIF-8** Normal, unread, error, hover, focus and delete-reveal states compose. The revealed delete action narrows the content and MUST NOT overlap text. On touch layouts, swipe MAY reveal the delete action; it MUST remain reachable and MUST NOT cover content. The delete action MUST be reachable and activatable by keyboard.
- **DC-NOTIF-9** The layout follows the row width, not only the viewport. Long file names and technical identifiers wrap or ellipsize (full value in `title`); the page MUST NOT scroll horizontally down to 320 px and at effective 200 % zoom (DC-MEAS-1). Title, description, resource status and timestamp stay within the row; the resource status sits below its description. On phones the time MAY break into date and time when the delete action is open.
- **DC-NOTIF-10** Light and dark use the same structure and tokens. Ownership: `gentelella-layout.css` owns colour tokens, `ui.css` shared primitives (dots and badges), `notifications.css` the row layout and presentation only.
- **DC-NOTIF-11** Checks: `tests/layout/notifications.spec.mjs` (helper `notifications.mjs`, tag `@notifications`, Chromium and WebKit, light and dark, 1280x720, 390x844, 320x640 and effective 200 % zoom) for hover colour, contrast, warm-hue absence, unchanged severity dot and unread marker, focus outline, status-line alignment, one secondary slot, one time column, containment, delete overlap, swipe reveal, native download links and keyboard deletion; `tests/test_notifications.py` for the status-line markup. The severity assertions measure the severity dot and fail on a severity badge.

### Dashboard widgets

Applies to the widgets of the dashboard grid. Sizing, edit-mode geometry and persistence belong to this contract; status, focus and scroll rules come from sections 5 and 6.

#### Shared widget structure

- **DC-DASH-1** Every dashboard tile MUST have an outer card, a reserved header and a flexible content body. The header aligns title, status and edit controls without overlap. The body owns the widget content and its loading, ready, empty and error states. Widgets MAY differ in their inner layout; shell behaviour, typography roles, spacing, status treatment (DC-STATE), borders and focus (DC-FOCUS) come from the dashboard component rules and shared tokens.
- **DC-DASH-2** The Online, Offline, Disabled and Total status heroes MUST use one centred composition: an unboxed status icon immediately before the large value in the same row, the uppercase label centred beneath. Icon and value form one group; neither sits at a card edge or in a filled icon tile. Each card fills its full two-row GridStack frame; the default width and placement stay the same, and the minimum width is two grid columns so icon and value stay unclipped. The icon is the target-state icon of DC-EX-STATE-1.
- **DC-DASH-3** While edit mode is active the move handle occupies a reserved header region and MUST NOT cover the title, status, values or resize controls. Dragging and resizing MUST stay keyboard accessible; the focused handle announces movement and size changes. Pointer targets meet DC-RESP-6, and visual states use shape or text as well as colour.

#### Delivery queue and metrics

- **DC-DASH-4** The Delivery queue MUST show exactly three primary metrics: Pending (`data.pending`), Processing (`data.processing`) and Failed (24 h) (`data.failed_recent`, falling back to `data.failed` when the recent count is absent or invalid). Each has a visible label and an associated value; zero is rendered as `0`. A derived total MUST NOT appear as another primary number unless it has its own label and a distinct product purpose. Equivalent metrics use one responsive container and equal typography.
- **DC-DASH-5** Numeric values in dashboard widgets MUST have the computed font size of the hero KPI numbers (`--ju-dashboard-value-size`); supporting labels keep their size and style. The larger values MUST fit the existing widget geometry: tile dimensions, registry minimums, row counts, padding and reserved header space MUST NOT grow to accommodate them.
- **DC-DASH-6** The queue status is separate from the three metrics and appears once, as the status dot with word of the widget header (S3, `JuStatusDot.html`; not a badge, DC-STATE-1). A load failure MUST replace stale metrics with a clear body error state. The Monitor Quality state `OK` and the Delivery queue state `Queue clear` MUST use the success dot (`ju-status-dot--success`) in light and dark. Colour is never the only status cue (DC-PRIN-2). The initial HTML is a loading shell; counts and status are rendered by the served dashboard script, so the browser path MUST be checked as well as the template.
- **DC-DASH-7** The Queue tile minimum width is 4 of 12 columns and its minimum height is three rows. A two-row minimum is allowed only when a browser measurement shows that title, longest status, all three metrics and the edit handle fit without clipping, overlap or body scrolling at the supported narrow and zoomed sizes, in view and edit mode; otherwise three rows stay. The stacking container query MUST NOT trigger at the minimum width (DC-BP-2), so the three metrics stay in one row there. The measurement is the `sizes` phase of `tests/layout/harness.mjs` (Chromium and WebKit, light and dark); the pixel arithmetic is a measurement record (A.4).
- **DC-DASH-8** Registry, `DASHBOARD_WIDGET_MIN_SIZES`, template defaults and GridStack limits MUST agree (DC-DASH-13).

#### Sizing contract

- **DC-DASH-9** The V2 layout is authoritative. The rows of the table are requirements.

| Layout state | Required behaviour |
| --- | --- |
| Default layout | Each widget starts at a compact size suited to its content; widgets need not share dimensions. |
| Automatic height | Height follows measured content in both directions; a stale saved height is not a permanent minimum. |
| Manual height | A resize switches that widget to manual height; later content or polling updates MUST NOT change it. A widget-specific minimum protects essential content. |
| Manual enlargement or reduction | The chosen dimensions stay stable after refresh and reload, subject to the declared minimum and the grid bounds. Content reflows or shows its compact state inside the tile. |
| Narrow widget | Content adapts to the tile's own inline size and stays inside the tile with its essential information. |
| Restored user layout | V2 widget identity, position, width, visibility and manual or automatic height intent are preserved. |
| Reset layout | Reset restores the registry defaults and automatic height; it MUST NOT leave a mixture of stale dimensions and new defaults. |
| Mobile and tablet stack | Natural content height where the grid stacks; responsive reflow MUST NOT overwrite the saved desktop arrangement. |

- **DC-DASH-10** Minimum sizes belong to the widget registry and are specific to each widget. A widget that cannot show all content at its minimum MUST use a defined compact or overflow treatment (a registered body scroll, DC-SCROLL-2); content MUST NOT escape the card or overlap another widget.
- **DC-DASH-11** Widget content SHOULD use container queries for compact, standard and expanded presentations. Viewport breakpoints serve the shell and the grid stacking only (DC-BP-2). Long labels, large values, empty data and text scaling MUST stay legible at the minimum width.
- **DC-DASH-12** The dashboard has one gap token, `--ju-dash-gap`, which CSS layout and GridStack both read; a per-side GridStack inset is derived from it. Cell height, card padding and type roles likewise have one token source (DC-TOK-3). A script fallback MAY exist for initialization failure only and MUST equal the token default.
- **DC-DASH-13** Widget minimum sizes have two owners: the widget registry (`dashboard-widgets.js`, which the client uses for GridStack node limits) and `DASHBOARD_WIDGET_MIN_SIZES` (server validation). The template carries default placement only and no `gs-min-*`. `tests/test_dashboard_layout.py` MUST fail when registry, server table and template defaults diverge.
- **DC-DASH-14** A stored V2 layout written under an older, smaller minimum MUST NOT be rejected and MUST NOT be migrated in storage: the client raises every stored size, hidden widgets included, to the current minimum while loading, keeps all other positions and pushes only overlapping tiles down. The server stays strict and answers 422 for a too small widget.

#### Edit mode geometry

- **DC-GRID-1** GridStack MUST be the only authority for widget coordinates and sizes; nothing else computes a position, row or column. The only exception is normalising a stored layout while it loads (raising sizes to the current minimum per DC-DASH-14 and pushing overlapping tiles down, `separateOverlaps()`); during drag, resize and keyboard move the script computes no position and GridStack decides.
- **DC-GRID-2** Widget frames (`.grid-stack-item-content`), the empty-cell slots painted in edit mode, the drag placeholder and the resize feedback MUST occupy the rectangle of the GridStack cell inset by the gutter. The slot layer takes its cell count from `grid.getRow()` and its cell size from the GridStack CSS variables.
- **DC-GRID-3** Edit mode MUST change neither the outer position nor the size of any widget. Every card fills its whole frame in both modes and the row count of a tile is the same in both modes.
- **DC-GRID-4** The drag handle takes space inside the frame: it shares the frame's single grid area and the card reserves the band as top padding (`--ju-dash-handle-height`) while editing. The handle MUST NOT shift the card or overlap text, and no widget has its own handle position.
- **DC-GRID-5** The tokens in `dashboard.css` are the single source for gutter (`--ju-dash-gap`, `--ju-dash-gutter`), cell height (`--ju-dash-cell-height`), radius (`--ju-dash-radius`) and handle band (`--ju-dash-handle-height`). `dashboard-grid.js` reads them for GridStack and holds only initialization-failure fallbacks equal to the token defaults. Empty cells use the surface tokens `--bs-tertiary-bg` and `--bs-body-bg` for a subtle gradient with a dashed border, so light and dark follow the theme and forced colours keep the border.
- **DC-GRID-6** Layout changes MUST stay compatible with the V2 persistence contract: stored coordinates and `height_mode` are never altered to compensate for CSS.
- **DC-GRID-7** Edit gestures MUST NOT scroll the document and MUST NOT make the scroll height follow the pointer. `<main>` is the scroller (DC-SCROLL-1). While editing, the GridStack `maxRow` is the settled layout plus a reserve of rows (`EDIT_RESERVE_ROWS` in `dashboard-grid.js`), refreshed after each gesture, keyboard move and content fit; outside edit mode the bound is open. GridStack's auto-scroll stays on so far rows stay reachable within that bound.
- **DC-GRID-8** A change to edit mode MUST be covered by the GridStack geometry audit (`node tests/layout/run.mjs --gridstack`, helpers in `tests/layout/harness.mjs`, tolerance `GEOMETRY_TOL_PX` there; CI runs it, `ci.yml`). It measures frame, card, slot and placeholder rectangles against the GridStack node coordinates before edit mode, right after entering it, after keyboard move and resize, during and after a drag, after saving, reload, cancelling and reset, the document and `<main>` scroll range while dragging and resizing to the bottom edge, and the hidden-controls check below 992 px, also at effective 200 % zoom. Local execution: 10.2.

#### Widget acceptance criteria

- **DC-DASH-15** At each widget's minimum, default and expanded size, header controls and content MUST stay inside the card without collisions or clipped essential information. Hero values, unchanged labels and existing tile dimensions MUST stay readable without overlap, clipping or body scrolling at those sizes.
- **DC-DASH-16** Across KPI, quality and data widgets, computed title size, weight, line height, header padding and title and status alignment MUST follow the same shell rules. Long titles wrap without colliding with status or edit controls at the minimum width and increased text size.
- **DC-DASH-17** Automatic widgets MUST grow and shrink after content changes. Manual heights MUST survive polling, save, reload and responsive reflow. V2 positions, widths, visibility and height modes restore; reset returns to the defaults.
- **DC-DASH-18** Compact, standard and expanded variants follow the widget container width; a narrow tile on a wide viewport and a wide tile on a narrow viewport are both tested. Loading, populated, empty or success, and error states MUST stay distinguishable; short and long labels and realistic data counts are tested.
- **DC-DASH-19** Move and resize handles MUST NOT cover headers or body content; pointer, keyboard and touch stay available and focus stays visible. Browser checks measure containment and handle overlap for representative sizes, stored-layout round trips, reset, resize and the mobile and tablet stack in light and dark.
- **DC-DASH-20** When the DOM, the sizing model or the handle placement conflicts with this contract, the implementation changes (DC-PREC-2) while the external contracts of 10.5 stay. The implementation report names how each criterion was verified and any gap.
- Recommendations: a semantic definition list per label and value pair with the label consistently above or below the value (SHOULD); reuse of `.dashboard-monitor-metrics`, `.dashboard-monitor-card`, `.dashboard-widget-header` and existing spacing and type tokens (SHOULD); a compact empty state that spends as little height as readability permits (SHOULD).
- Checks: `tests/layout/harness.mjs` (`sizes` phase and widget audits), `tests/layout/dashboard-metric-labels.spec.mjs`, `tests/js/dashboard-widgets.test.mjs`, `tests/test_dashboard_layout.py`.

### Target detail: Dashboard card stack

- **DC-TD-1** The Dashboard tab (`target-detail.css`, desktop fill mode from 1200x800) MUST stack KPI row (with the Checks hero), toolbar and chart row. Consecutive cards are one gutter (`--target-detail-gap`) apart; spare height goes to the chart row, never between cards. Every full-width card MUST get its own `auto` grid row and span all columns, because a card without a row of its own lands in the `1fr` row and stretches into an empty band.
- **DC-TD-2** The Checks hero MUST wrap its check dots (36 ports included) without horizontal overflow.
- **DC-TD-3** Check: `tests/layout/target-endpoints.spec.mjs` measures the gaps; the generic layout audit does not, because an empty band is no overflow, clipping or overlap.

### Traceroute map

The target detail page and the PDF share plotted hops, hop order, solid or dashed segment status, colours, line widths, marker gap and place-label selection (`route_map_rules.py`). Their renderers keep separate layout contracts.

- **DC-TRACE-1** Both outputs MUST show one route in hop order from the first mapped hop through every mapped estimate to the destination. A missing answer or location has no invented position; the gap is dashed and names the skipped hops. Reliable geographic back-jumps stay visible and are not reordered.
- **DC-TRACE-2** A solid link joins reliable endpoint positions. A dashed link has an estimated or approximate endpoint or bridges unmapped hops; the pattern carries the uncertainty. A solid curve is a schematic hop connection and MUST NOT be presented as a measured cable route.
- **DC-TRACE-3** Web overview: every visible route link MUST have a direction arrow tangent to the displayed curve; long curves repeat arrows at the web spacing of `route_map_rules.py`. An arrow means hop order on solid and dashed links; the dash alone describes geographic uncertainty. The legend states both meanings.
- **DC-TRACE-4** Web layout: curves MUST use deterministic, collision-aware lanes in projected screen space; repeated use of a corridor separates into lanes; nearby marker collisions create an ordered marker lane; a dense region becomes one region marker whose popup lists its hops in order. Placement starts from the shortest continuous world copy for routes crossing ±180°; marker boxes are measured after an initial placement and one bounded correction updates lanes and routes. The same geometry drives paths, arrows, hit areas and selection across timeline, table and overview.
- **DC-TRACE-5** Web scrolling: the hop table scrolls inside `.tr-table-scroll`; its header sticks to the top and its last (result) column sticks to the inline end with an opaque background, also on hover. The hop panel is a container: from 30rem panel width the table fits without sideways scrolling (host and location wrap between words, an IPv4 address, hop number, RTT and result never break); below that it scrolls sideways. The map card `.tr-map-panel` has no scroll area of its own at any size, on iPad included: in the height-bound panel (from 1200x800) the table/map row has a floor of 36rem (head, 20rem map, legend), and a lower main area scrolls as a whole. The table is a registered region (DC-SCROLL-2).
- **DC-TRACE-6** PDF: keeps its own geographic fit, circular marker fan-out, straight route links and arrow rule (`pdf_arrow_indices()`, `MIN_ARROW_SEGMENT_PX`: arrows on long solid links only). These differences MUST NOT change the shared hop order or uncertainty status.
- **DC-TRACE-7** The basemap stays secondary. Country labels are restricted to countries the chain touches; place-label selection is shared and web placement avoids measured markers and curves. A hop result is a connection state (S3).
- **DC-TRACE-8** Checks: `tests/js/traceroute-map-groups.test.mjs`, `tests/js/traceroute-map-layout.test.mjs`, `tests/test_route_map_rules.py`, `tests/test_traceroute_map_markup.py`, `tests/test_pdf_route_map.py`, `tests/layout/network-diagnostics-layout.spec.mjs` (iPad sizes, both orientations), `tests/layout/traceroute-map.spec.mjs` (Chromium and WebKit, light and dark, desktop and narrow, route details, selection, resizing, dateline wrapping).

### Target wizard: ports are entered once

- **DC-WIZ-1** The address step MUST run in this order: host name or address; name and group; **Ports to monitor** (the picker); optional Nmap discovery inside the picker. The wizard MUST show exactly one manual port input (`#wizard-manual-ports`) for TCP and UDP together, with the existing syntax (`22, 443` is TCP, `udp:53` or `53/udp` is UDP), and the chosen ports below it as removable chips (`TCP 22`, `UDP 53`).
- **DC-WIZ-2** `wizard-ports` and `wizard-udp-ports` stay the canonical form data as hidden backing fields; the picker keeps them in sync on manual entry, adoption from a scan, removal and submit. There is no second visible port list or input.
- **DC-WIZ-3** The checks step MUST NOT repeat ports as inputs. An active TCP or UDP check shows a summary row (`.wizard-port-summary`: label, comma-separated ports or "None chosen yet", and a `btn btn-link btn-sm` "Edit ports" button). "Edit ports" returns to the address step and focuses the visible manual input.
- **DC-WIZ-4** Text typed but not yet added MUST be taken over on Continue. Invalid text or a missing required port keeps the wizard on the address step, shows the field error through `JuFieldErrors` (DC-FORM-6 to DC-FORM-11, no `role="alert"`, `aria-invalid` on the input) and focuses the visible input, never a hidden field. Continue with an active port check and no port stays on the checks step and focuses "Edit ports".
- **DC-WIZ-5** The active step is the only vertical scroll region of the wizard and the picker adds none; Back and Continue stay in view. At heights up to 520 px `<main>` scrolls instead (DC-SCROLL-2, registered alternative).
- **DC-WIZ-6** Checks: `tests/layout/wizard.spec.mjs` (`@wizard`), `tests/js/target-wizard-preview.test.mjs`.

### Target editor navigation

- **DC-EDNAV-1** Edit Target -> Save Target -> Targets overview (`/ui/targets`). Only a complete save navigates: the target `PATCH` and, when the recipient list is loaded, the recipient `PUT` both succeeded. Any failure or partial failure MUST keep the editor open with the input and an enabled Save Target button.
- **DC-EDNAV-2** After a complete save the Save Target button MUST stay locked until the page changes, so the toast delay cannot start a second save. A pending notification task stays in `sessionStorage` for the global poller in `app.js`, which runs on the overview.
- **DC-EDNAV-3** Edit Target -> Cancel -> Targets overview. New Target -> Start monitoring -> Target detail (`/ui/targets/{id}`), handled by the wizard.
- **DC-EDNAV-4** Check: `tests/layout/edit-target-save.spec.mjs` (`@edit-target-save`).

### Port picker (manual entry and Nmap discovery)

One shared fragment, `fragments/network_scan_picker.html`, serves the wizard's address step and the Network tab of the target form. The pure rules live in `utils/port-scan-core.js` (`window.JuPortScan`), the DOM and requests in `network-scan-picker.js`. It reads and writes the TCP and UDP port fields and their check switches; only the final form submission saves anything. It is a plain section, not a card; the editor wraps it in its own card.

- **DC-PICK-1** Manual entry comes first and needs no scan: one text field with an `Add ports` button. Separators are commas or spaces; a port without a prefix is TCP; the range is 1 to 65535. Enter MUST add and MUST NOT submit the form or advance the wizard. Invalid tokens are named in the field error (DC-FORM-13), nothing is added and the text stays. Valid ports merge into the existing values (sorted, no duplicates, TCP 53 and UDP 53 stay two ports) and switch the matching check on. Chips (`tag-input__list`, label `TCP 22`, button `Remove TCP port 22`) follow the fields in both directions; removing a chip moves focus to the next remove button or back to the field.
- **DC-PICK-2** Nmap is optional and secondary. A native disclosure `Discover ports with Nmap (optional)` MUST be collapsed by default and holds the Nmap notice with the nmap.org link, the protocol checkboxes (they follow the check switches), the scope (Quick or Extended), `Scan ports` and, while a job runs, `Cancel scan`. It has no sub-cards, extra separators or second action bar. Manual entry MUST work fully without the worker. If the worker is down, the reason is visible next to the disclosure (not inside it) and `Scan ports` is disabled.
- **DC-PICK-3** Messages: one polite status line for progress and results and one `role="alert"` line for scan failures (DC-FORM-12), never both at once. Each error code has its own sentence and ends with the offer to enter ports manually. A changed host, a cancel or a newer scan makes every older answer stale; it MUST be dropped and the job id cleared.
- **DC-PICK-4** Results are ordered by relevance, not shown as a table. After a scan the status line summarizes the counts with confirmed open, closed and unclear kept apart (for example `16 ports found: 1 confirmed open, 2 closed, 13 unclear.`). Primary: `N confirmed open port(s)` lists ports with Nmap state `open` as compact rows (checkbox, `TCP 80`, service, `Open` badge), preselected for adoption but not yet part of the configuration; the primary action `Add selected ports (N)` is disabled at N = 0. Secondary: `N other results – not confirmed open` is a collapsed disclosure with closed and unclear states (`closed`, `filtered`, `open|filtered`, `closed|filtered`, `unfiltered`, unknown) as the same compact rows, unselected, with one shared note above them. Each row keeps its Nmap word, without icon (S5, DC-EX-STATE-2); `Closed` is confirmed closed and MUST NOT be called uncertain; unclear states carry the dashed `ju-state-badge--unknown` edge.
- **DC-PICK-5** Unclear and closed ports MUST NOT be selected automatically but can be ticked by hand. Without a confirmed open port the page says that Nmap did not confirm any port as open; it MUST NOT claim the scanned ports are closed. A service from the port table reads `name (table hint)` in italic, because a port state confirms no service identity; only a probed service shows name, product and version without the hint. A result state is a port-scan finding, not a monitoring status; the picker MUST NOT write `PORT_UP` or `PORT_DOWN`. All scanner text MUST be set with `textContent`.
- **DC-PICK-6** Adoption: `Add selected ports (N)` merges the ticked rows per protocol into the fields, keeps manual entries, adds no duplicates, updates form and chips at once and stores the job id in the hidden `network_scan_id` field (the server derives service names from that job only). Invalid existing field text blocks the merge with a message.
- **DC-PICK-7** Starting a scan, `Cancel scan`, a changed host (compared with the scanned host) and every failed or empty outcome MUST hide the result area and clear the job id; the old rows are removed from the DOM, so results of an earlier scan or host are never visible, tabbable or adoptable. Ports already adopted stay in the form. A host change while a job runs also deletes that job. The picker's own `display` rules MUST NOT depend on Bootstrap's `[hidden] { display: none !important; }` being absent.
- **DC-PICK-8** Keyboard: Enter in the manual input adds and keeps focus there; Tab reaches the chip remove buttons. Result rows are native checkboxes toggled with Space; both disclosures are native `<details>` opened with Enter or Space.
- **DC-PICK-9** Layout: nothing in the picker is a scroll container and none uses a fixed height; a long expanded list or chip list is reached through the page or wizard-step scroll. Result rows wrap, rows and the select checkbox meet `--ui-target-min`, and nothing overflows at 320 px or effective 200 % zoom.
- **DC-PICK-10** Checks: `tests/js/port-scan-core.test.mjs`, `tests/layout/port-picker.spec.mjs` (`@portpicker`, Chromium and WebKit, wizard and editor, light and dark, 1280x720, 390x844, 320x640), `tests/layout/wizard.spec.mjs`, `tests/test_port_picker_markup.py`.

### Endpoint state (dashboard and target detail)

A target's network endpoints are built once, by `build_endpoint_views` (`app/utils/endpoint_view.py`), for the check dots of the detail page, the targets table and nothing else.

- **DC-ENDPT-1** The list MUST be ICMP first and apart, then every configured TCP port, then every configured UDP port, each identified by protocol and number (`TCP 53` and `UDP 53` are two endpoints). A configured port without a measurement is listed as UNKNOWN, never missing. A disabled check or a removed port is not listed.
- **DC-ENDPT-2** State dot: `fragments/_state_dot.html` (`state_dot`) renders UP, DOWN and UNKNOWN as `.ju-status-item` with `.ju-status-dot` (DC-STATE-2: `Up` success circle, `Down` danger square, `Unknown` neutral hollow ring). The word is always present (visible, or `visually-hidden` after a label such as `TCP:587`). UNKNOWN means "no confirmed result" (scan failure, uncertain UDP state, stale result, no measurement yet); it MUST NOT be shown as DOWN or counted as an outage.
- **DC-ENDPT-3** On the target detail page the Checks hero MUST be the only place that shows the endpoint dots (the Targets list shows its own preview, DC-TLIST-2): one `state_dot` per enabled `HTTP` and `PING` check and per configured port (`TCP:25`, `UDP:53`, ...), plus a `CERT` dot (`cert_dot`) with the certificate status (ok, warning, critical, expired, unknown) when certificate monitoring is on, wrapping onto as many rows as needed. There is no endpoint table or card. The Nmap note of a UDP state is the dot item's tooltip; detected service and exact check times stay in the data (`network_ports` in the API).
- **DC-ENDPT-4** The check dots of the detail page and of the targets table MUST read the same list, so they cannot disagree with each other or with `network_ports` in the API. An UNKNOWN overall status does not hide the checks that were measured: HTTP and ICMP keep their own state.
- **DC-ENDPT-5** Overall status (scheduler rule): DOWN if an enabled endpoint is confirmed down, otherwise UNKNOWN if one is unknown, otherwise UP. The dashboard counts Online and Offline only for UP and DOWN; an UNKNOWN target is only in Total and shows the `help` icon (title and accessible name `Unknown`, a different shape from the `cancel` icon of DOWN) in the Status column (S2, DC-EX-STATE-1).
- **DC-ENDPT-6** Checks: `tests/test_endpoint_view.py`, `tests/layout/endpoint-state.spec.mjs` (`@endpoint-state`, Chromium and WebKit, light and dark, 1280x720, 390x844, 320x640).

### Downtime history: three kinds

- **DC-DOWN-1** The card header MUST be `Downtimes` plus the range (`30d`, `Selected Range`) as dim text on one line, with no second explanatory line. Below it one figure row `N Downtimes   M Data gaps` (plus `Ongoing` in the error colour while a downtime is open). `Data gaps` counts `gap` and `unknown` together and MUST NOT be added to downtimes. Only the event list scrolls (`.downtime-scroll`, one registered region, scrollbar only when needed); the figures stay visible.
- **DC-DOWN-2** The interface MUST NOT explain technical terms in running text, with an info icon, an explanatory paragraph or a tooltip; explanations live in `docs/features/monitoring.md` (Downtimes and data gaps). Definitions: a downtime is a confirmed outage; a data gap is time without measurements or with an inconclusive result and counts neither as downtime nor as uptime. No sentence may call a gap or unknown period uptime.
- **DC-DOWN-3** Event kinds (terms from `app/utils/downtime_view.py`; markers are the exception DC-EX-DOWNTIME-1): `Downtime` (confirmed, filled circle in `--td-bad`), `No measurements` + `Not downtime` (gap longer than three check intervals, dashed neutral circle) and `Unknown` + `Result inconclusive` (explicit UNKNOWN result, neutral diamond). The shapes differ without colour.
- **DC-DOWN-4** An event shows at most three lines: `kind ... duration` / `4 Nov · 18:57–21:50` / `HTTP · TCP:443 · note`. A wide card (container query, 30 rem) puts the range beside the kind; a very narrow card (15 rem) drops the duration under the kind. Layout follows the card width (`container: downtime`), the duration uses `tabular-nums`, with no absolute positioning and no fixed row heights.
- **DC-DOWN-5** Dates: same day `4 Nov · 18:57–21:50`; across midnight `4 Nov 23:50 → 5 Nov 00:15`; open `4 Nov · since 13:06`; the year only outside the current year or across New Year; seconds hidden. English month names, 24 h clock, no locale formatting. The exact timestamps with seconds are in the `title` and in visually hidden text. The server (UTC) and `utils/downtime-time.js` (local or UTC by the dashboard setting) MUST apply the same rules.
- **DC-DOWN-6** The duration MUST be derived from the displayed whole seconds (`display_seconds`), so it equals the difference of the shown times (stored instants carry scheduler jitter). Format: `45s`, `4m 30s`, `14m`, `2h 53m`, `1d 3h`. The downtime calculation itself is unchanged.
- **DC-DOWN-7** Empty states are compact text without illustration: valid data without downtimes `0 Downtimes` / `No outages in selected range`; only gaps `0 Downtimes` plus the gaps, no success claim; no measurements `No measurement data` / `No checks recorded in this range` without a downtime figure; the error state uses the same compact form.
- **DC-DOWN-8** The periods come from `query_downtime_periods` and `query_unmeasured_periods`, which share one gap rule (`availability_rules`), so an interval is never listed twice and never silently counted as either.
- **DC-DOWN-9** Checks: `tests/test_downtime_view.py`, `tests/js/downtime-time.test.mjs`, `tests/test_downtime_periods.py`, `tests/layout/endpoint-state.spec.mjs`, `tests/layout/downtime-history.spec.mjs`.

### Downtime overview: row link and time range

- **DC-DLINK-1** Each row of the dashboard widget "Downtime overview (24h)" MUST be one link to the target page (`/ui/targets/<id>?range=24h`). The target-name anchor is stretched over the row (`stretched-link`, `position: relative` on the row), so a click or tap anywhere in the row, Enter on the focused link, and middle or Ctrl click all work without a script handler. The focus ring is drawn around the whole row (variant V3, DC-FOCUS-6).
- **DC-DLINK-2** The target page MUST apply the range carried by the link before the first chart load (`?range=<preset>` or `?from=<ISO UTC>&to=<ISO UTC>`, at most 730 days, `from` before `to`). A missing or invalid value gives the usual last 24 hours without a message. The parameters are removed from the address bar after they are applied, so a reload or a later range change is not overridden.
- **DC-DLINK-3** Check: `tests/layout/downtime-widget-link.spec.mjs`.

## 10 Regression gates, change procedure and references

### 10.1 Regression gates

- **DC-GATE-1** A change to a shared rule or component MUST update this contract and its owning check in the same change (DC-OWN-2). A page-specific behaviour MUST be tested in the existing module that owns the contract; a test that only repeats implementation details MUST NOT be added.
- **DC-GATE-2** A change to a component in the left column MUST run at least the checks in the right column for the affected surface, in addition to the checks the change reaches through its own code path. Paths are under `tests/`.

| Changed component | Minimum checks |
| --- | --- |
| Tokens, palette, shadows, fonts (`gentelella-layout.css`, `ui.css` `:root`) | `test_ui_accessibility.py`, `test_status_dot_parity.py`, `layout/focus-style.spec.mjs` (`--focus`), `layout/load-stability.spec.mjs` |
| Shell, viewport, scroll regions, sidebar, drawer | Shell audit `layout/audit.js` (`@layout`, `@audit-self-test`), `layout/sidebar-transition.spec.mjs`, `layout/sidebar-menu-visibility.spec.mjs`, `test_ui_accessibility.py` |
| Focus rule group | `layout/focus-style.spec.mjs`, `test_ui_accessibility.py` |
| Status dot, badge, target state | `test_status_dot_parity.py`, `test_status_badge_no_icon.py`, `test_endpoint_view.py`, `test_unknown_status_display.py`, `layout/endpoint-state.spec.mjs`, `layout/notifications.spec.mjs`, `js/quality-state.test.mjs` |
| Palette adapters (PDF, e-mail) | `test_status_palette.py`, `test_email_template.py` |
| Cards | `layout/card-corners.spec.mjs`, `layout/card-overlays.spec.mjs`, `test_ui_accessibility.py`, `layout/public-egress.spec.mjs` |
| Tabs | `layout/tab-style.spec.mjs`, `layout/settings-tab-press.spec.mjs`, `layout/edit-target-tabs.spec.mjs`, `js/settings-tabs.test.mjs` |
| Modals | `layout/modal.spec.mjs` (`@modal`), `test_modal_dismissal.py`, `test_ui_accessibility.py`, `layout/reset-confirm.spec.mjs`, `js/confirm-via-modal.test.mjs`, `js/htmx-confirm.test.mjs` |
| Forms, field errors, optional groups | `test_ui_accessibility.py`, `test_port_picker_markup.py`, `layout/wizard.spec.mjs`, `layout/port-picker.spec.mjs`, `layout/smtp-link-field.spec.mjs`, `layout/recipient-channels.spec.mjs`, `js/optional-groups.test.mjs`, `js/check-groups.test.mjs` |
| Charts | `js/availability-chart-clip.test.mjs`, `js/chart-series-colors.test.mjs`, `js/combined-chart-data.test.mjs`, `layout/availability-clip.spec.mjs`, `layout/combined-clip.spec.mjs`, `layout/combined-legend.spec.mjs`, `layout/combined-zoom-reset.spec.mjs`, `layout/combined-note.spec.mjs` |
| Live update, list search | `layout/targets-live-update.spec.mjs`, `layout/targets-search.spec.mjs`, `test_targets_search.py`, `js/list-filter.test.mjs`, `layout/recipients-search.spec.mjs` |
| Dashboard widgets and grid | `node tests/layout/run.mjs --gridstack`, `layout/dashboard-metric-labels.spec.mjs`, `test_dashboard_layout.py`, `js/dashboard-widgets.test.mjs` |
| Notification list and popover | `layout/notifications.spec.mjs`, `layout/notif-dropdown.spec.mjs`, `test_notifications.py` |
| Traceroute map | The checks of DC-TRACE-8 |
| Target wizard, editor, port picker | The checks of DC-WIZ-6, DC-EDNAV-4, DC-PICK-10 |
| Settings surfaces (Delivery queue, Public outbound IPs, hostname) | The checks of DC-SPOOL-7, DC-EGRESS-11, DC-SETF-3 |
| Downtime history and overview | The checks of DC-DOWN-9, DC-DLINK-3 |

- **DC-GATE-3** For a change to layout, themes, overlays or responsive behaviour the applicable Chromium and WebKit desktop and phone cases MUST be run (1.2, DC-RESP). CI runs the shell matrix and the tagged UI regressions in Chromium and WebKit (`.github/workflows/ci.yml`). Firefox and the full profile are local only. A check that is unavailable MUST be reported as a gap, not treated as passed (10.3).
- **DC-GATE-4** The browser audit MUST fail on every console error. The one exception is the engine notice `Blocked script execution in 'about:blank'|'about:srcdoc' because the document's frame is sandboxed ...`, which Playwright's trace snapshots provoke in sandboxed frames in Chromium and WebKit. It is matched exactly and applies only to pages that declare `sandboxedFrames` in `tests/layout/harness.mjs`. The audit self-test and `tests/test_ui_accessibility.py` enforce both rules.
- **DC-GATE-5** A test MUST NOT add `waitForTimeout` or a raised timeout to turn a run green. A fixed wait is replaced by `expect.poll` or `waitForFunction` on a state signal when the spec is touched; a deliberate measurement window carries a one-line reason. A tolerance MUST NOT be widened to get green; a change of a tolerance is a change of the owning test with a reason (DC-NORM-4).

### 10.2 Execution prerequisites

- **DC-RUN-1** `AGENTS.md` and `tools/run-limited.sh` exist in the repository at the state this document describes (with `tools/test-budget.mjs` and `tests/test_run_limited.py`). They are absent on upstream `main` at tag v1.0.2, which predates this document. `AGENTS.md` is the only owner of the wrapper rules; this contract MUST NOT restate its sizing, slot or override rules.
- **DC-RUN-2** A heavy local run (Playwright, `tests/layout/run.mjs`, `npm run test:layout`, a complete `pytest` or Node run) MUST go through `tools/run-limited.sh`, for example `tools/run-limited.sh node tests/layout/run.mjs --profile quick --browsers chromium`. Only GitHub Actions runs these commands directly. Affected specs run first; a full browser matrix runs after the changes are merged together or as a justified final check.
- **DC-RUN-3** The layout suite uses the Playwright install of the repository and the configuration `tests/layout/playwright.config.mjs`, always passed with `--config`. A selection is checked with `--list --grep '<tag>'` before it runs. Tags are the `--grep` strings of `ci.yml`; `node tests/layout/run.mjs --help` lists profiles. `--focus` is the CI route for the `@focus` and `@targets-search` specs and `--gridstack` for the grid audit.
- **DC-RUN-4** Disposable output (screenshots, traces, reports) MUST be written outside the repository.

### 10.3 Evidence levels

| Level | Meaning |
| --- | --- |
| E-UNIT | pytest and Node unit and markup tests. |
| E-ENGINE | Desktop or emulated-device Playwright run (DC-MEAS): Chromium and WebKit are the CI gates, Firefox runs locally by project policy. |
| E-IOS | A run on a real iPhone or iPad, or in the iOS Simulator. Playwright WebKit is not E-IOS (1.2). |
| E-A11Y | A manual pass with assistive technology (screen reader, keyboard-only, zoom and text size in the browser). |

- **DC-EVID-1** A report MUST name, per claim, the level, the configuration (engine, CSS viewport, effective zoom or text enlargement, theme, input) and the result. A check that was not run MUST be reported as "not run", with the reason. A skipped, unavailable or partly run check MUST NOT be reported as passed.
- **DC-EVID-2** E-IOS and E-A11Y are manual levels. Unless a record with device or tool, OS, browser version and date exists, the result for them is "not run", and the behaviour is "unverified" (DC-MEAS-3, DC-VP-7). E-ENGINE results MUST NOT close an iOS-specific or assistive-technology claim.
- **DC-EVID-3** A test name or label MUST state what its configuration proves (DC-MEAS-1). A claim whose test does not exist is an open requirement (10.6), not a covered rule.

### 10.4 Change procedure

- **DC-CHG-1** To change a rule: find it by ID, determine its level (DC-PREC-1), change the owner of the topic (4.2), update this document and the owning check in the same change, and add an entry to the change log (A.2). A level-1 rule changes only by an explicit product decision stated in the change (DC-PREC-3).
- **DC-CHG-2** To add a rule: use the next free number of its area; an ID is never reused (DC-NORM-2). New requirements carry MUST, SHOULD or MAY (DC-NORM-1) and name their check or state why none is possible.
- **DC-CHG-3** To add an exception: add the row next to the rule it modifies with scope, rationale, owner and an existing test (DC-EXC-1), and list it in the exception index of part 9.
- **DC-CHG-4** To remove or weaken a rule: state the reason in the change log and remove or adjust its check in the same change. A rule MUST NOT be dropped because it is inconvenient to implement.
- **DC-CHG-5** History, one-off measurements and incident descriptions go to the appendix, not into a rule (DC-NORM-1).
- **DC-CHG-6** Who owns what: this document owns the requirements; the owner table of 4.2 names, per topic, the CSS, script, template and test owner; the feature contracts of part 9 own their surface; `AGENTS.md` owns the execution of tests (DC-RUN-1); the status of a requirement against the implementation is recorded in 10.6.

### 10.5 Implementation constraints

- **DC-IMPL-1** The application MUST use server-rendered Jinja templates, Bootstrap 5.3, local CSS, fonts and icons, and a `data-bs-theme` attribute set before paint. A new dependency or external runtime asset MUST be avoided (DC-PD-2).
- **DC-IMPL-2** A change MUST preserve widget identities, monitoring behaviour, API contracts, user layout intent, keyboard interactions and existing dialog and form IDs and confirmation safeguards. Internal widget markup, shared components, drag-handle placement and GridStack sizing MAY be redesigned to meet the contract; existing markup or sizing code that conflicts with it is not a preservation constraint.
- **DC-IMPL-3** The only active dashboard layout contract is V2: `version = 2` with a required `height_mode` (`auto` or `manual`) on every widget. The API, the frontend and the current persistence MUST accept only V2 JSON; V1 JSON is rejected and never reinterpreted. Historical SQLite schema migrations MAY convert stored rows once during an upgrade but MUST NOT add runtime V1 support.
- **DC-IMPL-4** Frames that show user-defined HTML (SMTP template editor and preview) MUST be sandboxed without `allow-scripts`. The sandbox is a security boundary (DC-PD-7) and MUST NOT be loosened to silence a console message or a test finding.

### 10.6 References and open requirements

- **DC-REF-1** Every file path, test and helper named in this document MUST exist in the repository, or be listed in the table below as an open requirement. A referenced section name or rule ID MUST resolve. The scripted check of the consolidation (report, section 5) verifies this; code comments reference rule IDs, not heading names.
- **DC-REF-2** A difference between this document and the implementation is classified as (1) a requirement still to implement, (2) a wrong or outdated reference to correct, (3) a rule no longer needed, or (4) a documented exception. Class 1 is listed below with the finding ID and removed from the list when implemented and verified; class 2 and 3 are corrected in the document; class 4 has an exception row (part 9).
- **DC-REF-3** `font-display`: the contract value for IBM Plex Sans is `fallback` (DC-TOK-8). The checked-out branch already sets it in `gentelella-layout.css`; upstream `main` at tag v1.0.2 still sets `optional` and takes the change with the branch. The icon font uses `block` and is outside DC-TOK-8.

Open implementation requirements (informative; status record, not a rule):

| Req | Finding | File(s) | Change |
| --- | --- | --- | --- |
| OR-1 | F-01, F-19 | `tests/layout/focus-style.spec.mjs`, `traceroute-map.spec.mjs`, `notifications.spec.mjs` | Done (Chromium, light and dark): the tag input and the type card (V4), the traceroute hop marker (V2) and the notification row (V3) on the real pages read the shared tokens; DC-FOCUS-5 (selected and focused) for the type card and the active tab. The token guard and the range and table-viewport (V3) cases were already in place. |
| OR-14 | F-22, F-23 | `tests/layout/recipient-channels.spec.mjs`, `recipients-search.spec.mjs`, `spooler-log.spec.mjs`, `targets-search.spec.mjs`, `harness.mjs`, `edit-target-tabs.spec.mjs` | Labels "200 % zoom" become "effective 200 % zoom" or are removed where only the viewport or only the DPR changes; one shared effective-zoom helper; a text-enlargement profile (DC-MEAS-2, T-01 to T-04). Done in B10: helpers in `test-support.mjs`, labels renamed, `targets-search.spec.mjs` runs reflow at 320 CSS px and text enlargement; the shell audit profile is opt-in and fails on most pages (see DC-MEAS-2), so the audit result stays open. The modal checks run the profile (`modal.spec.mjs`); 390x844 is open (DC-MEAS-2). |
| OR-16 | DC-WARN | `css/ui.css` (`.ju-warning`), `utils/warning.js`, `fragments/_warning.html`; warning messages migrated in templates/scripts | Component and migrations implemented 2026-11-12 (`4a7bc78`). DC-WARN-4 markup/helper coverage was not added or run; tests were administratively treated as complete per user instruction, with no pass claim. |
| OR-17 | F-25, F-27 | `css/ui.css`, `tests/test_ui_accessibility.py`, `tests/layout/contract-invariants.spec.mjs` | Elevation test T-27 in place: three levels with aliases (`--ui-modal-shadow`, `--td-shadow`), no literal blur shadow, cards and dialogs draw the tokens in both themes. Cards that carry Bootstrap's `shadow-sm` are mapped to `--ju-shadow` in `ui.css`; dead CSS (`.x_panel`, `.ju-modal--wide`) removed. The reduced-motion guard of the navigation pulse is in place (DC-TOK-6). |
| OR-18 | T-23 | `tests/layout/edit-target-save.spec.mjs`, `network-diagnostics-layout.spec.mjs`, `reset-confirm.spec.mjs`, `spooler-log.spec.mjs`, `load-stability.spec.mjs` | Fixed waits without a recorded reason become state-signal waits (DC-GATE-5). |
| OR-19 | F-34 | `js/settings.storage.js`, `js/settings.core.js` | A persistent field error plus toast for a failed autosave (DC-FORM-12, last row); scheduled after the Settings task that edits these files. |
| OR-20 | R-08 | `js/utils/field-errors.js`, `tests/layout/smtp-link-field.spec.mjs`, `card-corners.spec.mjs`, `card-overlays.spec.mjs`, `public-egress.spec.mjs`, `tests/test_ui_accessibility.py`, `templates/fragments/network_scan_picker.html`, `css/ui.css` | Code comments name the old headings "Validation and error display", "Cards and panels" and "Cards are not collapsible"; they cite rule IDs instead. |
| OR-22 | F-14 | `tests/layout/endpoint-state.spec.mjs` | The exception test of DC-EX-DOWNTIME-1 asserts the exception itself (distinct marker shapes of the three event kinds), so the row is proven (DC-EXC-1). The DC-EX-CHART-1 dash assertion is in place. |

## Appendix A Rationale and change log

Informative. This appendix holds history and reasons; it adds no requirement (DC-NORM-1).

### A.1 Rationale

- Visual reference: the WireBuddy documentation site (Material for MkDocs, indigo header, compact two-row navigation, white reading surface, automatic light and slate theme) was viewed at 1440 px in Chromium. No approved justUp! mockup existed; the justUp! teal palette stays authoritative.
- Viewport: a viewport-high shell keeps the top bar and footer visible. On mobile browsers `100dvh` does not cover the on-screen keyboard or a collapsing toolbar, which is why DC-VP-2 to DC-VP-6 are declared special cases and iOS has its own evidence level.
- Scroll ownership: nested scroll regions existed before the registry (wizard landscape fallback, widget bodies, recipient picker, Signal table). DC-SCROLL-2 registers them instead of banning them.
- Elevation: earlier text named two shadow tokens; the stylesheets also use `--ui-shadow-subtle` and `--ui-modal-shadow`. DC-TOK-5 records the three levels in use.
- Focus: an earlier Notifications rule asked for a primary-coloured inset outline, while the global rule forbids primary-coloured focus and the stylesheet already used the neutral colour with an inset offset. V3 records that variant.
- Measurement terms: some specs labelled a halved viewport at DPR 1, or DPR 2 alone, as "200 % zoom". DC-MEAS separates the terms.
- Status components: the earlier text called the list of remaining badges exhaustive, put the Delivery queue state both in a dot and a header badge, and asked Notifications for a severity badge while the template rendered a dot. DC-STATE-1 replaces it by one component per state kind. Status badges never carry an icon because a glyph would repeat what the colour and the word already say.
- Tabs: Notifications used `nav-pills` with the accent scrollbar while Settings, Target detail and Edit Target used `nav-tabs` with three page rules and hidden scrollbars; the Notifications look became binding at the user's request. No "More" control exists because the clipped last tab was the overflow and the strip is the single keyboard-operable control. The earlier line "Tab reaches every tab" held only for the link strip; real tabs use roving tabindex.
- Modals: three variants defined meaning and width together although a wide size was announced; `confirm` and `critical` shared one width and `.ju-modal--wide` had no consumer. Intent and size are now separate axes.
- Field errors: five mechanisms existed (`JuFieldErrors`, wizard code, port picker code, native `setCustomValidity`, toast only). The wizard and the port picker asked for `role="alert"` on each field error, which announces every field as an interruption; the shared pattern ties the error to the control instead.
- Charts: `clip: 0` was described as switching clipping off; in Chart.js it clips at the chart area. Series were told apart by colour only while colour must never be the only cue. The unused `lap` of the series slots was meant to shift a dash pattern and now shifts the marker shape.
- Live updates: commit 897d4b59 restored the `<main>` scroll position after every swap of the Targets fragment, which hid the full replacement of an unchanged list. The no-swap path of DC-LIVE-1 is the default; the restore remains only for structural swaps.
- Fonts: with `font-display: optional` WebKit failed to load the late variable font with a `NetworkError` in about half of the parallel cold loads, so `system-ui` stayed in use and the 320x640 spooler title wrapped to two lines; with `fallback` 24 of 24 cold loads kept Plex. WebKit paints the fallback font until Plex arrives: on a cold load the title row moved by up to 32 px and the top bar by 17 px, while Chromium never swapped. No second font is defined.

### A.2 Change log

- 2026-11-08: restructured into ten parts with rule IDs; added precedence and exception procedure, token and owner rules, scroll-ownership registry, viewport special cases, the focus policy and the warning-sign decision. Parts 9 and 10 were consolidated afterwards (below).
- 2026-11-08: parts 6 to 8 consolidated: state taxonomy with exception rows, badge allowlist, one field-error pattern, modal intent and size axes with a dismissal policy, tabs split into real tabs and link strip, card invariants, chart clipping and marker-shape differentiator, measurement claims per test configuration, live-update transaction rules. Check-state, Delivery queue and channel-status paragraphs moved to part 9.

- 2026-11-08: parts 9 and 10 consolidated: feature contracts as rule IDs (DC-TLIST, DC-TLIVE, DC-SPOOL, DC-RCPT, DC-EGRESS, DC-SETF, DC-NOTIF, DC-DASH, DC-GRID, DC-TD, DC-TRACE, DC-WIZ, DC-EDNAV, DC-PICK, DC-ENDPT, DC-DOWN, DC-DLINK) with the exception DC-EX-DOWNTIME-1; the two Notification list texts merged into one; the Targets live-update case table; regression gates with a component-to-check matrix, execution prerequisites, evidence levels, change procedure and the open implementation requirements; pixel arithmetic moved to A.4; wrong test id of the grid audit corrected.

### A.3 Earlier release decisions

#### Decisions for 1.0.2

The product decisions for this release are:

- Keep the existing sidebar navigation. Replacing it with horizontal tabs would change the established navigation model and needs a separate design review; it is outside this release.
- Keep the locally bundled IBM Plex Sans in the application. The docs site's Roboto does not justify adding or switching an application font in this release.

### A.4 Measurement records

Informative records behind requirements of part 9. They are snapshots; the requirement is the rule, not the number.

- Delivery queue height (DC-DASH-7): at 1280 x 720 with the compact metric labels the natural card measured 125 px, the GridStack frame 20 px and the reserved edit-handle band 24 px, 169 px in total. Two rows give 144 px at the 72 px cell height, so the minimum stayed three rows. The previous four-number rendering came from a visible `pending + processing` value before the three metrics, described only in `aria-label` and `title`; the three-row default and minimum then kept automatic sizing from reclaiming the space after the redundant row was removed.
- Delivery queue width (DC-DASH-7): with a minimum of 3 of 12 columns the metrics container was 7 rem and needed six stacked rows; at 4 columns it measured 10.9 rem (992 px) to 11.5 rem (1024 px), so the stacking container query starts below 10 rem.
- Targets scroll restore (DC-TLIVE-1): commit 897d4b59 restored the `<main>` scroll position after every swap of the Targets fragment, which hid the replacement of an unchanged list.
