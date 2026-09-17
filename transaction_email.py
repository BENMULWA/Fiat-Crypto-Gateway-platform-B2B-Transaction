"""Best-effort deposit/withdrawal confirmation email — the email-channel
counterpart to notify_user()'s in-app bell + real-time websocket push (see
notifications.py). Same SMTP pattern already proven in routes/auth.py's
send_login_notification_email/send_email_otp (settings.smtp_*, smtplib,
EmailMessage) — NOT the dead backend/email_service.py, which is unused
elsewhere in the codebase and imports a nonexistent `.utils` module (backend
isn't a package), so it would raise ImportError the moment anything tried to
call it.
"""
import smtplib
from email.message import EmailMessage

from config import settings

SEVERITY_EMOJI = {"success": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️"}


def send_transaction_email(
    recipient: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    anti_phishing_code: str = "",
    extra: dict | None = None,
) -> None:
    """Sends a deposit/withdrawal confirmation email. Never raises — a
    delivery failure (or SMTP not configured at all, e.g. local dev) must
    never break the deposit/withdrawal flow that triggered it, exactly like
    notify_user()'s own in-app notification insert/broadcast."""
    smtp_host = getattr(settings, "smtp_host", "")
    smtp_port = int(getattr(settings, "smtp_port", 587) or 587)
    smtp_user = getattr(settings, "smtp_user", "")
    smtp_pass = getattr(settings, "smtp_password", "")
    sender = getattr(settings, "smtp_from_email", "") or smtp_user
    tls_enabled = bool(getattr(settings, "smtp_use_tls", True))

    if not smtp_host or not sender or not recipient:
        return

    emoji = SEVERITY_EMOJI.get(severity, "")
    subject = f"{emoji} {title}".strip()
    extra = extra or {}
    asset = extra.get("asset")
    amount = extra.get("amount")
    tx_hash = extra.get("txHash")

    detail_lines = []
    if amount is not None and asset:
        detail_lines.append(f"Amount: {amount:g} {asset}" if isinstance(amount, (int, float)) else f"Amount: {amount} {asset}")
    if tx_hash:
        detail_lines.append(f"Reference: {tx_hash}")
    details_text = ("\n" + "\n".join(detail_lines) + "\n") if detail_lines else ""
    anti_phishing_line = f"Anti-Phishing Code: {anti_phishing_code}\n\n" if anti_phishing_code else ""

    plain_body = (
        f"{anti_phishing_line}"
        f"{message}\n"
        f"{details_text}\n"
        "If you didn't expect this activity, contact support immediately."
    )

    details_rows = "".join(
        f'<tr><td style="padding:4px 0;color:#6b7280;font-size:13px;">{line.split(":")[0]}</td>'
        f'<td style="padding:4px 0;color:#111827;font-size:13px;font-weight:600;text-align:right;">{line.split(":", 1)[1].strip()}</td></tr>'
        for line in detail_lines
    )
    anti_phishing_html = (
        f'<p style="color:#6b7280;font-size:12px;margin:0 0 16px;">Anti-Phishing Code: <strong>{anti_phishing_code}</strong></p>'
        if anti_phishing_code else ""
    )
    html_body = f"""
    <html>
      <body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f8fafc; padding:24px; margin:0;">
        <div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;">
          <div style="padding:24px 24px 0;">
            <p style="font-size:13px;color:#10b981;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;margin:0 0 8px;">Jasiri</p>
            <h2 style="margin:0 0 8px;color:#0f172a;font-size:20px;">{emoji} {title}</h2>
            <p style="color:#475569;font-size:14px;line-height:1.5;margin:0 0 20px;">{message}</p>
          </div>
          {f'<table style="width:100%;padding:0 24px;border-collapse:collapse;">{details_rows}</table>' if detail_lines else ''}
          <div style="padding:20px 24px 24px;">
            {anti_phishing_html}
            <p style="color:#94a3b8;font-size:12px;line-height:1.5;margin:0;">If you didn't expect this activity, contact support immediately.</p>
          </div>
        </div>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
        if tls_enabled:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
    except Exception:
        pass
