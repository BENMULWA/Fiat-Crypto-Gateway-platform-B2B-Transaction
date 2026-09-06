import base64
import hashlib
import io
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from uuid import uuid4
import bcrypt
import pyotp
import qrcode
from jose import jwt
from jose.exceptions import JWTError
from fastapi import HTTPException, Security, status, APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from bson import ObjectId

from database import get_db
from config import settings
from cardano.wallet import CardanoWallet, get_or_create_wallet_index

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Setup standard secure token parameters
security = HTTPBearer()
SECRET_KEY = getattr(settings, "jwt_secret", None) or "a-secure-32-character-fallback-key-string"
ALGORITHM = "HS256"


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_admin_role(role: Optional[str]) -> bool:
    """Treat platform roles other than end-user roles as admin-capable."""
    return (role or "").lower() not in {"", "retail", "trader", "user"}


def hash_password(password: str) -> str:
    """Securely hash a raw string password using native bcrypt."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    return hashed_bytes.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check if a raw input password matches the stored database string."""
    try:
        plain_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(plain_bytes, hashed_bytes)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generate a secure cryptographic JWT access token for user authentication session."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=120)  # Session active for 2 hours
        
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def _client_ip(request: Optional[Request]) -> str:
    if not request:
        return ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""

# start creating user session for the user after successful login and return the access token and user payload
async def create_session(db, user_id, request: Optional[Request] = None) -> str:
    """Record a new login session so it can be listed and individually
    revoked later (see GET/DELETE /api/auth/sessions). Auth was previously
    fully stateless JWTs with no server-side record of who was logged in
    where — a "sign out this device" feature is impossible without this.
    Returns the session id to embed as the JWT's `sid` claim."""
    session_id = str(uuid4())
    now = datetime.utcnow()
    await db.sessions.insert_one({
        "_id": session_id,
        "userId": ObjectId(str(user_id)) if ObjectId.is_valid(str(user_id)) else user_id,
        "userAgent": request.headers.get("user-agent", "") if request else "",
        "ip": _client_ip(request),
        "createdAt": now,
        "lastSeenAt": now,
        "revoked": False,
    })
    return session_id


async def _check_session_not_revoked(db, session_id: Optional[str]) -> None:
    """Raises 401 if this session was explicitly revoked (see DELETE
    /api/auth/sessions/{id}). Tokens issued before session tracking existed
    carry no `sid` claim — those are let through unchanged (they'll simply
    expire naturally within their existing 2-hour JWT lifetime) rather than
    force-logging-out everyone the moment this shipped."""
    if not session_id:
        return
    session = await db.sessions.find_one({"_id": session_id}, {"revoked": 1})
    if session and session.get("revoked"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This session has been signed out from another device. Please log in again.",
        )
    if session:
        try:
            await db.sessions.update_one({"_id": session_id}, {"$set": {"lastSeenAt": datetime.utcnow()}})
        except Exception:
            pass


def generate_otp_code(length: int = 6) -> str:
    """Generate a numeric OTP code."""
    alphabet = "0123456789"
    return "".join(alphabet[ord(os.urandom(1)) % len(alphabet)] for _ in range(length))


def send_email_otp(recipient: str, otp_code: str, purpose: str, anti_phishing_code: str = "") -> None:
    """Send OTP to user email via configured SMTP provider."""
    smtp_host = getattr(settings, "smtp_host", "")
    smtp_port = int(getattr(settings, "smtp_port", 587) or 587)
    smtp_user = getattr(settings, "smtp_user", "")
    smtp_pass = getattr(settings, "smtp_password", "")
    sender = getattr(settings, "smtp_from_email", "") or smtp_user
    tls_enabled = bool(getattr(settings, "smtp_use_tls", True))

    if not smtp_host or not sender:
        raise HTTPException(
            status_code=500,
            detail="SMTP is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM_EMAIL.",
        )

    title = "Jasiri account verification code" if purpose == "signup" else "Jasiri secure login code"
    # A user-set phrase (see /api/auth/anti-phishing-code) echoed back in every
    # real security email — a phishing email spoofing Jasiri won't know it,
    # so its absence (or a wrong value) is an at-a-glance tell.
    anti_phishing_line = f"Anti-Phishing Code: {anti_phishing_code}\n\n" if anti_phishing_code else ""
    body = (
        f"{anti_phishing_line}"
        f"Your Jasiri OTP code is: {otp_code}\n\n"
        f"This code expires in {getattr(settings, 'otp_expiry_minutes', 10)} minutes.\n"
        "If you did not request this code, please ignore this email."
    )

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
        if tls_enabled:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(message)
        server.quit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to send OTP email: {exc}")


