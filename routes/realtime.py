from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from broadcast import broadcast_manager

router = APIRouter()


@router.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket, userId: str | None = None):
    """Simple WebSocket endpoint for real-time dashboard updates.

    Clients should connect with ?userId=<user id> so server can send user-scoped updates.
    This is intentionally minimal; in production authenticate via token and map to user id.
    """
    await websocket.accept()

    # wrapper that sends JSON to the websocket
    async def ws_send(message: dict):
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    if not userId:
        # If no userId provided, accept but don't register for user-scoped messages
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    await broadcast_manager.connect(userId, ws_send)

    try:
        while True:
            # keep connection alive; ignore incoming messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        await broadcast_manager.disconnect(userId, ws_send)
