# Reports (PDF)

justUp! creates a print-friendly PDF report per target (A4 or Letter; set under
Settings > General). On the target page, select the **Range** first (custom dates
work), then start the report.

Reports are created as a background job; the report button shows a spinner meanwhile.
When it finishes, a **PDF ready** (or **PDF failed**) notification appears in the
Notification Center, only for you. In the tab that started the report, the download starts
automatically (once per report). Other tabs and reloads only show the notification; tap its row to
download the file at any time until the report expires.
The download is a plain navigation to the attachment URL (`/ui/reports/jobs/<id>/download`,
`Content-Disposition: attachment`, session cookie). No `blob:` or data URL is ever created, so iOS
Safari offers its download sheet instead of opening a PDF viewer. If the page is in the background
when the report finishes, the download waits until the page is visible again. If the browser blocks
the automatic download, use the notification row. After the report expires, the row shows **Report expired**. Generated
files are removed after they expire; the Compose file keeps them in a volatile `tmpfs`.

## Content

## PDF typography contract

ReportLab PDF text uses the embedded IBM Plex Sans Regular, Bold, Italic, and Bold Italic
fonts. HarfBuzz shaping is mandatory for paragraphs, tables, and direct canvas text; PDF
generation fails with a clear error if the selected font cannot be shaped. Text-width and
wrapping calculations use the same shaped glyph advances as canvas rendering, including
centering and right alignment. Do not use `pdfmetrics.stringWidth()` for report layout.

`uharfbuzz` is pinned in `pyproject.toml` and the hash-checked, wheel-only
`docker/requirements.lock`; production installs do not download fonts or shaping data at
runtime. IBM Plex Sans is checked for missing glyphs before shaping. Material Icons are a
deliberate exception: they are symbol glyphs rather than prose and remain unshaped.

Charts are rasterized separately by Matplotlib's Agg/FreeType text renderer. The PDF's
ReportLab shaping settings do not apply to chart images; the chart configuration selects
the same local IBM Plex Sans family. Changes to PDF typography must keep shaping contract
tests and full-report rendering checks passing, including searchable/extractable text,
font embedding, and layout at the supported paper sizes.

| No. | Chapter | Content |
| --- | --- | --- |
| 1 | Cover | Target, period with time zone, status, availability, average and maximum response, estimated downtime, SLA attainment |
| 2 | Technical summary | Checks, IP, ASN, provider, location, certificate, TLS and security results |
| 3 | Network path | Route map, route diagram and hop table |
| 4 | Availability & performance | Availability timeline, response time, HTTP status codes, ping and TCP latency |
| 5 | Downtime log | Outage events, or a note that there were none |

The chapter opens with a **route map**, followed by the route diagram and the hop table.
The map is drawn as vector graphics inside the report. It needs no network access and no tile
server. Its outlines are the Natural Earth country shapes in `app/static/vendor/geo`
(public domain, see `PROVENANCE.md` there), the same data the web map uses.

- **Hops:** the map plots the same points as the web route map, so "Geolocated route for N of M hops"
  matches the web card. Hops without a usable location (private, no reply) are never drawn.
  Consecutive hops at one place share a marker labelled like `6–8`.
- **Markers:** circle = hop, square `S` = first observed hop (only if it is on the map),
  diamond `D` = destination at its own resolved location. Dashed outline = approximate position,
  with a dashed circle for the accuracy radius when it is known and at most 250 km.
- **Lines:** solid between located hops, dashed where hops were interpolated.
- **Projection:** equirectangular, scaled by the cosine of the centre latitude, at least 3 degrees
  wide. The scale bar is true only near the centre latitude. Routes crossing the antimeridian are not handled.
- **No located hop:** the map is replaced by one line, "No hop could be placed on a map."

Country flags are loaded from flagcdn.com when reachable; the country code always stays as text.
The interactive map is described in [Traceroute & maps](traceroute.md).

## Reading the report

- Missing data appears as `n/a` or as a gap in charts, never as `0`. Without an
  availability measurement the status is **No data**.
- The status depends on availability only: Operational from 99 %, Degraded from
  95 %, Critical below 95 %. It does not depend on the SLA.
- The cover shows the start page title of the website as a subheadline under the
  name, only if the page has one. It comes from the report's WhatWeb scan of the
  configured HTTP check URL; nothing is stored.
- The cover shows how much of the period the availability checks cover. Below
  80 % coverage the availability is shown as an approximation with a note.
- Downtime and the downtime log use the same recorded downtime periods as the dashboard. Time
  without measurements is never counted as downtime. Only if the periods cannot be read does the
  report fall back to the sampled data (failed checks x interval), which is an estimate.
- The SLA table shows only configured targets. Charts without a latency SLA draw a
  default reference line (500 ms response, 100 ms ping), labelled as not an SLA.
- The traceroute (Nmap, the same measurement as the web view) queries at most 30 hops and
  shows one round-trip time per hop, without loss or average columns. If the destination was
  not reached, the report says so; a destination that did not answer is marked **NO REPLY**.
- TLS checks show text labels (PASS / FAIL), **NOT TESTED**, or **INCOMPLETE**, not
  only colors.
- The HTTP status chart counts stored samples; older data may be aggregated.
- Timestamps use the time zone named in the report.
