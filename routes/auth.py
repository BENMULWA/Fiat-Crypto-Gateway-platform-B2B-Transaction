import os
from datetime import datetime, timedelta
from typing import Optional
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
        "role": "admin", 
        "workspaceId": workspace_id_str
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
    
    return {
        "access_token": token, 
        "user": {
            "_id": user_doc["_id"], 
            "email": email.lower(), 
            "displayName": display, 
            "role": user_doc["role"], 
            "workspaceId": workspace_id_str,
            "walletAddress": user_doc.get("walletAddress")
        }
    }


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
            "walletAddress": wallet_address
        }
    }
