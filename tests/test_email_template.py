#!/usr/bin/env python3
#
# tests/test_email_template.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Default alert email: rendering, optional sections, escaping, the inline-logo MIME
structure and the test-notification sample data.

These cover the durable contracts of the redesigned template:
- the sent message carries a plain-text part and the logo embedded by Content-ID
  (cid:) in a multipart/related body, not an external image link;
- optional sections (SLA, CTA, group) appear only when their data is present and
  never leave an unreplaced placeholder behind;
- values stay HTML-escaped (no injection through target/alert fields);
- the test notification shows a realistic, self-consistent ~15-minute open outage.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services.notifications.email_template import (
    DEFAULT_HTML_TEMPLATE,
    DEFAULT_SUBJECT,
    LOGO_CID,
    LOGO_SRC,
    build_alert_context,
    build_email_message,
    build_test_alert_sample,
    html_to_text,
    render_subject,
    render_template,
    template_to_preview,
)
from app.utils.status_palette import STATUS_PALETTE
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests._support import wcag_contrast

_FIXED_NOW = datetime(2026, 11, 1, 19, 18, tzinfo=UTC)


def _ctx(**alert_over):
    alert = {
        "status": "OPEN",
        "started_at": (_FIXED_NOW - timedelta(minutes=15)).isoformat(),
        "resolved_at": None,
        "reason": "HTTP 503 Service Unavailable",
        "downtime_minutes": 15,
        "sla_threshold": 99.9,
        "sla_actual": 99.7,
        "system_url": "https://justup.example.com",
    }
    alert.update(alert_over)
    target = {"name": "Test Target", "host": "example.com", "group": "Production", "check_type": "HTTP"}
    return build_alert_context(alert, target)


# ─── Rendering: status, placeholders, content ────────────────────────────────


def test_default_template_renders_without_leftover_placeholders():
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx())
    assert "{{" not in html and "}}" not in html
    assert "Service DOWN" in html
    assert "Test Target" in html and "example.com" in html


def test_subject_carries_status_and_target():
    subject = render_template(DEFAULT_SUBJECT, _ctx())
    assert "Test Target" in subject
    assert "Service DOWN" in subject


@pytest.mark.parametrize(
    ("status", "text", "label"),
    [
        ("OPEN", "Service DOWN", "DOWN"),
        ("DOWN", "Service DOWN", "DOWN"),
        ("RESOLVED", "Service RESOLVED", "RESOLVED"),
        ("UP", "Service UP", "UP"),
        ("WARN", "Degraded", "WARNING"),
        ("UNKNOWN", "Unknown", "UNKNOWN"),
    ],
)
def test_status_maps_to_text_and_redundant_label(status, text, label):
    ctx = _ctx(status=status)
    assert ctx["status"]["text"] == text
    assert ctx["status"]["label"] == label
    # Status colour is a valid 6-digit hex (CSS-injection guard in render_template).
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", ctx["status"]["color"])


@pytest.mark.parametrize(
    ("status", "variant"),
    [
        ("OPEN", "danger"),
        ("DOWN", "danger"),
        ("UP", "success"),
        ("RESOLVED", "success"),
        ("WARN", "warning"),
        ("WARNING", "warning"),
        ("UNKNOWN", "neutral"),
        ("unexpected", "neutral"),
    ],
)
def test_email_status_fill_uses_shared_palette_with_legible_foreground(status, variant):
    ctx = _ctx(status=status)
    palette = STATUS_PALETTE[variant]
    assert ctx["status"]["accent_color"] == palette.background
    assert ctx["status"]["foreground_color"] == palette.foreground
    assert ctx["status"]["text_color"] == palette.text
    assert ctx["status"]["color"] == palette.text
    rendered = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    assert rendered.count(f"background-color:{palette.background}") >= 3
    assert f"color:{palette.foreground};background-color:{palette.background}" in rendered
    assert f"color:{palette.text};" in rendered


def test_status_is_conveyed_by_text_not_only_colour():
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx(status="OPEN"))
    # The redundant DOWN pill and the heading both carry the status as text.
    assert "Service DOWN" in html
    assert ">DOWN<" in html


# ─── Optional sections (conditional blocks) ──────────────────────────────────


def test_sla_section_present_when_sla_available():
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx())
    assert "SLA" in html and "Threshold" in html and "99.90%" in html


