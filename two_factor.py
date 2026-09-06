"""Withdrawal security: an emailed one-time code plus a mandatory TOTP
authenticator-app code, checked before any withdrawal moves funds — the same
two-factor pattern Binance/Bybit/Coinstore use.

The email code reuses the existing OTP session machinery in routes/auth.py
(the same mechanism login already uses) rather than inventing a second one.
TOTP is genuinely new: a per-user secret stored on the user document, enrolled
via QR code, verified with pyotp.

TOTP enrollment is mandatory for withdrawals, not opt-in — an account with no
authenticator app configured cannot withdraw at all. Both the request-otp
endpoint (routes/auth.py) and this function enforce that, so it can't be
bypassed by calling either API directly.
"""
from datetime import datetime

import pyotp

from fastapi import HTTPException

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None


def safe_object_id(val):
    if ObjectId and isinstance(val, str) and len(val) == 24:
        try:
            return ObjectId(val)
        except Exception:
            pass
    return val


# Function to define the 2fa veridication for all withdrwals, including cardano and ramp off-ramps.
# This is called before any balance is debited or provider call is made — a failed check must never have already moved funds.
async def verify_withdrawal_2fa(
    db,
    current_user: dict,
    otp_session_id: str,
    otp_code: str,
    totp_code: str | None = None,
) -> None:
    
    """Raises HTTPException if verification fails. Must be called before any
    balance is debited or provider call is made — a failed check must never
    have already moved funds.
    """
    # Imported here, not at module load, to avoid a circular import: routes.auth
    # is imported by nearly every route module, so importing two_factor.py
    # from routes.auth (which it doesn't, but easily could in the future)
    # would deadlock the import graph. This keeps the dependency one-directional.
    from routes.auth import validate_otp_session

    if not otp_session_id or not otp_code:
        raise HTTPException(status_code=400, detail="Email verification code is required for withdrawals.")

    user_id = safe_object_id(str(current_user.get("_id")))
    user_doc = await db["users"].find_one({"_id": user_id}, {"totpEnabled": 1, "totpSecret": 1})

    if not user_doc or not user_doc.get("totpEnabled") or not user_doc.get("totpSecret"):
        # 428 (not 400/403) so the frontend can distinguish "you haven't set
        # this up yet" from "you got a code wrong" and redirect to enrollment
        # instead of just showing an error.
        raise HTTPException(
            status_code=428,
            detail="Set up an authenticator app before making withdrawals. Go to Profile > Security to enable it.",
        )

    session = await validate_otp_session(db, otp_session_id, otp_code, purpose="withdrawal")

    if not totp_code:
        raise HTTPException(status_code=400, detail="Authenticator app code is required for withdrawals.")
    totp = pyotp.TOTP(user_doc["totpSecret"])
    if not totp.verify(str(totp_code).strip(), valid_window=2):
        raise HTTPException(status_code=400, detail="Invalid authenticator code.")

    # Consume the OTP session only after every check passes, mirroring the
    # login/signup OTP flows in routes/auth.py — a session must stay usable
    # if the TOTP step that follows it fails, so the user isn't forced to
    # request an entirely new email code just because they mistyped the app code.
    await db["auth_otps"].update_one(
        {"_id": session["_id"]},
        {"$set": {"consumed": True, "consumedAt": datetime.utcnow()}},
    )