def send_login_notification_email(recipient: str, anti_phishing_code: str = "") -> None:
    """Best-effort notice that this account was just logged into. Unlike
    send_email_otp, this must never block a successful login on delivery
    failure — it's advisory, not part of the auth flow itself."""
    smtp_host = getattr(settings, "smtp_host", "")
    smtp_port = int(getattr(settings, "smtp_port", 587) or 587)
    smtp_user = getattr(settings, "smtp_user", "")
    smtp_pass = getattr(settings, "smtp_password", "")
    sender = getattr(settings, "smtp_from_email", "") or smtp_user
    tls_enabled = bool(getattr(settings, "smtp_use_tls", True))

    if not smtp_host or not sender or not recipient:
        return

    anti_phishing_line = f"Anti-Phishing Code: {anti_phishing_code}\n\n" if anti_phishing_code else ""
    message = EmailMessage()
    message["Subject"] = "New login to your Jasiri account"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"{anti_phishing_line}"
        f"Your Jasiri account was just signed in to at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
        "If this wasn't you, contact support immediately."
    )

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
        if tls_enabled:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(message)
        server.quit()
    except Exception:
        pass


async def _get_anti_phishing_code(db, email: str) -> str:
    """Best-effort lookup — a signup OTP email has no account/code to embed
    yet, and that's fine, this just returns "" in that case."""
    try:
        user_doc = await db.users.find_one({"email": (email or "").lower()}, {"antiPhishingCode": 1})
        return (user_doc or {}).get("antiPhishingCode", "") or ""
    except Exception:
        return ""


async def create_otp_session(db, purpose: str, email: str, payload: dict) -> tuple[str, int]:
    """Create a short-lived OTP challenge session in database."""
    otp_code = generate_otp_code(getattr(settings, "otp_length", 6))
    otp_hash = hash_password(otp_code)
    expires_minutes = int(getattr(settings, "otp_expiry_minutes", 10))
    session_id = str(uuid4())

    now = datetime.utcnow()

    await db.auth_otps.insert_one({
        "sessionId": session_id,
        "purpose": purpose,
        "email": email.lower(),
        "otpHash": otp_hash,
        "payload": payload,
        "attempts": 0,
        "maxAttempts": int(getattr(settings, "otp_max_attempts", 5)),
        "consumed": False,
        "resendCount": 0,
        "lastSentAt": now,
        "expiresAt": now + timedelta(minutes=expires_minutes),
        "createdAt": now,
    })

    anti_phishing_code = await _get_anti_phishing_code(db, email)
    send_email_otp(email.lower(), otp_code, purpose, anti_phishing_code)
    return session_id, expires_minutes


async def resend_otp_for_session(db, session_id: str, purpose: str) -> dict:
    """Regenerate and resend OTP for an existing challenge session."""
    session = await db.auth_otps.find_one({"sessionId": session_id, "purpose": purpose})
    if not session or session.get("consumed"):
        raise HTTPException(status_code=400, detail="Invalid or used OTP session.")

    now = datetime.utcnow()
    cooldown_seconds = int(getattr(settings, "otp_resend_cooldown_seconds", 30))
    max_resends = int(getattr(settings, "otp_max_resends", 5))

    resend_count = int(session.get("resendCount", 0))
    if resend_count >= max_resends:
        raise HTTPException(status_code=429, detail="Maximum OTP resend attempts reached.")

    last_sent_at = session.get("lastSentAt") or session.get("createdAt")
    if last_sent_at:
        elapsed = int((now - last_sent_at).total_seconds())
        remaining = cooldown_seconds - elapsed
        if remaining > 0:
            raise HTTPException(status_code=429, detail=f"Please wait {remaining}s before requesting another OTP.")

    otp_code = generate_otp_code(getattr(settings, "otp_length", 6))
    otp_hash = hash_password(otp_code)
    expires_minutes = int(getattr(settings, "otp_expiry_minutes", 10))

    # code 
    anti_phishing_code = await _get_anti_phishing_code(db, session.get("email"))
    send_email_otp(session.get("email"), otp_code, purpose, anti_phishing_code)

    await db.auth_otps.update_one(
        {"_id": session["_id"]},
        {
            "$set": {
                "otpHash": otp_hash,
                "expiresAt": now + timedelta(minutes=expires_minutes),
                "lastSentAt": now,
                "attempts": 0,
            },
            "$inc": {"resendCount": 1},
        },
    )

    return {
        "otp_session_id": session_id,
        "expires_in_minutes": expires_minutes,
        "cooldown_seconds": cooldown_seconds,
        "message": "A new OTP has been sent to your email.",
    }