def test_sla_section_dropped_when_sla_absent():
    ctx = _ctx(sla_threshold=None, sla_actual=None)
    assert ctx["flags"]["sla"] is False
    html = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    assert "Threshold" not in html
    assert "{{" not in html  # dropped block leaves no placeholder behind


def test_cta_button_only_for_valid_instance_url():
    assert _ctx(system_url="https://justup.example.com")["flags"]["cta"] is True
    assert _ctx(system_url="#")["flags"]["cta"] is False
    assert _ctx(system_url="")["flags"]["cta"] is False
    html_no = render_template(DEFAULT_HTML_TEMPLATE, _ctx(system_url="#"))
    assert "Open in justUp!" not in html_no
    html_yes = render_template(DEFAULT_HTML_TEMPLATE, _ctx(system_url="https://justup.example.com"))
    assert "Open in justUp!" in html_yes
    assert "https://justup.example.com" in html_yes


def test_group_row_hidden_for_default_group():
    ctx = _ctx()
    ctx_default = build_alert_context(
        {"status": "OPEN", "started_at": _FIXED_NOW.isoformat(), "reason": "x", "system_url": "#"},
        {"name": "n", "host": "h", "group": "Default", "check_type": "PING"},
    )
    assert ctx["flags"]["group"] is True
    assert ctx_default["flags"]["group"] is False


def test_unknown_conditional_flag_drops_block():
    tpl = "A{{#if nope}}SECRET{{/if nope}}B"
    assert render_template(tpl, {"flags": {"nope": True}}) == "AB"


# ─── Escaping / injection ────────────────────────────────────────────────────


def test_hostile_values_are_html_escaped():
    ctx = build_alert_context(
        {
            "status": "OPEN",
            "started_at": _FIXED_NOW.isoformat(),
            "reason": "<script>alert(1)</script>",
            "system_url": "#",
        },
        {"name": "<b>Pwn</b>", "host": 'a"onmouseover="x', "group": "Production", "check_type": "HTTP"},
    )
    html = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>Pwn</b>" not in html


def _hostile_name_ctx(name):
    return build_alert_context(
        {"status": "OPEN", "started_at": _FIXED_NOW.isoformat(), "reason": "x", "system_url": "#"},
        {"name": name, "host": "example.com", "group": "Production", "check_type": "HTTP"},
    )


def test_subject_header_carries_raw_characters_while_body_stays_escaped():
    name = "Tom & Jerry <b>\"q\" 'x'"
    ctx = _hostile_name_ctx(name)
    subject = render_subject(DEFAULT_SUBJECT, ctx)
    assert name in subject and "&amp;" not in subject
    msg = build_email_message(
        subject=subject,
        html_body=render_template(DEFAULT_HTML_TEMPLATE, ctx),
        from_addr="a@example.com",
        to_addr="b@example.com",
    )
    assert name in str(msg["Subject"])
    assert "<b>Tom" not in render_template(DEFAULT_HTML_TEMPLATE, ctx)


def test_subject_cannot_inject_headers_via_line_breaks():
    subject = render_subject(DEFAULT_SUBJECT, _hostile_name_ctx("A\r\nBcc: evil@example.com"))
    assert "\r" not in subject and "\n" not in subject
    msg = build_email_message(subject=subject, html_body="<p>x</p>", from_addr="a@example.com", to_addr="b@example.com")
    assert msg["Bcc"] is None


def test_status_color_cannot_inject_css():
    # A non-hex status colour falls back to grey instead of being written verbatim.
    rendered = render_template("x{{status.color}}y", {"status": {"color": "red;}body{display:none"}})
    assert rendered == "x#6b7280y"


@pytest.mark.parametrize(
    ("placeholder", "fallback"),
    [("accent_color", "#6c757d"), ("foreground_color", "#ffffff"), ("text_color", "#5b6b7b")],
)
def test_status_badge_colors_cannot_inject_css(placeholder, fallback):
    rendered = render_template(f"x{{{{status.{placeholder}}}}}y", {"status": {placeholder: "red;}body{display:none"}})
    assert rendered == f"x{fallback}y"


# ─── Target Details are metadata: neutral in every mail state ────────────────

_NEUTRAL = STATUS_PALETTE["neutral"]
_CARD_BG = "#f7f9fb"  # background of the Target Details card in the default template


