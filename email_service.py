import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from .utils import RESET_TOKEN_EXPIRE_MINUTES

async def send_password_reset_email(to_email: str, user_name: str, reset_link: str):
    """
    Sends a password reset email using SMTP.
    Falls back to console logging if SendGrid is not configured.
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM_EMAIL")
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "t")

    if not all([smtp_host, smtp_port, smtp_user, smtp_password, smtp_from]):
        print("--- EMAIL SENDING SKIPPED (SMTP_* environment variables not fully set) ---")
        print("--- SENDING PASSWORD RESET EMAIL (SIMULATION) ---")
        print(f"To: {to_email}")
        print("Subject: Reset Your Jasiri Capital Password")
        print(f"Link: {reset_link}")
        print("-------------------------------------------------")
        return

    subject = "Reset Your Jasiri Capital Password"
    html_content = f"""
    <html>
        <body style="font-family: sans-serif; color: #333;">
            <p>Hi {user_name},</p>
            <p>You requested a password reset. Please click the link below to set a new password:</p>
            <p><a href="{reset_link}" style="color: #ffffff; background-color: #10b981; padding: 12px 20px; text-decoration: none; border-radius: 8px; font-weight: bold; display: inline-block;">Reset Password</a></p>
            <p>This link will expire in {RESET_TOKEN_EXPIRE_MINUTES} minutes.</p>
            <p>If you did not request this, please ignore this email.</p>
            <br/>
            <p>Thanks,</p>
            <p>The Jasiri Capital Team</p>
        </body>
    </html>
    """

    msg = MIMEMultipart()
    msg['From'] = smtp_from
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            if smtp_use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        print(f"Password reset email sent to {to_email} via SMTP.")
    except Exception as e:
        print(f"Error sending email via SMTP: {e}")
        raise