async def validate_otp_session(db, session_id: str, otp_code: str, purpose: str) -> dict:
    """Validate OTP against session and return the underlying session doc."""
    session = await db.auth_otps.find_one({"sessionId": session_id, "purpose": purpose})
    if not session or session.get("consumed"):
        raise HTTPException(status_code=400, detail="Invalid or used OTP session.")

    if datetime.utcnow() > session.get("expiresAt"):
        raise HTTPException(status_code=400, detail="OTP session expired. Request a new code.")

    max_attempts = int(session.get("maxAttempts", 5))
    attempts = int(session.get("attempts", 0))
    if attempts >= max_attempts:
        raise HTTPException(status_code=429, detail="Too many OTP attempts. Request a new code.")

    if not verify_password(otp_code, session.get("otpHash", "")):
        await db.auth_otps.update_one({"_id": session["_id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Invalid OTP code.")

    return session


async def issue_auth_payload(db, user_doc: dict, request: Optional[Request] = None) -> dict:
    """Issue access token and normalized user payload."""
    user_id = str(user_doc.get("_id"))
    workspace_id_str = user_doc.get("workspaceId") or str(ObjectId())
    wallet_address = user_doc.get("walletAddress")

    if not wallet_address:
        try:
            account_index = await get_or_create_wallet_index(db, workspace_id_str)
            wallet = CardanoWallet(account_index)
            wallet_address = wallet.address_str
            await db.users.update_one(
                {"_id": user_doc.get("_id")},
                {"$set": {"walletAddress": wallet_address, "accountIndex": int(account_index), "workspaceId": workspace_id_str}},
            )
        except Exception:
            wallet_address = None

    session_id = await create_session(db, user_id, request)
    token = create_access_token({
        "sub": user_id,
        "workspaceId": workspace_id_str,
        "walletAddress": wallet_address,
        "sid": session_id,
    })

    return {
        "access_token": token,
        "user": {
            "_id": user_id,
            "email": user_doc.get("email"),
            "displayName": user_doc.get("displayName"),
            "role": user_doc.get("role", "retail"),
            "workspaceId": workspace_id_str,
            "walletAddress": wallet_address,
            "kycStatus": user_doc.get("kycStatus", "unverified"),
            "avatarUrl": user_doc.get("avatarUrl"),
        },
    }


async def get_current_user_with_role(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db=Depends(get_db),
) -> dict:
    """Resolve authenticated user and fetch persisted role for admin-only actions."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        session_id = payload.get("sid")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token context.")

        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user_doc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User session is no longer valid.")
    except HTTPException:
        raise
    except (JWTError, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired. Please log in again.",
        )

    # Outside the try/except above so a revoked-session 401 (see
    # DELETE /api/auth/sessions/{id}) isn't swallowed by its broad `except
    # Exception` and rewritten into a generic message.
    await _check_session_not_revoked(db, session_id)

    return {
        "_id": str(user_doc.get("_id")),
        "email": user_doc.get("email"),
        "displayName": user_doc.get("displayName") or user_doc.get("name"),
        "role": user_doc.get("role", "retail"),
        "workspaceId": user_doc.get("workspaceId"),
        "walletAddress": user_doc.get("walletAddress"),
        "kycStatus": user_doc.get("kycStatus", "unverified"),
        "avatarUrl": user_doc.get("avatarUrl"),
        "sessionId": session_id,
    }


# ── FIX 1: Fixed Middleware to correctly extract and enforce unique workspaceIds ──
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db=Depends(get_db),
) -> dict:
    """Middleware gate that decodes token payload headers to identify the active user workspace."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        workspace_id = payload.get("workspaceId")
        session_id = payload.get("sid")

        if user_id is None or workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token context: user identity or workspace index missing.",
            )
    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired. Please log in again.",
        )

    # Every withdrawal/deposit/wallet route depends on get_current_user (not
    # get_current_user_with_role), so the revocation check has to live here
    # too, or "sign out this device" would only affect admin-only endpoints.
    await _check_session_not_revoked(db, session_id)

    # Returns the actual distinct workspace data back to your cardano routes!
    return {"_id": user_id, "workspaceId": workspace_id, "sessionId": session_id}