def _target_details_html(status: str, check_type: str = "HTTP") -> str:
    ctx = _ctx(status=status)
    ctx["target"]["check_badges"] = build_alert_context(
        {"status": status}, {"name": "t", "host": "h", "check_type": check_type}
    )["target"]["check_badges"]
    html = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    start = html.index("<!-- TARGET DETAILS -->")
    return html[start : html.index("<!-- SLA -->", start)]


_MAIL_STATES = ["OPEN", "DOWN", "RESOLVED", "UP", "WARNING", "UNKNOWN"]


@pytest.mark.parametrize("status", _MAIL_STATES)
@pytest.mark.parametrize("check_type", ["HTTP", "HTTPS", "PING", "TCP", "DNS", "CERT", "HTTP + PING"])
def test_check_badge_is_neutral_in_every_mail_state(status, check_type):
    section = _target_details_html(status, check_type)
    assert f"background-color:{_NEUTRAL.background};color:{_NEUTRAL.foreground};" in section
    # No status colour (and no per-type colour) anywhere in the card.
    for variant in ("success", "warning", "danger", "info"):
        assert STATUS_PALETTE[variant].background not in section
    for legacy in ("#dbeafe", "#fef9c3", "#16815c"):
        assert legacy not in section


def test_target_details_are_identical_across_states():
    sections = {_target_details_html(s) for s in _MAIL_STATES}
    assert len(sections) == 1


@pytest.mark.parametrize("status", ["OPEN", "RESOLVED", "WARNING"])
def test_status_banner_keeps_its_state_colour(status):
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx(status=status))
    banner = html[: html.index("<!-- TARGET DETAILS -->")]
    variant = {"OPEN": "danger", "RESOLVED": "success", "WARNING": "warning"}[status]
    assert STATUS_PALETTE[variant].background in banner
    assert STATUS_PALETTE[variant].text in banner


def test_neutral_badge_contrast_meets_wcag_limits():
    assert wcag_contrast(_NEUTRAL.foreground, _NEUTRAL.background) >= 4.5  # badge text
    assert wcag_contrast(_NEUTRAL.background, _CARD_BG) >= 3.0  # badge edge against the card
    assert wcag_contrast(_NEUTRAL.text, _CARD_BG) >= 4.5  # card icon/labels


def test_plain_text_part_does_not_depend_on_badge_colour():
    assert render_template("{{target.check_badges}}", _ctx(), mode="plain_text") == "HTTP"


# ─── Inline logo / MIME structure ────────────────────────────────────────────


def _sample_message(**over):
    ctx = _ctx(**over)
    subject = render_template(DEFAULT_SUBJECT, ctx)
    html = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    return build_email_message(
        subject=subject,
        html_body=html,
        text_body="",
        from_addr="alerts@justup.test",
        to_addr="rcpt@example.com",
        message_id="<q1@justup.test>",
    )


def test_rendered_html_references_the_inline_logo_not_an_external_url():
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx())
    assert LOGO_SRC in html  # cid:justup-logo
    assert "raw.githubusercontent.com" not in html
    assert "http://" not in html.replace("http://www.w3.org", "")  # only the XML namespace, no image host


def test_message_is_multipart_alternative_with_text_and_related_html():
    msg = _sample_message()
    assert msg.get_content_type() == "multipart/alternative"
    types = [p.get_content_type() for p in msg.iter_parts()]
    assert types == ["text/plain", "multipart/related"]


