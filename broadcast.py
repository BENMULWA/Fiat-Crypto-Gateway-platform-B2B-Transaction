import asyncio
from typing import Dict, Set

# Very small in-memory broadcaster for WebSocket connections.
# For production use a Redis pub/sub or message broker so multiple
# server instances can broadcast to all connected clients.


class BroadcastManager:
    def __init__(self):
        # user_id -> set of websocket send coroutines
        self._connections: Dict[str, Set] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws_send):
        async with self._lock:
            conns = self._connections.setdefault(user_id, set())
            conns.add(ws_send)

    async def disconnect(self, user_id: str, ws_send):
        async with self._lock:
            conns = self._connections.get(user_id)
            if conns and ws_send in conns:
                conns.remove(ws_send)
                if not conns:
                    del self._connections[user_id]

    async def send_user(self, user_id: str, message: dict):
        # fire-and-forget sends to all connections for user
        async with self._lock:
            conns = list(self._connections.get(user_id, []))

        if not conns:
            return

        coros = []
        for ws_send in conns:
            try:
                coros.append(ws_send(message))
            except Exception:
                # ignore individual errors
                continue

        if coros:
            await asyncio.gather(*coros, return_exceptions=True)


# singleton manager used by routes
broadcast_manager = BroadcastManager()
