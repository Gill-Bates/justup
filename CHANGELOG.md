## [1.1.15] - 2026-02-xx
- ``Fix`` Improved spooler processing for notifications
- ``Fix`` Correction of downtime calculation
- ``Fix`` Signal CLI blocked the front end due to an error


<details markdown="1">
<summary>Previous versions...</summary>

## [1.1.14] - 2026-02-25

- ``New`` Alarms can now be closed in bulk
- ``New`` The notification log now also shows the target affected by the alert
- ``New`` Updating the Signal Message Template
- ``New`` The uptime for the last 30 days is now also displayed in the dashboard
- ``Fix`` Update package ``uvicorn`` and ``fastapi`` to the lastest version
- ``Fix`` And as always: several CSS Design improvements

## [1.1.13] - 2026-02-16

- ``New`` Introduction of a global message spooler for alarm messages
- ``New`` Addinng Button to flush Notification logs
- ``New`` The sampling rate in the graphs now dynamically adjusts to the screen size for better readability
- ``Fix`` Update Python dependencies to latest version
- ``Fix`` And as always: several CSS Design improvements

## [1.1.12] - 2026-02-12

- ``New`` Alerts can now be automatically closed per target if necessary
- ``Fix`` Uptime calculations were based on only 10 data points instead of the last 24 hours
- ``Fix`` In rare cases, the target page may need to be reloaded in order to display graphs
- ``Fix`` Alarm messages were not sent; instead, the error ``object has no attribute 'close'`` appeared

## [1.1.11] - 2026-02-11

- ``Fix`` Fixing a memory exception while interpolating the traceroute
- ``Fix`` Sending a test message with Signal blocked the front end for 15 seconds
- ``Fix`` The legends in the availability overview were not functioning correctly

## [1.1.10] - 2026-02-10

- ``New`` Deleting the metrics of a target now also deletes all associated alarms.
- ``New`` The Availability graph now shows downtime by affected service with separate lines for PING, HTTP, and TCP (eliminates phantom downtimes from aggregated status)
- ``New`` Changes to the settings are now confirmed by a toast notification
- ``New`` Smaller Docker image by removing Apps like build tools, gnupg, busybox
- ``Fix`` In the PDF report, the columns in the "Network Path" section were empty
- ``Fix`` This release contains a security and stability issues
- ``Fix`` URLs for a target were constructed incorrectly when an additional path was specified
- ``Fix`` The downtime overview accumulated all failures of all services per target, which led to incorrect results

## [1.1.9] - 2026-02-07

- ``New`` Browser sessions are now automatically terminated after inactivity (sliding session)
- ``New`` CLI tool for admin tasks: ``docker exec -it <container_name> justup-cli reset password admin``
- ``New`` Active user sessions are automatically revoked when a user is disabled
- ``New`` Introducing an official Logo 🎉
- ``New`` The performance graph for TCP ports is unified again in the web view, but separate for each port in the PDF for better visualization
- ``New`` The Downtimes tile in the dashboard now shows the reason for the outage
- ``New`` Upgrade to ``Python 3.13``
- ``New`` Upgrade ``fastapi v0.128.0`` to ``fastapi v0.128.2``
- ``Fix`` TDSB database engine designed for continuous operation with enhanced robustness
- ``Fix`` And as always: several CSS Design improvements
- ``Fix`` Fixing a memory leak in database processing
- ``Fix`` A successful check of a monitored service of a target reset the alert window of another service. This could prevent an alert from being triggered
- ``Fix`` Hardening of the internal crypto engine (secret and token handling)

## [1.1.8] - 2026-02-05

- ``New`` The IP address of the last login is now displayed in the user view
- ``New`` Improved signal receiver validation
- ``New`` Adding Option to flush Signal Notification Queue
- ``Fix`` Layout of action buttons standardized

## [1.1.7] - 2026-02-04

- ``New`` Scheduler idempotency: Run-locking prevents duplicate alerts on restart
- ``New`` Downtime debouncing: 60-second minimum duration filters transient packet loss
- ``New`` Signal rate-limiting: Automatic 1-hour cooldown for rate-limited notifications
- ``New`` PDF serialization: Thread-safe matplotlib rendering with global lock
- ``New`` Settings atomic save: Single transaction wraps all setting updates
- ``New`` Fragment rate limits: 10-60 req/min on HTMX endpoints
- ``New`` Atomic notification claim: BEGIN IMMEDIATE prevents worker race conditions
- ``Fix`` Spooler exception handling: Type-specific retry strategies now work correctly
- ``Fix`` Alert state restoration: Handles both string and datetime types from SQLite
- ``Fix`` Config validation: Removed noisy warnings for optional features