def test_logo_is_embedded_by_content_id_inline():
    msg = _sample_message()
    related = msg.get_payload()[1]
    assert related.get_content_type() == "multipart/related"
    images = [p for p in related.iter_parts() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    image = images[0]
    assert image.get("Content-ID") == f"<{LOGO_CID}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_payload(decode=True)[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes


def test_message_has_a_non_empty_plain_text_part():
    msg = _sample_message()
    text = next(p for p in msg.iter_parts() if p.get_content_type() == "text/plain").get_content()
    assert text.strip()
    assert "Service DOWN" in text
    assert "<" not in text  # no raw HTML in the text alternative


def test_message_without_logo_reference_has_no_related_wrapper():
    # A custom template that does not use the logo must not drag in an image part.
    msg = build_email_message(
        subject="s",
        html_body="<p>no logo here</p>",
        text_body="",
        from_addr="a@b.c",
        to_addr="d@e.f",
    )
    assert msg.get_content_type() == "multipart/alternative"
    assert all(p.get_content_type() != "image/png" for p in msg.walk())


def test_message_serializes_and_carries_the_content_id_header():
    raw = _sample_message().as_bytes()
    assert b"Content-ID: <justup-logo>" in raw
    assert b"Content-Type: multipart/related" in raw


# ─── Test-notification sample data ───────────────────────────────────────────


def test_test_sample_is_a_consistent_open_15_minute_outage():
    alert, target = build_test_alert_sample(now=_FIXED_NOW)
    ctx = build_alert_context(alert, target)
    # Open, started 15 minutes before the sample reference, still ongoing.
    assert ctx["alert"]["resolved_at"] == "Ongoing"
    assert ctx["downtime"]["human"] == "15m"
    assert "2026-01-29" not in ctx["alert"]["started_at"]  # the old hard-coded date is gone
    assert ctx["flags"]["sla"] is True


def test_test_sample_duration_tracks_current_time():
    # With no explicit clock the outage is anchored to "now", so duration reads ~15m ongoing.
    alert, target = build_test_alert_sample()
    ctx = build_alert_context(alert, target)
    assert "15m" in ctx["alert"]["duration"]
    assert "ongoing" in ctx["alert"]["duration"].lower()


@pytest.mark.parametrize("resolved_offset", [None, timedelta(minutes=15)])
def test_naive_timestamps_count_as_utc_for_the_duration(resolved_offset):
    # Naive values are UTC by convention; mixing them with an aware clock must not degrade the duration.
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=15)
    resolved = (started + resolved_offset).replace(tzinfo=UTC).isoformat() if resolved_offset else None
    alert, target = build_test_alert_sample()
    ctx = build_alert_context({**alert, "started_at": started.isoformat(), "resolved_at": resolved}, target)
    assert ctx["alert"]["duration"].startswith("15m")


def test_test_sample_is_marked_as_a_test_notification():
    alert, _ = build_test_alert_sample()
    assert "test notification" in alert["reason"].lower()


# ─── Preview / logo rewriting ────────────────────────────────────────────────


def test_template_to_preview_rewrites_logo_for_the_browser():
    # Both the raw placeholder and an already-rendered cid: reference map to a loadable URL.
    assert "cid:" not in template_to_preview("x {{logo_url}} y")
    assert "cid:" not in template_to_preview(f"x {LOGO_SRC} y")
    assert "/static/img/justup_email_logo.png" in template_to_preview("{{logo_url}}")


def test_html_to_text_strips_markup_and_keeps_content():
    text = html_to_text("<p>Hello <b>World</b></p><div>Line&amp;two</div><script>x</script>")
    assert "Hello World" in text
    assert "Line&two" in text
    assert "<" not in text and "script" not in text


# ─── SMTP end-to-end: the real message as it leaves the transport ────────────


class _CapturingSMTPServer:
    """Local SMTP server on 127.0.0.1 that accepts one message and records its DATA.

    A test double, not a real MTA: nothing leaves the host. It advertises STARTTLS=off
    and no AUTH so the hardened transport sends in the explicit plaintext-relay mode
    (no credentials, TLS off), which lets us capture the exact bytes on the wire.
    """

    def __init__(self):
        import socket
        import threading

        self.message_bytes: bytes = b""
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(10)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            reader = conn.makefile("rb")
            conn.sendall(b"220 localhost ready\r\n")
            in_data = False
            data_lines: list[bytes] = []
            try:
                for raw in reader:
                    if in_data:
                        if raw.strip() == b".":
                            self.message_bytes = b"".join(data_lines)
                            conn.sendall(b"250 queued\r\n")
                            in_data = False
                            continue
                        data_lines.append(raw)
                        continue
                    verb = raw.decode("ascii", "replace").strip().split(" ", 1)[0].upper()
                    if verb == "EHLO":
                        conn.sendall(b"250-localhost\r\n250 HELP\r\n")
                    elif verb == "DATA":
                        conn.sendall(b"354 go ahead\r\n")
                        in_data = True
                    elif verb == "QUIT":
                        conn.sendall(b"221 bye\r\n")
                        return
                    else:
                        conn.sendall(b"250 ok\r\n")
            except OSError:
                return

    def close(self):
        self._sock.close()
        self._thread.join(timeout=5)