async def get_verified_current_user(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    """Require approved KYC before an end user can move value."""
    try:
        user_id = ObjectId(str(current_user.get("_id")))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user identity.") from exc

    user_doc = await db.users.find_one({"_id": user_id}, {"role": 1, "kycStatus": 1})
    if not user_doc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User session is no longer valid.")

    role = user_doc.get("role", "retail")
    kyc_status = str(user_doc.get("kycStatus", "unverified")).strip().lower()
    if not is_admin_role(role) and kyc_status not in {"verified", "approved"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Complete and receive approval for KYC before moving funds.")

    return {**current_user, "role": role, "kycStatus": kyc_status}


# --- Auth routes (signup / login) ------------------------------------------


@router.post("/signup")
async def signup(data: dict, request: Request, db=Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    display = data.get("displayName") or data.get("display") or email
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")

    users = db.users
    existing = await users.find_one({"email": email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    hashed = hash_password(password)
    
    # Generate a fresh workspace string link uniquely anchored to this specific business registration
    workspace_id_str = str(ObjectId())
    
    user_doc = {
        "email": email.lower(), 
        "password": hashed, 
        "displayName": display, 
        "role": "retail", 
        "workspaceId": workspace_id_str,
        "kycStatus": "unverified",
        "createdAt": datetime.utcnow(),
    }
    res = await users.insert_one(user_doc)
    user_doc["_id"] = str(res.inserted_id)

    # Create or reserve a Cardano account index for this workspace and derive an address
    try:
        account_index = await get_or_create_wallet_index(db, workspace_id_str)
        wallet = CardanoWallet(account_index)
        wallet_address = wallet.address_str
        # persist wallet address and account index on the user doc
        await users.update_one({"_id": res.inserted_id}, {"$set": {"walletAddress": wallet_address, "accountIndex": int(account_index)}})
        user_doc["walletAddress"] = wallet_address
        user_doc["accountIndex"] = int(account_index)
    except Exception:
        # If wallet creation fails, continue without blocking signup but don't attach an address
        user_doc["walletAddress"] = None

    # ── FIX 2: Explicitly baking workspace details directly into the token payload string block ──
    session_id = await create_session(db, user_doc["_id"], request)
    token = create_access_token({
        "sub": user_doc["_id"],
        "workspaceId": workspace_id_str,
        "walletAddress": user_doc.get("walletAddress"),
        "sid": session_id,
    })
    # Initialize an empty retail wallet for this user with all supported assets
    try:
        # All supported assets in the platform (must match retail.py SUPPORTED_ASSETS)
        SUPPORTED_ASSETS = [
            "KES", "USDA", "USDT", "USDC", "cUSD", "USD", 
            "UGX", "TZS", "RWF", "BIF", "XAF", "XOF", 
            "AIRT", "IMP", "BTC", "ETH"
        ]
        wallet_init = {"userId": user_doc["_id"]}
        for asset in SUPPORTED_ASSETS:
            wallet_init[asset] = 0.0
        
        await db["retail_wallets"].insert_one(wallet_init)
    except Exception:
        pass
    
    return {
        "access_token": token, 
        "user": {
            "_id": user_doc["_id"], 
            "email": email.lower(), 
            "displayName": display, 
            "role": user_doc["role"], 
            "workspaceId": workspace_id_str,
            "walletAddress": user_doc.get("walletAddress"),
            "kycStatus": user_doc.get("kycStatus", "unverified"),
            "avatarUrl": user_doc.get("avatarUrl"),
        }
    }


@router.post("/signup/request-otp")
async def signup_request_otp(data: dict, db=Depends(get_db)):
    """Start signup OTP challenge by validating input and emailing code."""
    email = normalize_email(data.get("email") or "")
    password = data.get("password") or ""
    display = data.get("displayName") or data.get("display") or email

    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")


    # creates session after the user has entered the balid diplay name, email and password but user not yet added to db.
    session_id, expires_minutes = await create_otp_session(
        db,
        purpose="signup",
        email=email,
        payload={
            "displayName": display,
            "passwordHash": hash_password(password),
        },
    )

    return {
        "otp_session_id": session_id,
        "expires_in_minutes": expires_minutes,
        "message": "OTP sent to your email.",
    }


@router.post("/signup/verify-otp")
async def signup_verify_otp(data: dict, request: Request, db=Depends(get_db)):
    """Complete signup after OTP verification and issue token."""
    session_id = data.get("otp_session_id")
    otp_code = str(data.get("otp_code") or "")
    if not session_id or not otp_code:
        raise HTTPException(status_code=400, detail="otp_session_id and otp_code required")

    session = await validate_otp_session(db, session_id, otp_code, purpose="signup")
    email = session.get("email")
    payload = session.get("payload") or {}

    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    workspace_id_str = str(ObjectId())
    user_doc = {
        "email": email,
        "password": payload.get("passwordHash"),
        "displayName": payload.get("displayName") or email,
        "role": "retail",
        "workspaceId": workspace_id_str,
        "kycStatus": "unverified",
        "emailVerified": True,
        "createdAt": datetime.utcnow(),
    }
    res = await db.users.insert_one(user_doc)
    user_doc["_id"] = res.inserted_id

    try:
        await db["retail_wallets"].insert_one({
            "userId": str(res.inserted_id),
            "KES": 0.0,
            "USDA": 0.0,
            "IMP": 0.0,
            "createdAt": datetime.utcnow()
        })
    except Exception:
        pass

    await db.auth_otps.update_one({"_id": session["_id"]}, {"$set": {"consumed": True, "consumedAt": datetime.utcnow()}})

    return await issue_auth_payload(db, user_doc, request)

# Resend OTP routes for both signup and login, with cooldown and max resend limits enforced.
@router.post("/signup/resend-otp")
async def signup_resend_otp(data: dict, db=Depends(get_db)):
    session_id = data.get("otp_session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="otp_session_id required")

    return await resend_otp_for_session(db, session_id, purpose="signup")


@router.post("/login")
async def login(data: dict, request: Request, db=Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")

    users = db.users
    user = await users.find_one({"email": email.lower()})
    if not user or not verify_password(password, user.get("password", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    uid = str(user.get("_id"))
    
    # Read the distinct workspace link from database, fallback to a fallback only if data is historic
    workspace = user.get("workspaceId") or str(ObjectId())
    # Ensure user has a wallet address; if missing try to create one on-the-fly
    wallet_address = user.get("walletAddress")
    if not wallet_address:
        try:
            account_index = await get_or_create_wallet_index(db, workspace)
            wallet = CardanoWallet(account_index)
            wallet_address = wallet.address_str
            await users.update_one({"_id": user.get("_id")}, {"$set": {"walletAddress": wallet_address, "accountIndex": int(account_index)}})
        except Exception:
            wallet_address = None
    
    # ── FIX 3: Fully include actual user workspace properties inside login signature session token ──
    session_id = await create_session(db, uid, request)
    token = create_access_token({
        "sub": uid,
        "workspaceId": workspace,
        "walletAddress": wallet_address,
        "sid": session_id,
    })

    send_login_notification_email(user.get("email"), user.get("antiPhishingCode", ""))

    return {
        "access_token": token, 
        "user": {
            "_id": uid, 
            "email": user.get("email"), 
            "displayName": user.get("displayName"), 
            "role": user.get("role", "user"), 
            "workspaceId": workspace,
            "walletAddress": wallet_address,
            "kycStatus": user.get("kycStatus", "unverified"),
            "avatarUrl": user.get("avatarUrl"),
        }
    }


@router.post("/login/request-otp")
async def login_request_otp(data: dict, db=Depends(get_db)):
    """Start login OTP challenge after verifying password."""
    email = normalize_email(data.get("email") or "")
    password = data.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")

    user = await db.users.find_one({"email": email})
    if not user or not verify_password(password, user.get("password", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_id, expires_minutes = await create_otp_session(
        db,
        purpose="login",
        email=email,
        payload={"userId": str(user.get("_id"))},
    )

    return {
        "otp_session_id": session_id,
        "expires_in_minutes": expires_minutes,
        "message": "OTP sent to your email.",
    }


@router.post("/login/verify-otp")
async def login_verify_otp(data: dict, request: Request, db=Depends(get_db)):
    """Complete login after OTP verification and issue token."""
    session_id = data.get("otp_session_id")
    otp_code = str(data.get("otp_code") or "")
    if not session_id or not otp_code:
        raise HTTPException(status_code=400, detail="otp_session_id and otp_code required")

    session = await validate_otp_session(db, session_id, otp_code, purpose="login")
    payload = session.get("payload") or {}
    user_id = payload.get("userId")
    email = session.get("email")

    user = None
    if user_id:
        try:
            user = await db.users.find_one({"_id": ObjectId(user_id)})
        except Exception:
            user = None
    if not user and email:
        user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Successful OTP proves current email inbox ownership; close any pending recovery lock.
    await db.users.update_one(
        {"_id": user.get("_id")},
        {
            "$set": {
                "emailVerified": True,
                "recoveryRequired": False,
                "withdrawalsBlocked": False,
                "recoveryClosedAt": datetime.utcnow(),
            }
        },
    )

    await db.auth_otps.update_one({"_id": session["_id"]}, {"$set": {"consumed": True, "consumedAt": datetime.utcnow()}})
    send_login_notification_email(user.get("email"), user.get("antiPhishingCode", ""))
    return await issue_auth_payload(db, user, request)


@router.post("/login/resend-otp")
async def login_resend_otp(data: dict, db=Depends(get_db)):
    session_id = data.get("otp_session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="otp_session_id required")

    return await resend_otp_for_session(db, session_id, purpose="login")


#=============================================
# Forgot / reset password — link-based, not OTP-code-based (see
# ForgotPasswordPage.tsx / ResetPasswordPage.tsx: the reset link carries a
# `?token=` query param, not a 6-digit code). This was previously frontend-only
# scaffolding with no backend behind it — both endpoints 404'd, so a user who
# forgot their password had no way back into their account.
#=============================================

def _hash_reset_token(token: str) -> str:
    # The token itself is the secret (256 bits from secrets.token_urlsafe),
    # so a fast deterministic hash is appropriate here — unlike passwords,
    # there's no "slow it down for attackers" purpose that requires bcrypt,
    # and a deterministic hash is what makes an O(1) lookup by hash possible.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/forgot-password")
async def forgot_password(data: dict, db=Depends(get_db)):
    """Start a password-reset flow. Always returns the same generic response
    whether or not the email matches an account — ForgotPasswordPage.tsx
    already assumes this (it shows success even when the request errors) to
    avoid leaking which emails are registered."""
    generic_response = {
        "status": "success",
        "message": "If an account exists for this email, a reset link has been sent.",
    }
    email = normalize_email(data.get("email") or "")
    if not email:
        return generic_response

    user = await db.users.find_one({"email": email})
    if not user:
        return generic_response

    raw_token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires_minutes = int(getattr(settings, "password_reset_expiry_minutes", 30))

    await db.password_reset_tokens.insert_one({
        "tokenHash": _hash_reset_token(raw_token),
        "userId": user["_id"],
        "email": email,
        "consumed": False,
        "createdAt": now,
        "expiresAt": now + timedelta(minutes=expires_minutes),
    })

    reset_link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"
    smtp_host = getattr(settings, "smtp_host", "")
    sender = getattr(settings, "smtp_from_email", "") or getattr(settings, "smtp_user", "")
   
   # email message to user rest account.
    if smtp_host and sender:
        message = EmailMessage()
        message["Subject"] = "Reset your Jasiri password"
        message["From"] = sender
        message["To"] = email
        anti_phishing_line = f"Anti-Phishing Code: {user.get('antiPhishingCode')}\n\n" if user.get("antiPhishingCode") else ""
        message.set_content(
            f"{anti_phishing_line}"
            f"We received a request to reset your Jasiri password.\n\n"
            f"Reset it here: {reset_link}\n\n"
            f"This link expires in {expires_minutes} minutes. If you didn't request this, ignore this email — your password will stay unchanged."
        )
        try:
            server = smtplib.SMTP(smtp_host, int(getattr(settings, "smtp_port", 587) or 587), timeout=20)
            if bool(getattr(settings, "smtp_use_tls", True)):
                server.starttls()
            smtp_user = getattr(settings, "smtp_user", "")
            if smtp_user:
                server.login(smtp_user, getattr(settings, "smtp_password", ""))
            server.send_message(message)
            server.quit()
        except Exception:
            # Best-effort, same as send_login_notification_email — a delivery
            # failure must not leak through the generic response either.
            pass

    return generic_response


@router.post("/reset-password")
async def reset_password(data: dict, db=Depends(get_db)):
    """Complete a password reset using the token from the emailed link."""
    token = str(data.get("token") or "").strip()
    new_password = str(data.get("new_password") or "")

    if not token:
        raise HTTPException(status_code=400, detail="Reset token is required.")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if len(new_password) > 72:
        raise HTTPException(status_code=400, detail="Password cannot be longer than 72 characters.")

    record = await db.password_reset_tokens.find_one({"tokenHash": _hash_reset_token(token), "consumed": False})
    if not record or record.get("expiresAt") < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired reset link. Please request a new one.")

    await db.users.update_one({"_id": record["userId"]}, {"$set": {"password": hash_password(new_password)}})
    await db.password_reset_tokens.update_one({"_id": record["_id"]}, {"$set": {"consumed": True, "consumedAt": datetime.utcnow()}})

    # Best-effort security notice, same non-blocking pattern as
    # send_login_notification_email — a changed password is worth alerting on
    # in case the reset wasn't actually the account owner.
    smtp_host = getattr(settings, "smtp_host", "")
    sender = getattr(settings, "smtp_from_email", "") or getattr(settings, "smtp_user", "")
    if smtp_host and sender and record.get("email"):
        try:
            reset_user = await db.users.find_one({"_id": record["userId"]}, {"antiPhishingCode": 1})
            anti_phishing_line = f"Anti-Phishing Code: {reset_user.get('antiPhishingCode')}\n\n" if reset_user and reset_user.get("antiPhishingCode") else ""
            message = EmailMessage()
            message["Subject"] = "Your Jasiri password was changed"
            message["From"] = sender
            message["To"] = record["email"]
            message.set_content(
                f"{anti_phishing_line}"
                f"Your Jasiri account password reset was just changed at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
                "If this wasn't you, contact support immediately 0714073826"
            )
            server = smtplib.SMTP(smtp_host, int(getattr(settings, "smtp_port", 587) or 587), timeout=20)
            if bool(getattr(settings, "smtp_use_tls", True)):
                server.starttls()
            smtp_user = getattr(settings, "smtp_user", "")
            if smtp_user:
                server.login(smtp_user, getattr(settings, "smtp_password", ""))
            server.send_message(message)
            server.quit()
        except Exception:
            pass

    return {"status": "success", "message": "Password has been reset successfully."}


@router.post("/admin/recovery/change-email")
async def admin_recovery_change_email(
    data: dict,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Admin-only account recovery: migrate user to a reachable email for OTP login."""
    if not is_admin_role(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Admin role required")

    # The payload structure 
    current_email = normalize_email(data.get("current_email") or "")
    new_email = normalize_email(data.get("new_email") or "")
    reason = (data.get("reason") or "invalid_or_unreachable_email").strip()

    if not current_email or not new_email:
        raise HTTPException(status_code=400, detail="current_email and new_email required")
    if current_email == new_email:
        raise HTTPException(status_code=400, detail="new_email must be different from current_email")

    user = await db.users.find_one({"email": current_email})
    if not user:
        raise HTTPException(status_code=404, detail="Target user not found")

    existing_new = await db.users.find_one({"email": new_email})
    if existing_new and str(existing_new.get("_id")) != str(user.get("_id")):
        raise HTTPException(status_code=409, detail="new_email is already in use")

    now = datetime.utcnow()

    await db.users.update_one(
        {"_id": user.get("_id")},
        {
            "$set": {
                "email": new_email,
                "emailVerified": False,
                "recoveryRequired": True,
                "withdrawalsBlocked": True,
                "recoveryUpdatedAt": now,
                "recoveryUpdatedBy": current_user.get("_id"),
                "recoveryReason": reason,
            }
        },
    )

    otp_res = await db.auth_otps.update_many(
        {
            "email": {"$in": [current_email, new_email]},
            "consumed": False,
        },
        {
            "$set": {
                "consumed": True,
                "consumedAt": now,
                "invalidatedReason": "email_recovery",
            }
        },
    )

    try:
        await db.auth_recovery_audit.insert_one(
            {
                "targetUserId": str(user.get("_id")),
                "workspaceId": user.get("workspaceId"),
                "oldEmail": current_email,
                "newEmail": new_email,
                "reason": reason,
                "invalidatedOtpCount": int(otp_res.modified_count),
                "performedBy": current_user.get("_id"),
                "performedByRole": current_user.get("role"),
                "performedAt": now,
            }
        )
    except Exception:
        # Keep recovery flow operational even if audit write fails; user lock state remains secure.
        pass

    return {
        "message": "Recovery email updated. User must verify OTP on new email before high-risk actions.",
        "user_id": str(user.get("_id")),
        "old_email": current_email,
        "new_email": new_email,
        "recovery_required": True,
        "withdrawals_blocked": True,
        "invalidated_otp_sessions": int(otp_res.modified_count),
    }


#=============================================
# Withdrawal security: emailed OTP + optional TOTP authenticator app.
# See two_factor.py's verify_withdrawal_2fa, called by every withdrawal
# endpoint (ramp.py, cardano.py, valora.py, treasury.py) before any funds move.
#=============================================

@router.post("/withdrawal/request-otp")
async def withdrawal_request_otp(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Sends the verification code a user must enter to complete a withdrawal.
    An authenticator app is mandatory for withdrawals (see two_factor.py) —
    blocked here too, before even sending an email, so an unenrolled account
    can't get partway through the flow only to be rejected at the final step."""
    email = current_user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="No email on file for this account.")

    # Check if the user has TOTP enabled; if not, block withdrawal OTP request
    user_id = ObjectId(str(current_user.get("_id")))
    user_doc = await db.users.find_one({"_id": user_id}, {"totpEnabled": 1})
    if not user_doc or not user_doc.get("totpEnabled"):
        raise HTTPException(
            status_code=428,
            detail="Set up an authenticator app before making withdrawals. Go to Profile > Security to enable it.",
        )

    session_id, expires_minutes = await create_otp_session(
        db,
        purpose="withdrawal",
        email=email,
        payload={"userId": str(current_user.get("_id"))},
    )
    return {
        "otp_session_id": session_id,
        "expires_in_minutes": expires_minutes,
        "message": "A verification code has been sent to your email.",
    }


@router.post("/2fa/totp/setup")
async def totp_setup(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Generates a new TOTP secret and its enrollment QR code. Not active yet —
    stored as `totpSecretPending` until the user proves they can generate a
    valid code via /2fa/totp/verify-setup, so a botched scan can't lock
    withdrawals behind a secret nobody's authenticator app actually has."""
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=current_user.get("email") or str(current_user.get("_id")),
        issuer_name="Jasiri",
    )

    qr_img = qrcode.make(uri)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    user_id = ObjectId(str(current_user.get("_id")))
    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"totpSecretPending": secret}},
    )

    return {"secret": secret, "qrCode": qr_data_uri, "otpauthUri": uri}


@router.post("/2fa/totp/verify-setup")
async def totp_verify_setup(
    data: dict,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    code = str(data.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")

    user_id = ObjectId(str(current_user.get("_id")))
    user_doc = await db.users.find_one({"_id": user_id}, {"totpSecretPending": 1})
    pending_secret = user_doc.get("totpSecretPending") if user_doc else None
    if not pending_secret:
        raise HTTPException(status_code=400, detail="No pending authenticator setup. Call /2fa/totp/setup first.")

    if not pyotp.TOTP(pending_secret).verify(code, valid_window=2):
        raise HTTPException(status_code=400, detail="Invalid code. Check your authenticator app and try again.")

    await db.users.update_one(
        {"_id": user_id},
        {
            "$set": {"totpSecret": pending_secret, "totpEnabled": True, "totpEnabledAt": datetime.utcnow()},
            "$unset": {"totpSecretPending": ""},
        },
    )
    return {"status": "success", "message": "Authenticator app enabled for withdrawals."}


@router.post("/2fa/totp/disable")
async def totp_disable(
    data: dict,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Requires a currently-valid TOTP code to disable — otherwise a stolen
    session token alone could turn off withdrawal 2FA."""
    code = str(data.get("code") or "").strip()
    user_id = ObjectId(str(current_user.get("_id")))
    user_doc = await db.users.find_one({"_id": user_id}, {"totpSecret": 1, "totpEnabled": 1})
    if not user_doc or not user_doc.get("totpEnabled"):
        raise HTTPException(status_code=400, detail="Authenticator app is not enabled.")
    if not code or not pyotp.TOTP(user_doc["totpSecret"]).verify(code, valid_window=2):
        raise HTTPException(status_code=400, detail="Invalid authenticator code.")

    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"totpEnabled": False}, "$unset": {"totpSecret": ""}},
    )
    return {"status": "success", "message": "Authenticator app disabled."}


@router.get("/2fa/status")
async def totp_status(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    user_id = ObjectId(str(current_user.get("_id")))
    user_doc = await db.users.find_one({"_id": user_id}, {"totpEnabled": 1})
    return {"totpEnabled": bool(user_doc.get("totpEnabled")) if user_doc else False}


#=============================================
# Anti-phishing code: a user-set phrase echoed back in every real security
# email (OTP, login alert, password reset). A phishing email spoofing Jasiri
# won't know it, so its absence — or a wrong value — is an at-a-glance tell.
# See _get_anti_phishing_code and every send_*_email call site above.
#=============================================

ANTI_PHISHING_CODE_PATTERN = re.compile(r"^[A-Za-z0-9 _\-]{1,20}$")


@router.get("/anti-phishing-code")
async def get_anti_phishing_code(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    user_id = ObjectId(str(current_user.get("_id")))
    user_doc = await db.users.find_one({"_id": user_id}, {"antiPhishingCode": 1})
    return {"antiPhishingCode": (user_doc or {}).get("antiPhishingCode", "")}


@router.post("/anti-phishing-code")
async def set_anti_phishing_code(
    data: dict,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    code = str(data.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="A code is required.")
    if not ANTI_PHISHING_CODE_PATTERN.match(code):
        raise HTTPException(status_code=400, detail="Use 1-20 letters, numbers, spaces, hyphens, or underscores.")

    user_id = ObjectId(str(current_user.get("_id")))
    await db.users.update_one({"_id": user_id}, {"$set": {"antiPhishingCode": code}})
    return {"status": "success", "antiPhishingCode": code}


#=============================================
# Avatar upload — stored as a base64 data URI directly on the user document,
# the same pattern already used for KYC identity documents
# (routes/retail.py's kycDetails.documentDataUrl). No new file-storage
# infrastructure (S3, etc.) needed for something this small.
#=============================================

MAX_AVATAR_BYTES = 1_500_000  # ~1.5MB decoded; keeps user docs well under MongoDB's 16MB limit
ALLOWED_AVATAR_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@router.post("/avatar")
async def upload_avatar(
    data: dict,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    data_url = str(data.get("dataUrl") or data.get("data_url") or "").strip()
    if not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="Expected a data URI (data:image/...;base64,...).")

    try:
        header, b64_payload = data_url.split(",", 1)
        mime_type = header.split(";")[0].removeprefix("data:")
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed data URI.")

    if mime_type not in ALLOWED_AVATAR_MIME_TYPES:
        raise HTTPException(status_code=400, detail="Avatar must be a PNG, JPEG, WEBP, or GIF image.")

    # Base64 encodes 3 bytes as 4 chars, so this approximates decoded size
    # without actually decoding — cheap to reject an oversized upload early.
    approx_decoded_bytes = len(b64_payload) * 3 / 4
    if approx_decoded_bytes > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Avatar image is too large (max 1.5MB).")

    user_id = ObjectId(str(current_user.get("_id")))
    await db.users.update_one({"_id": user_id}, {"$set": {"avatarUrl": data_url}})
    return {"status": "success", "avatarUrl": data_url}


@router.delete("/avatar")
async def delete_avatar(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    user_id = ObjectId(str(current_user.get("_id")))
    await db.users.update_one({"_id": user_id}, {"$unset": {"avatarUrl": ""}})
    return {"status": "success"}


#=============================================
# Active sessions / device management — list and individually revoke logins.
# Only possible because create_session/_check_session_not_revoked now give
# JWT auth a server-side record; a stateless JWT alone can't be "signed out"
# before it naturally expires.
#=============================================

@router.get("/sessions")
async def list_sessions(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    user_id = ObjectId(str(current_user.get("_id")))
    current_session_id = current_user.get("sessionId")
    cursor = db.sessions.find({"userId": user_id, "revoked": False}).sort("lastSeenAt", -1)
    sessions = await cursor.to_list(length=100)
    return {
        "sessions": [
            {
                "id": s["_id"],
                "userAgent": s.get("userAgent", ""),
                "ip": s.get("ip", ""),
                "createdAt": s.get("createdAt").isoformat() + "Z" if s.get("createdAt") else None,
                "lastSeenAt": s.get("lastSeenAt").isoformat() + "Z" if s.get("lastSeenAt") else None,
                "current": s["_id"] == current_session_id,
            }
            for s in sessions
        ]
    }


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Sign out one device/session. Revoking the current session is allowed —
    that's just "log out" — the frontend should clear its own stored token
    when it does this to itself."""
    user_id = ObjectId(str(current_user.get("_id")))
    result = await db.sessions.update_one(
        {"_id": session_id, "userId": user_id},
        {"$set": {"revoked": True, "revokedAt": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"status": "success"}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    current_user: dict = Depends(get_current_user_with_role),
    db=Depends(get_db),
):
    """Sign out every device except the one making this request."""
    user_id = ObjectId(str(current_user.get("_id")))
    current_session_id = current_user.get("sessionId")
    result = await db.sessions.update_many(
        {"userId": user_id, "revoked": False, "_id": {"$ne": current_session_id}},
        {"$set": {"revoked": True, "revokedAt": datetime.utcnow()}},
    )
    return {"status": "success", "revokedCount": result.modified_count}


#=============================================
# Checks iff the user is still logged in when they refrsh the page.
#=============================================

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user_with_role)):
    """Return fresh persisted session data, including latest KYC approval."""
    return {"user": current_user}
