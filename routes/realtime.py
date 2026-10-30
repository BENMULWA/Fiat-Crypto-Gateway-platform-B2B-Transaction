from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import jwt
from jose.exceptions import JWTError

from broadcast import broadcast_manager
from database import get_db
from routes.auth import SECRET_KEY, ALGORITHM

router = APIRouter()

# WebSocket close code (4000-4999 is the app-defined range) for "not authenticated".
WS_UNAUTHORIZED = 4401


async def _authenticate_ws(token: str | None, claimed_user_id: str | None) -> str | None:
    """Return the user id proven by `token`, or None if it can't be trusted.

    The browser WebSocket API can't set an Authorization header, so the JWT
    travels as ?token=. The server-side identity always comes from the
    token's own `sub` -- a client-supplied ?userId= is only accepted if it
    matches, and is otherwise ignored, so one user can no longer subscribe
    to another user's wallet/deposit/withdrawal events.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    if claimed_user_id and claimed_user_id != str(user_id):
        return None

    # Honour "sign out this device" the same way HTTP routes do.
    session_id = payload.get("sid")
    if session_id:
        session = await get_db().sessions.find_one({"_id": session_id}, {"revoked": 1})
        if session and session.get("revoked"):
            return None
    return str(user_id)


@router.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket, userId: str | None = None, token: str | None = None):
    """WebSocket endpoint for real-time, user-scoped dashboard updates.

    Connect with ?token=<JWT> (and optionally ?userId=<same id>). Unauthenticated
    or mismatched connections are closed before any events are delivered.
    """
    user_id = await _authenticate_ws(token, userId)
    if not user_id:
        # Must accept-then-close or close before accept; closing before accept
        # makes Starlette reject the handshake with HTTP 403.
        await websocket.close(code=WS_UNAUTHORIZED)
        return

    await websocket.accept()

    # wrapper that sends JSON to the websocket
    async def ws_send(message: dict):
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    await broadcast_manager.connect(user_id, ws_send)

    try:
        while True:
            # keep connection alive; ignore incoming messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcast_manager.disconnect(user_id, ws_send)
