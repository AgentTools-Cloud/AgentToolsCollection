"""Outbound email for directory notifications (Aliyun DirectMail / SMTP_SSL).

Reads SMTP_* from the environment. If SMTP is not configured the helpers
become no-ops (logged), so approval flows never fail because of email.
"""

from __future__ import annotations

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