## [1.1.6] - 2026-02-04

- ``New`` Introducting Multi-Arch Support (``arm64`` and ``amd64``)
- ``New`` Send a test alert to all recipients per target
- ``New`` Multiple TCP ports can be defined per target as comma-separated values and monitored
- ``New`` Implemented a migration framework for database/schema adjustments for future version updates
- ``New`` Mobile-friendly HTML email template with dark mode support
- ``New`` Multiple recipients for an email notification are now sent as BCC in a single email
- ``New`` Automatic verification whether a recipient’s phone number is registered on the Signal network
- ``New`` Improved User Management: Change Password, Enable/Disable User, Show Last Login
- ``Fix`` Fixed a 500 SQL error in the user query
- ``Fix`` Several frontend design fixes
- ``Fix`` Saving the HTML email template did not trigger a toast notification
- ``Fix`` Email notifications always displayed the status as "UNKNOWN"
- ``Fix`` Under Advanced Settings, a linked Signal account was shown even though no connection existed
- ``Fix`` If no SLA is defined for a target, the email notification now shows "N/A"
- ``Fix`` Major JavaScript updates under the hood to improve frontend behavior

## [1.1.5] - 2026-02-01

- ``New`` Include TLS details in PDF reports using SSLyze
- ``Fix`` More reliably display enabled modules in the About section
- ``Fix`` Prevent a backend 500 error and SQL exception when opening Signal settings
- ``Fix`` Restore email alert delivery
- ``Fix`` Show a toast notification when deleting a target
- ``Fix`` Ensure Signal messages are delivered during downtime
- ``Fix`` Several CSS Design-Fixes in the Frontend

## [1.1.4] - 2026-01-31

- ``Fix`` Alert recipients can now be selected per target (instead of per user)
- ``Fix`` The page title was missing in the PDF report when the WhatWeb scan failed
- ``Fix`` Typo fixes in the PDF report
- ``Fix`` More robust WhatWeb target analysis with improved error-case logging
- ``Fix`` The “Cert” badge was shown in the target overview even when the HTTP check was disabled
- ``Fix`` When the TCP check was enabled for a target, the TCP badge was not shown in the target details

## [1.1.3] - 2026-01-30

- E-Mail alert notifications with configurable SMTP and editable templates
- About page including changelog, license, and build information
- Per-target metrics reset on dashboard and target detail pages
- IPv6 support for traceroute
- Integration of a dedicated Signal service
- Complete redesign of the internal PTR database and traceroute logic for significantly more accurate location detection
- Signal Messenger: substantially improved device registration workflow
- Signal Messenger: new phone number registration restricted to mobile devices only (captcha requirement)
- Settings: added automatic save functionality for configuration changes

## [1.1.2] - 2026-01-27

- Replaced standard traceroute with MTR for better handling of ICMP drops (e.g. AWS)
- Added intelligent route interpolation to visualize paths even when hops are blocked
- Integrated Signal device registration and linking via QR code in the frontend
- Improved PDF reports with high-resolution charts (600 DPI), refined layout, and better typography
- Unified percentage formatting across dashboard and reports
- Refined UI responsiveness for tables and KPI cards on mobile devices
- Switched to Material Icons for a cleaner, consistent visual style
- Optimized database connection handling and timeout robustness
- Fixed: Permission issues when restoring backups


## [1.1.1] - 2026-01-20

- Fixed: Basic Auth was enabled in target settings even when disabled during save
- PDF report: Traceroute visualization now uses the full page width
- Frontend: Resolved alerts are now clearly visible in dark mode
- Frontend: "URL or Path" field no longer auto-populated for HTTP checks after page refresh when left empty
- Default retention period for targets changed from 30 to 365 days
- Frontend: Signal recipients overview is now collapsible
- Frontend: Improved rendering of measurement points at low zoom levels
- PDF report: More cities shown in traceroute chart for better orientation

## [1.1.0] - 2026-01-19

- Initial release of the project

</details>
