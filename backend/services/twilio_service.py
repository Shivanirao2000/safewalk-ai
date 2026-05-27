"""
Notification service for SafeWalk AI.

Public interface is intentionally identical to the original Twilio SMS version
so swapping backends requires no changes in callers.

Current backend: Gmail SMTP via smtplib (free, no credentials cost).

To revert to Twilio SMS:
  1. Restore `from twilio.rest import Client` and the Client initialisation.
  2. Replace `_send_email` with the original synchronous `_send` method.
  3. Update `send_emergency_sms` / `send_safe_confirmation` to call `_send`.
  4. Re-enable the Twilio fields in config.py.
"""

import asyncio
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from config import settings
from models import SafeWalkSession

logger = logging.getLogger(__name__)

_MOCK_MESSAGE_ID = "<mock-test-mode@safewalk.local>"
_SMTP_HOST       = "smtp.gmail.com"
_SMTP_PORT       = 587


class TwilioService:
    def __init__(self):
        self._test_mode = os.getenv("SAFEWALK_TEST_MODE", "").strip() in ("1", "true", "yes")
        self._from_addr = settings.gmail_user
        self._to_addr   = settings.emergency_contact_email
        if self._test_mode:
            logger.info("TwilioService: TEST MODE — all notifications are mocked")

    # ------------------------------------------------------------------ #
    #  Core delivery (synchronous — called via asyncio.to_thread)        #
    # ------------------------------------------------------------------ #

    def _send_email(
        self, to: str, subject: str, text_body: str, html_body: str
    ) -> dict:
        if self._test_mode:
            logger.info("[TEST MODE] Mock email → %s | %s", to, subject)
            return {"success": True, "message_sid": _MOCK_MESSAGE_ID, "error": None}

        msg = MIMEMultipart("alternative")
        msg["Message-ID"] = make_msgid(domain="safewalk.local")
        msg["Date"]        = formatdate(localtime=False)
        msg["Subject"]     = subject
        msg["From"]        = f"SafeWalk AI <{self._from_addr}>"
        msg["To"]          = to

        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))

        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(self._from_addr, settings.gmail_app_password)
            smtp.sendmail(self._from_addr, [to], msg.as_string())

        return {"success": True, "message_sid": msg["Message-ID"], "error": None}

    async def _deliver(
        self, subject: str, text_body: str, html_body: str, session_id: str, label: str
    ) -> dict:
        """Async wrapper: runs _send_email in a thread and handles all exceptions."""
        try:
            result = await asyncio.to_thread(
                self._send_email, self._to_addr, subject, text_body, html_body
            )
            logger.info(
                "%s email delivered → %s for session %s (id=%s)",
                label, self._to_addr, session_id, result["message_sid"],
            )
            return result
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                "%s email auth failed for session %s: %s — "
                "check GMAIL_USER / GMAIL_APP_PASSWORD in .env",
                label, session_id, exc,
            )
            return {"success": False, "message_sid": None, "error": str(exc)}
        except Exception as exc:
            logger.exception(
                "%s email failed for session %s: %s", label, session_id, exc
            )
            return {"success": False, "message_sid": None, "error": str(exc)}

    # ------------------------------------------------------------------ #
    #  Public interface (unchanged from original Twilio version)         #
    # ------------------------------------------------------------------ #

    async def send_emergency_sms(self, session: SafeWalkSession) -> dict:
        loc       = session.location
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        maps_url  = f"https://maps.google.com/?q={loc.lat},{loc.lng}"

        subject = f"🚨 SafeWalk ALERT — {session.user_name} may need help"

        text_body = (
            f"SAFEWALK EMERGENCY ALERT\n"
            f"{'─' * 40}\n"
            f"User:            {session.user_name}\n"
            f"Contact number:  {session.emergency_contact}\n"
            f"Location:        {loc.address}\n"
            f"GPS:             {loc.lat}, {loc.lng}\n"
            f"Google Maps:     {maps_url}\n"
            f"Distress level:  {session.distress_level}/5\n"
            f"Time:            {timestamp}\n"
            f"{'─' * 40}\n"
            f"This is an automated alert from SafeWalk AI.\n"
        )

        html_body = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:10px;overflow:hidden;
                    box-shadow:0 4px 20px rgba(0,0,0,.12);">

        <!-- Header -->
        <tr><td style="background:#ef4444;padding:24px 32px;text-align:center;">
          <p style="margin:0;font-size:28px;">🚨</p>
          <h1 style="margin:8px 0 0;color:#fff;font-size:22px;letter-spacing:-.3px;">
            SafeWalk Emergency Alert
          </h1>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 20px;font-size:16px;color:#111;">
            <strong>{session.user_name}</strong> may need help.
            Please check on them immediately.
          </p>

          <table width="100%" cellpadding="8" cellspacing="0"
                 style="border-collapse:collapse;font-size:14px;">
            <tr style="background:#fef2f2;">
              <td style="font-weight:600;color:#374151;width:38%;
                         border-bottom:1px solid #fecaca;">Distress level</td>
              <td style="color:#ef4444;font-weight:700;font-size:16px;
                         border-bottom:1px solid #fecaca;">
                {session.distress_level} / 5
              </td>
            </tr>
            <tr>
              <td style="font-weight:600;color:#374151;
                         border-bottom:1px solid #e5e7eb;">Contact number</td>
              <td style="color:#111;border-bottom:1px solid #e5e7eb;">
                {session.emergency_contact}
              </td>
            </tr>
            <tr style="background:#f9fafb;">
              <td style="font-weight:600;color:#374151;
                         border-bottom:1px solid #e5e7eb;">Last location</td>
              <td style="color:#111;border-bottom:1px solid #e5e7eb;">{loc.address}</td>
            </tr>
            <tr>
              <td style="font-weight:600;color:#374151;
                         border-bottom:1px solid #e5e7eb;">GPS</td>
              <td style="border-bottom:1px solid #e5e7eb;">
                <a href="{maps_url}" style="color:#3b82f6;">
                  {loc.lat:.5f}, {loc.lng:.5f}
                </a>
              </td>
            </tr>
            <tr style="background:#f9fafb;">
              <td style="font-weight:600;color:#374151;">Time (UTC)</td>
              <td style="color:#111;">{timestamp}</td>
            </tr>
          </table>

          <p style="margin:24px 0 0;text-align:center;">
            <a href="{maps_url}"
               style="display:inline-block;background:#ef4444;color:#fff;
                      padding:12px 28px;border-radius:6px;font-weight:600;
                      text-decoration:none;font-size:14px;">
              Open in Google Maps
            </a>
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:16px 32px;
                       border-top:1px solid #e5e7eb;text-align:center;">
          <p style="margin:0;font-size:11px;color:#9ca3af;">
            Automated alert from SafeWalk AI · Session {session.session_id[:8]}
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

        return await self._deliver(subject, text_body, html_body, session.session_id, "Emergency")

    async def send_safe_confirmation(self, session: SafeWalkSession) -> dict:
        subject = f"✅ SafeWalk — {session.user_name} is home safe"

        text_body = (
            f"SafeWalk Safe Confirmation\n"
            f"{'─' * 40}\n"
            f"{session.user_name} has safely ended their walk.\n"
            f"No further action needed.\n"
            f"{'─' * 40}\n"
            f"This is an automated message from SafeWalk AI.\n"
        )

        html_body = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:system-ui,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:10px;overflow:hidden;
                    box-shadow:0 4px 20px rgba(0,0,0,.12);">

        <!-- Header -->
        <tr><td style="background:#22c55e;padding:24px 32px;text-align:center;">
          <p style="margin:0;font-size:28px;">✅</p>
          <h1 style="margin:8px 0 0;color:#fff;font-size:22px;letter-spacing:-.3px;">
            All Clear — Safe Walk Complete
          </h1>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 32px;text-align:center;">
          <p style="margin:0 0 8px;font-size:18px;font-weight:700;color:#111;">
            {session.user_name} is home safe.
          </p>
          <p style="margin:0;font-size:15px;color:#6b7280;">
            Their SafeWalk session has ended normally.<br>No further action needed.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:16px 32px;
                       border-top:1px solid #e5e7eb;text-align:center;">
          <p style="margin:0;font-size:11px;color:#9ca3af;">
            Automated message from SafeWalk AI · Session {session.session_id[:8]}
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

        return await self._deliver(subject, text_body, html_body, session.session_id, "Safe confirmation")


twilio_service = TwilioService()
