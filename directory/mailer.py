"""External email plus local internal notice routing.

Submitter/owner email still uses configured SMTP. Agent Tools internal admin
notifications and watchdog alerts are recorded locally, never sent by SMTP.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import urlsplit

log = logging.getLogger("directory.mailer")

FROM_NAME = "Agent Tools"
REPLY_TO = "contact@agent-tools.cloud"
CLAIM_INVITE_FROM = "noreply@mail.agent-tools.cloud"
SITE = "https://agent-tools.cloud"
ADMIN_EMAIL = os.getenv("ADMIN_NOTIFY_EMAIL", "admin@agent-tools.cloud")

# palette
BLUE = "#2563eb"
INK = "#0f172a"
SLATE = "#475569"
LIGHT = "#f1f5f9"
BORDER = "#e2e8f0"


def _conf() -> dict | None:
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    if not (host and user and pw):
        return None
    return {
        "host": host,
        "port": int(os.getenv("SMTP_PORT", "465")),
        "user": user,
        "pass": pw,
        "from": os.getenv("SMTP_FROM", user),
    }


def _record_external_result(subject: str, state: str, event_id: str, *, purpose: str | None = None) -> None:
    """Best-effort local receipt; never resend or copy a mail to the admin."""
    try:
        from . import notices
        purpose = purpose or {
            "You're listed on Agent Tools (x402 directory)": 'approval',
            "Your Agent Tools submission wasn't listed (here's why)": 'rejection',
        }.get(subject, 'external')
        if not notices.publish_mail_result(purpose, state, event_id):
            log.warning('external mail result was not recorded locally')
    except Exception as exc:
        log.warning('external mail result notice failed: %s', type(exc).__name__)


def _suppression_reason(to_email: str) -> str | None:
    """A recipient the provider already failed to deliver to is never mailed
    again. If the list cannot be read, no suppression is known and the send
    goes ahead (a DB hiccup must not silently drop mail)."""
    try:
        from . import db
        with db.connect(read_only=True) as conn:
            return db.mail_suppressed(conn, to_email)
    except Exception as exc:  # noqa: BLE001
        log.warning("mail suppression lookup failed: %s", type(exc).__name__)
        return None


def _send(to_email: str, subject: str, text: str, html: str) -> bool:
    if str(to_email).strip().lower() in {ADMIN_EMAIL.lower(), 'admin@agent-tools.cloud'}:
        from . import notices
        return notices.capture_legacy_admin(subject, text)
    return _deliver_external(to_email, subject, text, html)["status"] == "accepted"


def claim_invitation_configured() -> bool:
    try:
        conf = _conf()
    except (TypeError, ValueError):
        return False
    return bool(conf and conf["from"].casefold() == CLAIM_INVITE_FROM and conf["port"] == 465)


def send_claim_invitation(to_email: str, subject: str, text: str, markup: str,
                  *, message_id: str, before_dispatch=None) -> dict:
    return _deliver_external(to_email, subject, text, markup, purpose="claim_invitation",
                    message_id=message_id, expected_sender=CLAIM_INVITE_FROM, before_dispatch=before_dispatch)


def _deliver_external(to_email: str, subject: str, text: str, markup: str,
                      *, purpose: str | None = None, message_id: str | None = None,
                      expected_sender: str | None = None, before_dispatch=None) -> dict:
    event_id = uuid.uuid4().hex
    accepted = False
    status = "failed"
    reason = None
    stage = "configuration"
    message_id = message_id or make_msgid(domain="mail.agent-tools.cloud")
    suppressed = _suppression_reason(to_email)
    if suppressed:
        log.info("external email skipped: recipient on the suppression list (%s)", suppressed)
        _record_external_result(subject, "skipped", event_id, purpose=purpose)
        return {"status": "skipped", "reason": "recipient_suppressed", "event_id": event_id,
                "message_id": message_id}
    try:
        conf = _conf()
        if conf is None:
            status = "skipped"
            reason = "smtp_not_configured"
            log.warning('SMTP not configured; external email not sent')
        else:
            if expected_sender and (conf["from"].casefold() != expected_sender or conf["port"] != 465):
                raise ValueError("claim_invite_sender_configuration_invalid")
            sender = conf["from"]
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = formataddr((FROM_NAME, sender))
            msg["To"] = to_email
            msg["Reply-To"] = REPLY_TO
            msg["Date"] = formatdate(localtime=False)
            msg["Message-ID"] = message_id
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(markup, "html", "utf-8"))
            ctx = ssl.create_default_context()
            stage = "connection"
            with smtplib.SMTP_SSL(conf["host"], conf["port"], context=ctx, timeout=30) as session:
                session.login(conf["user"], conf["pass"])
                if before_dispatch is not None and not before_dispatch():
                    return {"status": "failed", "reason": "dispatch_guard_blocked", "event_id": event_id, "message_id": message_id}
                stage = "dispatch"
                refused = session.sendmail(sender, [to_email], msg.as_string())
                accepted = not bool(refused)
                status = "accepted" if accepted else "failed"
                reason = None if accepted else "smtp_recipient_refused"
                stage = "complete"
            if accepted:
                log.info('external notification accepted by SMTP; delivery unverified')
    except Exception as exc:
        log.warning('external email transport ended with %s; accepted=%s', type(exc).__name__, accepted)
        definite_refusal = isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError))
        status = "accepted" if accepted else ("indeterminate" if stage == "dispatch" and not definite_refusal else "failed")
        reason = None if accepted else type(exc).__name__
    finally:
        state = "accepted" if status == "accepted" else ("skipped" if status == "skipped" else "failed_or_unknown")
        if purpose:
            _record_external_result(subject, state, event_id, purpose=purpose)
        else:
            _record_external_result(subject, state, event_id)
    return {"status": status, "reason": reason, "event_id": event_id, "message_id": message_id}


def _approval_html(service_name: str, service_url: str, verified_line: str | None) -> str:
    safe_name = html.escape(str(service_name), quote=True)
    safe_url = html.escape(str(service_url), quote=True)
    safe_verified = html.escape(str(verified_line), quote=True) if verified_line else ""
    verified_card = ""
    if safe_verified:
        verified_card = f"""
          <!-- verified card -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
            style="background:#f8fafc;border:1px solid {BORDER};border-radius:12px;margin:0 0 24px 0;">
            <tr><td style="padding:14px 16px;">
              <div style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;">Verified endpoint</div>
              <div style="font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:{INK};line-height:1.5;">
                {safe_verified}
              </div>
            </td></tr>
          </table>"""
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{LIGHT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{LIGHT};padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:16px;overflow:hidden;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

        <!-- header -->
        <tr><td style="background:{BLUE};padding:18px 28px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:middle;padding-right:10px;">
              <div style="width:26px;height:26px;background:#ffffff;border-radius:6px;text-align:center;line-height:26px;color:{BLUE};font-weight:800;font-size:15px;">&#9670;</div>
            </td>
            <td style="vertical-align:middle;color:#ffffff;font-size:16px;font-weight:700;letter-spacing:.2px;">
              Agent&nbsp;Tools <span style="color:#bfdbfe;font-weight:500;">&middot; x402</span>
            </td>
          </tr></table>
        </td></tr>

        <!-- body -->
        <tr><td style="padding:34px 32px 8px 32px;">
          <span style="display:inline-block;background:#dcfce7;color:#15803d;font-size:12px;font-weight:600;
            padding:5px 12px;border-radius:9999px;letter-spacing:.3px;">&#10003;&nbsp; YOU'RE LISTED</span>

          <h1 style="margin:18px 0 4px 0;font-size:22px;line-height:1.3;color:{INK};font-weight:800;">
            Your service is live on Agent&nbsp;Tools
          </h1>
          <p style="margin:0 0 20px 0;font-size:15px;line-height:1.6;color:{SLATE};">
            Thanks for submitting <strong style="color:{INK};">{safe_name}</strong> to the
            Agent&nbsp;Tools x402 directory. We verified your endpoint and it&rsquo;s now indexed.
          </p>
{verified_card}
          <!-- CTA -->
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 26px 0;">
            <tr><td style="border-radius:10px;background:{BLUE};">
              <a href="{safe_url}" target="_blank"
                style="display:inline-block;padding:11px 22px;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:10px;">
                View in directory &rarr;
              </a>
            </td></tr>
          </table>

          <!-- bullets -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 8px 0;">
            <tr><td style="font-size:14px;line-height:1.7;color:{SLATE};">
              &bull;&nbsp; <strong style="color:{INK};">Discovery-only.</strong> We index verifiable x402 endpoints so agents can find them &mdash; we don&rsquo;t proxy your traffic, hold funds, or take a cut.<br>
              &bull;&nbsp; <strong style="color:{INK};">Listing is free.</strong><br>
              &bull;&nbsp; Wrong price, description, or category? Just reply to this email.
            </td></tr>
          </table>
        </td></tr>

        <!-- footer -->
        <tr><td style="padding:20px 32px 26px 32px;border-top:1px solid {BORDER};">
          <p style="margin:0;font-size:12px;line-height:1.6;color:#94a3b8;">
            Agent&nbsp;Tools &mdash; the open x402 service discovery layer.<br>
            <a href="{SITE}" style="color:{BLUE};text-decoration:none;">agent-tools.cloud</a>
            &nbsp;&middot;&nbsp; Reply to reach a human.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_approval_email(
    to_email: str,
    service_name: str,
    slug: str,
    verified_line: str | None = None,
) -> bool:
    """Notify a submitter that their service was approved and listed."""
    if not to_email or "@" not in to_email:
        log.info("no valid contact email for %r; skipping", service_name)
        return False
    service_url = f"{SITE}/services/{slug}"
    subject = "You're listed on Agent Tools (x402 directory)"
    vline = f"\nWe verified your endpoint ({verified_line}) and it's" if verified_line else "\nYour service is"
    text = (
        "You're listed on Agent Tools\n\n"
        f'Thanks for submitting "{service_name}" to the Agent Tools x402 directory.'
        f"{vline} now indexed:\n{service_url}\n\n"
        "- Discovery-only: we index verifiable x402 endpoints so agents can find them.\n"
        "  We don't proxy traffic, hold funds, or take a cut.\n"
        "- Listing is free.\n"
        "- Wrong price/description/category? Just reply to this email.\n\n"
        "Agent Tools - agent-tools.cloud\n"
    )
    html = _approval_html(service_name, service_url, verified_line)
    return _send(to_email, subject, text, html)


def claim_invitation_message(
    service_name: str,
    source_name: str,
    listing_url: str,
    unsubscribe_url: str,
) -> tuple[str, str, str]:
    """Render an English invitation without consulting SMTP or sending mail."""
    name = " ".join(str(service_name).split())[:160]
    source = " ".join(str(source_name).split())[:160]
    if not name or not source:
        raise ValueError("invitation_name_and_source_required")
    for value in (listing_url, unsubscribe_url):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "agent-tools.cloud"
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise ValueError("invitation_links_must_use_our_site")
    subject = f"{name} is listed on Agent Tools"
    claim_url = SITE + "/account"
    text = (
        "Hi,\n\n"
        f'We found "{name}" in {source} and added it to Agent Tools:\n'
        f"{listing_url}\n\n"
        "Claiming is free. After verifying control of your host, you can correct "
        "your listing and maintain its API details:\n"
        f"{claim_url}\n\n"
        "Your listing stays up whether or not you claim it.\n"
        "This is a one-time listing notification. We won't subscribe you to a mailing list.\n"
        f"Stop these invitations: {unsubscribe_url}\n\n"
        "Agent Tools\ncontact@agent-tools.cloud\n"
    )
    safe_name = html.escape(name, quote=True)
    safe_source = html.escape(source, quote=True)
    safe_listing = html.escape(listing_url, quote=True)
    safe_unsubscribe = html.escape(unsubscribe_url, quote=True)
    markup = f"""<!DOCTYPE html>
  <html lang="en"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1"></head>
  <body style="margin:0;padding:24px 12px;background:{LIGHT};color:{INK};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr><td align="center"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"
   style="max-width:560px;background:#fff;border:1px solid {BORDER};border-radius:8px;
   font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;">
  <tr><td style="padding:18px 24px;background:{BLUE};color:#fff;font-size:18px;font-weight:700;">Agent Tools</td></tr>
  <tr><td style="padding:24px;overflow-wrap:anywhere;">
  <h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;">{safe_name} is listed</h1>
  <p>We found your service in {safe_source} and added it to Agent Tools.</p>
  <p><a href="{safe_listing}" style="color:{BLUE};">View your listing</a></p>
  <p>Claiming is free. After verifying control of your host, you can correct your listing and maintain its API details.</p>
  <p><a href="{claim_url}" style="display:inline-block;padding:10px 16px;background:{BLUE};color:#fff;border-radius:6px;text-decoration:none;">Claim your host</a></p>
  <p>Your listing stays up whether or not you claim it.</p>
  <p style="font-size:13px;color:{SLATE};">This is a one-time listing notification. We won't subscribe you to a mailing list.
  <a href="{safe_unsubscribe}" style="color:{SLATE};">Stop these invitations</a>.</p>
  <p style="font-size:13px;color:{SLATE};">Agent Tools<br>contact@agent-tools.cloud</p>
  </td></tr></table></td></tr></table></body></html>"""
    return subject, text, markup



def claim_invitation_group_message(listings: list[dict], unsubscribe_url: str) -> tuple[str, str, str]:
  if not listings:
    raise ValueError("claim_invitation_listings_required")
  first = listings[0]
  single = claim_invitation_message(first["name"], first["source_name"], first["listing_url"], unsubscribe_url)
  if len(listings) == 1:
    return single
  entries = []
  markup_entries = []
  for listing in listings[:10]:
    name = " ".join(str(listing["name"]).split())[:160]
    source = " ".join(str(listing["source_name"]).split())[:160]
    url = str(listing["listing_url"])
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "agent-tools.cloud" or parsed.username or parsed.password or parsed.port not in (None, 443):
      raise ValueError("invitation_links_must_use_our_site")
    entries.append(f"- {name} ({source}): {url}")
    markup_entries.append(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a> ({html.escape(source)})</li>')
  more = f"\nAnd {len(listings) - 10} more listings from the same host operator.\n" if len(listings) > 10 else "\n"
  subject = " ".join(str(first["name"]).split())[:160] + " and other listings on Agent Tools"
  text = ("Hi,\n\nWe found your public services and added them to Agent Tools:\n" + "\n".join(entries) + more
      + "\nClaiming is free. After verifying control of the listed hosts, you can correct your listings and maintain their API details:\n"
      + SITE + "/account\n\nYour listings stay up whether or not you claim them.\n"
      + "This is a one-time listing notification. We won't subscribe you to a mailing list.\n"
      + "Stop these invitations: " + unsubscribe_url + "\n\nAgent Tools\ncontact@agent-tools.cloud\n")
  markup = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:24px 12px;background:{LIGHT};color:{INK};font-family:Segoe UI,sans-serif;line-height:1.6;">'
        f'<main style="max-width:560px;margin:auto;background:white;border:1px solid {BORDER};border-radius:8px;padding:24px;overflow-wrap:anywhere;">'
        '<h1 style="font-size:22px;line-height:1.3;">Your services are listed on Agent Tools</h1>'
        '<p>We found your public services and added them to Agent Tools.</p><ul>' + "".join(markup_entries) + '</ul>'
        + (f'<p>And {len(listings) - 10} more listings from the same host operator.</p>' if len(listings) > 10 else '')
        + '<p>Claiming is free. After verifying control of the listed hosts, you can correct your listings and maintain their API details.</p>'
        + f'<p><a href="{SITE}/account">Claim your hosts</a></p><p>Your listings stay up whether or not you claim them.</p>'
        + "<p>This is a one-time listing notification. We won't subscribe you to a mailing list.</p>"
        + f'<p><a href="{html.escape(unsubscribe_url, quote=True)}">Stop these invitations</a></p><p>Agent Tools<br>contact@agent-tools.cloud</p></main></body></html>')
  return subject, text, markup


def _rejection_html(service_name: str, reason: str | None) -> str:
    safe_name = html.escape(str(service_name), quote=True)
    safe_reason = html.escape(str(reason), quote=True) if reason else ""
    reason_card = ""
    if safe_reason:
        reason_card = f"""
          <!-- reason card -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
            style="background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;margin:0 0 24px 0;">
            <tr><td style="padding:14px 16px;">
              <div style="font-size:11px;font-weight:600;color:#c2730c;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;">Why it wasn&rsquo;t listed</div>
              <div style="font-size:13px;color:{INK};line-height:1.55;">
                {safe_reason}
              </div>
            </td></tr>
          </table>"""
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{LIGHT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{LIGHT};padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:16px;overflow:hidden;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

        <!-- header -->
        <tr><td style="background:{BLUE};padding:18px 28px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="vertical-align:middle;padding-right:10px;">
              <div style="width:26px;height:26px;background:#ffffff;border-radius:6px;text-align:center;line-height:26px;color:{BLUE};font-weight:800;font-size:15px;">&#9670;</div>
            </td>
            <td style="vertical-align:middle;color:#ffffff;font-size:16px;font-weight:700;letter-spacing:.2px;">
              Agent&nbsp;Tools <span style="color:#bfdbfe;font-weight:500;">&middot; x402</span>
            </td>
          </tr></table>
        </td></tr>

        <!-- body -->
        <tr><td style="padding:34px 32px 8px 32px;">
          <span style="display:inline-block;background:#ffedd5;color:#c2410c;font-size:12px;font-weight:600;
            padding:5px 12px;border-radius:9999px;letter-spacing:.3px;">&#9888;&nbsp; NOT LISTED YET</span>

          <h1 style="margin:18px 0 4px 0;font-size:22px;line-height:1.3;color:{INK};font-weight:800;">
            We couldn&rsquo;t verify your submission
          </h1>
          <p style="margin:0 0 20px 0;font-size:15px;line-height:1.6;color:{SLATE};">
            Thanks for submitting <strong style="color:{INK};">{safe_name}</strong> to the
            Agent&nbsp;Tools x402 directory. We couldn&rsquo;t confirm x402 support, so it&rsquo;s
            not listed yet.
          </p>
{reason_card}
          <!-- how to fix -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 8px 0;">
            <tr><td style="font-size:14px;line-height:1.7;color:{SLATE};">
              &bull;&nbsp; <strong style="color:{INK};">Expose x402.</strong> Your endpoint should return an HTTP <strong>402</strong> challenge, or serve a <strong>/.well-known/x402</strong> manifest.<br>
              &bull;&nbsp; <strong style="color:{INK};">Then resubmit</strong> &mdash; auto-verification runs again and you&rsquo;ll be listed within minutes.<br>
              &bull;&nbsp; Think this is a mistake? Just reply to this email and a human will take a look.
            </td></tr>
          </table>

          <!-- CTA -->
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0 26px 0;">
            <tr><td style="border-radius:10px;background:{BLUE};">
              <a href="{SITE}/submit" target="_blank"
                style="display:inline-block;padding:11px 22px;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:10px;">
                Resubmit &rarr;
              </a>
            </td></tr>
          </table>
        </td></tr>

        <!-- footer -->
        <tr><td style="padding:20px 32px 26px 32px;border-top:1px solid {BORDER};">
          <p style="margin:0;font-size:12px;line-height:1.6;color:#94a3b8;">
            Agent&nbsp;Tools &mdash; the open x402 service discovery layer.<br>
            <a href="{SITE}" style="color:{BLUE};text-decoration:none;">agent-tools.cloud</a>
            &nbsp;&middot;&nbsp; Reply to reach a human.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_rejection_email(
    to_email: str,
    service_name: str,
    reason: str | None = None,
) -> bool:
    """Notify a submitter that their service was not listed, with the reason."""
    if not to_email or "@" not in to_email:
        log.info("no valid contact email for %r; skipping rejection", service_name)
        return False
    subject = "Your Agent Tools submission wasn't listed (here's why)"
    reason_line = f"\n\nReason: {reason}" if reason else ""
    text = (
        "We couldn't verify your Agent Tools submission\n\n"
        f'Thanks for submitting "{service_name}" to the Agent Tools x402 directory.'
        " We couldn't confirm x402 support, so it's not listed yet."
        f"{reason_line}\n\n"
        "How to fix:\n"
        "- Expose x402: return an HTTP 402 challenge, or serve a /.well-known/x402 manifest.\n"
        "- Then resubmit at " + SITE + "/submit - auto-verification runs again.\n"
        "- Think this is a mistake? Just reply to this email.\n\n"
        "Agent Tools - agent-tools.cloud\n"
    )
    html = _rejection_html(service_name, reason)
    return _send(to_email, subject, text, html)


# --- admin notifications (every submission + auto-review verdict) -----------

_VERDICT_COLOR = {
    "listed": "#15803d", "verified": "#15803d", "updated": "#15803d",
    "rejected": "#b91c1c", "pending": "#b45309", "uncertain": "#b45309",
    "error": "#b91c1c",
}


def _admin_html(title: str, kind: str, verdict: str, fields: list[tuple]) -> str:
    color = _VERDICT_COLOR.get((verdict or "").lower(), SLATE)
    rows = ""
    for label, value in fields:
        if value is None or value == "":
            continue
        lbl = html.escape(str(label))
        val = html.escape(str(value))
        rows += (
            f'<tr><td style="padding:11px 0;border-bottom:1px solid {BORDER};">'
            f'<div style="font-size:11px;font-weight:600;color:#94a3b8;'
            f'letter-spacing:.5px;text-transform:uppercase;margin-bottom:3px;">{lbl}</div>'
            f'<div style="font-size:14px;color:{INK};line-height:1.5;'
            f'word-break:break-word;">{val}</div>'
            f'</td></tr>'
        )
    safe_title = html.escape(str(title))
    safe_kind = html.escape(str(kind))
    safe_verdict = html.escape(str(verdict))
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{LIGHT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{LIGHT};padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:14px;overflow:hidden;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
        <tr><td style="background:{INK};padding:14px 24px;color:#fff;font-size:14px;font-weight:700;">
          Agent&nbsp;Tools &middot; admin
        </td></tr>
        <tr><td style="padding:24px 24px 8px 24px;">
          <div style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:.6px;text-transform:uppercase;">{safe_kind}</div>
          <h1 style="margin:6px 0 2px 0;font-size:19px;color:{INK};font-weight:800;">{safe_title}</h1>
          <span style="display:inline-block;margin:10px 0 18px 0;background:{color}1a;color:{color};
            font-size:12px;font-weight:700;padding:5px 13px;border-radius:9999px;text-transform:uppercase;letter-spacing:.4px;">
            {safe_verdict}</span>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
            style="border-top:1px solid {BORDER};margin-top:4px;">
            {rows}
          </table>
        </td></tr>
        <tr><td style="padding:16px 24px 22px 24px;">
          <p style="margin:0;font-size:11px;color:#94a3b8;">
            Automated admin notification &middot; <a href="{SITE}" style="color:{BLUE};text-decoration:none;">agent-tools.cloud</a>
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_admin_notification(
    kind: str,
    title: str,
    verdict: str,
    fields: list[tuple],
) -> bool:
    """Record an internal notice about a submission, never email the admin.

    kind    short channel label, e.g. "x402 submission", "MCP submission".
    title   service / endpoint name.
    verdict listed | rejected | pending | updated | verified | uncertain.
    fields  list of (label, value) rows to render (url, contact, x402, ...).
    Never raises; storage failure does not fall back to SMTP.
    """
    try:
        from . import notices
        return notices.publish_admin(kind, title, verdict, fields)
    except Exception as e:  # noqa: BLE001
        log.warning("internal notice failed: %s", type(e).__name__)
        return False


def send_internal_notice(subject: str, text: str, html: str) -> bool:
    """Compatibility-shaped watchdog sink; persist typed counts, not email HTML."""
    from . import notices
    titles = ('来源端点失配警告', '摄入数据质量提醒', '后台任务异常')
    title = next((value for value in titles if f'[agent-tools] {value}:' in subject), '后台巡检通知')
    problems = [line[2:] for line in str(text).splitlines() if line.startswith('- ')]
    advisory_only = all(problem.startswith(('新增数据质量拒绝组:', '来源端点失配警告:'))
                        for problem in problems)
    return notices.publish_watchdog(title, problems, advisory_only=advisory_only)
