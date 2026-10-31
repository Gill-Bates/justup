# Reports (PDF)

justUp! generates a print-optimized RGB PDF report per target on the server side.
IBM Plex Sans is embedded in the PDF; Matplotlib charts are rendered at 600 dpi.
The report summarizes status, availability, and context.

On the target detail page, the report uses the time period selected under
**Range**, including custom dates. Select the period before requesting the job.

## Content

The report (A4 or Letter) is a light, print-friendly document: dark blue provides
structure, green stands for positive, yellow for warning, and red for problematic
states. Each chapter starts on a new page (the cover is always page 1); long
chapters span several pages without content being truncated.

| No. | Chapter | Content |
| --- | --- | --- |
| 1 | Cover | Target, report period with time zone, status, availability, average/maximum response, estimated downtime, and SLA attainment if applicable |
| 2 | Technical Summary | Target Information (checks, IP, ASN, provider, location), Certificate, TLS & Security, detected tech stack |
| 3 | Network Path | Origin and destination host, a compact vector route schematic showing the hop order, and the full hop table with status OK / PARTIAL / TIMEOUT / BLOCKED / NO REPLY (destination did not respond) |
| 4 | Availability & Performance | Availability Timeline, Response Time, HTTP status codes, ping and TCP latency |
| 5 | Downtime Log | Event table (start, end, estimated duration, status) or an empty state with no outages |

!!! note "Route schematic, not a world map"
    The PDF report shows the traceroute as a compact **vector route schematic**
    (hop order, origin and destination) followed by the full hop **table**, not
    as a geographic world map. Start and destination are distinguishable without
    relying on color, so the page stays readable in greyscale printing. A hop
    with no reliable location is shown as *position unknown* rather than placed
    somewhere, and a destination that did not answer is marked *no reply*. The
    interactive geographic map exists only in the web UI
    ([Traceroute & Maps](traceroute.md)).

### Presentation of missing and uncertain data

- Missing measurement series appear as `n/a`, never as `0 %` or `0 ms`. Without
  an availability measurement, the status is **No data**, not "Operational".
- Gaps in the charts stay open; they are not zero values.
- Availability never rounds up to 100 % while checks have failed.
- Untested TLS checks carry the neutral **NOT TESTED** badge; an incomplete scan
  is marked as **INCOMPLETE**. Results are additionally labeled as text
  (PASS / FAIL), not only by color.
- All timestamps are in the time zone of the report start (usually UTC) and are
  named on the period, the chart axes, and the table headers.
- The traceroute queries at most 20 hops. If the destination was not confirmed
  within this limit, the chapter states **Route incomplete – 20-hop limit
  reached**; the route is then never shown as complete. If the destination does
  not respond, it carries the **NO REPLY** badge.
- Downtime is an **estimate** and is called *Estimated downtime* or
  *Est. duration* everywhere. The cover metric is failed checks × check interval.
  The durations in the Downtime Log are event spans from the first failed to the
  next successful check. Neither is an exactly measured outage time.
- The status (Operational from 99 %, Degraded from 95 %, **Critical** below 95 %)
  depends only on availability, not on the configured SLA. Without
  measured values it reads **No data**.
- The cover shows a coverage figure (share of the period covered by availability
  checks). Below 80 % coverage the availability is shown only as a rounded
  approximation with a caveat.
- The SLA table on the cover shows only configured targets. If the SLA is active
  but without its own latency limit, the charts draw a reference line at 500 ms
  (response) or 100 ms (ping), labeled as *Default threshold … no SLA set*; it
  does not count as an agreed SLA.
- The "HTTP Status Codes" chart counts stored measurement points (*samples*).
  Older data may be aggregated, so these are not exact single-check counts.

## Asynchronous generation

Reports are generated exclusively as an **asynchronous job**. On the
target detail page you start the job and see progress, download, and
expiry date. There is no synchronous instant download.

```mermaid
graph LR
  Start[Request report] --> Job[report_jobs]
  Job --> Worker[PDF worker]
  Worker --> Done[Download ready]
```

At application start, orphaned jobs (from a previous crash) are marked as failed,
so that no hanging jobs remain.

## Technology

ReportLab and matplotlib (the latter exclusively for the PDF charts) generate
the RGB report directly. A background worker uses thread pools for rendering and
file I/O. Rendering does not block the HTTP event loop. Both page formats use
20-mm margins. Multi-page tables (traceroute, downtime log) repeat their header
row on every subsequent page; charts are generated at print size (full
text width).
