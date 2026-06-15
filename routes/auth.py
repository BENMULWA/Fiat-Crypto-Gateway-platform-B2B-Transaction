from fastapi import APIRouter, HTTPException, status, Depends
from bson import ObjectId
from database import get_db
from models.schemas import SignupRequest, LoginRequest, AuthResponse, UserOut
from auth import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_out(user: dict) -> UserOut:
    return UserOut(
        id=str(user["_id"]),
        email=user["email"],
        displayName=user["displayName"],
        role=user["role"],
        workspaceId=str(user["workspaceId"]),
    )


@router.post("/signup", response_model=AuthResponse)
async def signup(req: SignupRequest):
    db = get_db()
    existing = await db.users.find_one({"email": req.email.lower()})
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered.")

    # First user in workspace becomes admin
    count = await db.users.count_documents({})
    role = "admin" if count == 0 else "member"
    workspace_id = ObjectId()

    user_doc = {
        "displayName": req.displayName,
        "email": req.email.lower(),
        "password_hash": hash_password(req.password),
        "role": role,
        "workspaceId": workspace_id,
    }
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id

    token = create_access_token({"sub": str(result.inserted_id)})
    return AuthResponse(access_token=token, user=_user_out(user_doc))


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    db = get_db()
    user = await db.users.find_one({"email": req.email.lower()})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    token = create_access_token({"sub": str(user["_id"])})
    return AuthResponse(access_token=token, user=_user_out(user))