def test_end_to_end_send_captures_a_multipart_related_message_with_the_inline_logo():
    import email as _email
    from email import policy as _policy

    from app.services.notifications import smtp_transport

    server = _CapturingSMTPServer()
    try:
        msg = _sample_message()
        cfg = smtp_transport.SMTPConnection(host="127.0.0.1", port=server.port, user=None, password=None, use_tls=False)
        smtp_transport.send_message(msg, cfg, timeout=5)
    finally:
        server.close()

    assert server.message_bytes, "server captured no message"
    # Parse with the modern policy so the reconstructed message exposes iter_parts().
    received = _email.message_from_bytes(server.message_bytes, policy=_policy.default)

    assert received.get_content_type() == "multipart/alternative"
    # Plain-text alternative is present and non-empty.
    text_part = next(p for p in received.walk() if p.get_content_type() == "text/plain")
    assert text_part.get_payload(decode=True).strip()
    # The HTML lives inside a multipart/related that carries the inline PNG by Content-ID.
    related = next(p for p in received.walk() if p.get_content_type() == "multipart/related")
    html_part = next(p for p in related.iter_parts() if p.get_content_type() == "text/html")
    assert LOGO_SRC.encode() in html_part.get_payload(decode=True)
    image = next(p for p in related.iter_parts() if p.get_content_type() == "image/png")
    assert image.get("Content-ID") == f"<{LOGO_CID}>"
    assert image.get_payload(decode=True)[:8] == b"\x89PNG\r\n\x1a\n"
    # No dependency on an external image host survived into the wire bytes.
    assert b"raw.githubusercontent.com" not in server.message_bytes


def test_debug_log_of_placeholder_rendering_never_contains_the_values(caplog):
    secret_url = "https://internal.example/status?token=s3cr3t-token-value"  # noqa: S105 - fake value
    ctx = _ctx()
    ctx["system"] = {**ctx.get("system", {}), "url": secret_url}
    ctx["target"] = {**ctx.get("target", {}), "host": "db01.internal.example", "name": "Payroll-DB"}
    with caplog.at_level(logging.DEBUG, logger="app.services.notifications.email_template"):
        rendered = render_template("{{system.url}} {{target.host}} {{target.name}}", ctx, mode="plain_text")
    assert secret_url in rendered  # the value is rendered ...
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "system.url" in logged  # ... the name and length are logged, never the value
    for value in ("s3cr3t-token-value", "internal.example", "db01", "Payroll-DB"):
        assert value not in logged


# ─── Header is the logo only: no "Monitoring alert" label, bell or divider ───

_HEADER_STATES = ["OPEN", "DOWN", "RESOLVED", "UP", "WARNING", "UNKNOWN"]


@pytest.mark.parametrize("status", _HEADER_STATES)
def test_mail_header_carries_no_monitoring_alert_label(status):
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx(status=status))
    assert "Monitoring alert" not in html
    assert "&#128276;" not in html and "\U0001f514" not in html
    assert "Monitoring alert" not in html_to_text(html)


def test_test_notification_header_carries_no_monitoring_alert_label():
    alert, target = build_test_alert_sample(now=_FIXED_NOW)
    html = render_template(DEFAULT_HTML_TEMPLATE, build_alert_context(alert, target))
    assert "Monitoring alert" not in html
    assert "Monitoring alert" not in html_to_text(html)


def test_header_keeps_the_logo_left_and_the_status_line():
    html = render_template(DEFAULT_HTML_TEMPLATE, _ctx(status="DOWN"))
    header = html[html.index("<!-- HEADER") : html.index("<!-- PRIMARY EVENT -->")]
    assert 'alt="justUp!"' in header
    assert "<!-- STATUS ACCENT LINE -->" in header
    assert "border-left" not in header  # the divider belonged to the removed label


# ── Placeholder reference docs (single source for both settings fragments) ──────


def test_placeholder_docs_cover_exactly_the_whitelist():
    from app.services.notifications.email_template import ALLOWED_PLACEHOLDERS, PLACEHOLDER_DOCS

    assert set(PLACEHOLDER_DOCS) == ALLOWED_PLACEHOLDERS
    assert all(cat and desc for cat, desc in PLACEHOLDER_DOCS.values())


@pytest.mark.parametrize("fragment", ["settings-smtp", "settings-signal"])
def test_settings_fragments_list_every_allowed_placeholder(client, fragment):
    from html import unescape

    from app.services.notifications.email_template import ALLOWED_PLACEHOLDERS

    html = unescape(client.get(f"/ui/fragments/{fragment}").text)
    missing = [p for p in sorted(ALLOWED_PLACEHOLDERS) if f"<code>{{{{{p}}}}}</code>" not in html]
    assert not missing
