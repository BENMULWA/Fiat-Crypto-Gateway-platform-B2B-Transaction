import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from uuid import uuid4
import bcrypt
from jose import jwt
from jose.exceptions import JWTError
from fastapi import HTTPException, Security, status, APIRouter, Depends
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


def generate_otp_code(length: int = 6) -> str:
    """Generate a numeric OTP code."""
    alphabet = "0123456789"
    return "".join(alphabet[ord(os.urandom(1)) % len(alphabet)] for _ in range(length))


def send_email_otp(recipient: str, otp_code: str, purpose: str) -> None:
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
    body = (
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

    send_email_otp(email.lower(), otp_code, purpose)
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

    send_email_otp(session.get("email"), otp_code, purpose)

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


async def issue_auth_payload(db, user_doc: dict) -> dict:
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

    token = create_access_token({
        "sub": user_id,
        "workspaceId": workspace_id_str,
        "walletAddress": wallet_address,
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
            "kycStatus": user_doc.get("kycStatus", "pending"),
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
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token context.")

        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user_doc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User session is no longer valid.")

        return {
            "_id": str(user_doc.get("_id")),
            "email": user_doc.get("email"),
            "role": user_doc.get("role", "retail"),
            "workspaceId": user_doc.get("workspaceId"),
        }
    except (JWTError, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired. Please log in again.",
        )


# ── FIX 1: Fixed Middleware to correctly extract and enforce unique workspaceIds ──
async def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    """Middleware gate that decodes token payload headers to identify the active user workspace."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        workspace_id = payload.get("workspaceId")
        
        if user_id is None or workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token context: user identity or workspace index missing.",
            )
            
        # Returns the actual distinct workspace data back to your cardano routes!
        return {"_id": user_id, "workspaceId": workspace_id}
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired. Please log in again.",
        )


# --- Auth routes (signup / login) ------------------------------------------


@router.post("/signup")
async def signup(data: dict, db=Depends(get_db)):
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
        "kycStatus": "pending",
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
    token = create_access_token({
        "sub": user_doc["_id"], 
        "workspaceId": workspace_id_str,
        "walletAddress": user_doc.get("walletAddress")
    })
    # Initialize an empty retail wallet for this user
    try:
        await db["retail_wallets"].insert_one({
            "userId": user_doc["_id"],
            "KES": 0.0,
            "USDA": 0.0,
            "IMP": 0.0
        })
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
            "kycStatus": user_doc.get("kycStatus", "pending")
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
async def signup_verify_otp(data: dict, db=Depends(get_db)):
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
        "kycStatus": "pending",
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

    return await issue_auth_payload(db, user_doc)

# Resend OTP routes for both signup and login, with cooldown and max resend limits enforced.
@router.post("/signup/resend-otp")
async def signup_resend_otp(data: dict, db=Depends(get_db)):
    session_id = data.get("otp_session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="otp_session_id required")

    return await resend_otp_for_session(db, session_id, purpose="signup")


@router.post("/login")
async def login(data: dict, db=Depends(get_db)):
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
    token = create_access_token({
        "sub": uid, 
        "workspaceId": workspace,
        "walletAddress": wallet_address
    })
    
    return {
        "access_token": token, 
        "user": {
            "_id": uid, 
            "email": user.get("email"), 
            "displayName": user.get("displayName"), 
            "role": user.get("role", "user"), 
            "workspaceId": workspace,
            "walletAddress": wallet_address,
            "kycStatus": user.get("kycStatus", "pending")
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
async def login_verify_otp(data: dict, db=Depends(get_db)):
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
    return await issue_auth_payload(db, user)


@router.post("/login/resend-otp")
async def login_resend_otp(data: dict, db=Depends(get_db)):
    session_id = data.get("otp_session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="otp_session_id required")

    return await resend_otp_for_session(db, session_id, purpose="login")


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
# Checks iff the user is still logged in when they refrsh the page.
#=============================================

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Returns the current user's data based on their token"""
    return {"user": current_user}