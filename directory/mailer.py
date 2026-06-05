"""Outbound email for directory notifications (Aliyun DirectMail / SMTP_SSL).

Reads SMTP_* from the environment. If SMTP is not configured the helpers
become no-ops (logged), so approval flows never fail because of email.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger("directory.mailer")

FROM_NAME = "Agent Tools"
REPLY_TO = "contact@agent-tools.cloud"
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


def _send(to_email: str, subject: str, text: str, html: str) -> bool:
    conf = _conf()
    if conf is None:
        log.warning("SMTP not configured; skipping email to %s", to_email)
        return False
    sender = conf["from"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_NAME, sender))
    msg["To"] = to_email
    msg["Reply-To"] = REPLY_TO
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="mail.agent-tools.cloud")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(conf["host"], conf["port"], context=ctx, timeout=30) as s:
            s.login(conf["user"], conf["pass"])
            s.sendmail(sender, [to_email], msg.as_string())
        log.info("sent notification email to %s (%s)", to_email, subject)
        return True
    except Exception as e:  # noqa: BLE001 - email must never break approval
        log.warning("failed to send email to %s: %r", to_email, e)
        return False


def _approval_html(service_name: str, service_url: str, verified_line: str | None) -> str:
    verified_card = ""
    if verified_line:
        verified_card = f"""
          <!-- verified card -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
            style="background:#f8fafc;border:1px solid {BORDER};border-radius:12px;margin:0 0 24px 0;">
            <tr><td style="padding:14px 16px;">
              <div style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;">Verified endpoint</div>
              <div style="font-family:'SF Mono',ui-monospace,Menlo,Consolas,monospace;font-size:13px;color:{INK};line-height:1.5;">
                {verified_line}
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
            Thanks for submitting <strong style="color:{INK};">{service_name}</strong> to the
            Agent&nbsp;Tools x402 directory. We verified your endpoint and it&rsquo;s now indexed.
          </p>
{verified_card}
          <!-- CTA -->
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 26px 0;">
            <tr><td style="border-radius:10px;background:{BLUE};">
              <a href="{service_url}" target="_blank"
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



def _rejection_html(service_name: str, reason: str | None) -> str:
    reason_card = ""
    if reason:
        reason_card = f"""
          <!-- reason card -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
            style="background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;margin:0 0 24px 0;">
            <tr><td style="padding:14px 16px;">
              <div style="font-size:11px;font-weight:600;color:#c2730c;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;">Why it wasn&rsquo;t listed</div>
              <div style="font-size:13px;color:{INK};line-height:1.55;">
                {reason}
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
            Thanks for submitting <strong style="color:{INK};">{service_name}</strong> to the
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
    """Email admin@agent-tools.cloud about a submission + its review verdict.

    kind    short channel label, e.g. "x402 submission", "MCP submission".
    title   service / endpoint name.
    verdict listed | rejected | pending | updated | verified | uncertain.
    fields  list of (label, value) rows to render (url, contact, x402, ...).
    Never raises — email failures must not break the submit/review flow.
    """
    try:
        subject = f"[Agent Tools] {kind}: {title} — {verdict}"
        text_lines = [f"{kind}", f"{title}", f"verdict: {verdict}", ""]
        for label, value in fields:
            if value not in (None, ""):
                text_lines.append(f"{label}: {value}")
        text = "\n".join(text_lines) + "\n"
        html = _admin_html(title, kind, verdict, fields)
        return _send(ADMIN_EMAIL, subject, text, html)
    except Exception as e:  # noqa: BLE001
        log.warning("admin notification failed for %r: %r", title, e)
        return False
