"""Tests for the WebSocket ConnectionManager fan-out (no real sockets)."""


from app.routers.ws import ConnectionManager


class FakeWebSocket:
    """Minimal stand-in for starlette's WebSocket."""

    def __init__(self, fail_on_send=False):
        self.accepted = False
        self.sent = []
        self.fail_on_send = fail_on_send

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self.fail_on_send:
            raise RuntimeError("socket closed")
        self.sent.append(message)


class TestConnectionManager:
    async def test_connect_accepts_and_tracks(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        await mgr.connect(ws)
        assert ws.accepted is True
        assert mgr.connection_count == 1

    async def test_disconnect_removes(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        await mgr.connect(ws)
        await mgr.disconnect(ws)
        assert mgr.connection_count == 0

    async def test_broadcast_reaches_all_clients(self):
        mgr = ConnectionManager()
        a, b = FakeWebSocket(), FakeWebSocket()
        await mgr.connect(a)
        await mgr.connect(b)
        await mgr.broadcast({"event": "hello"})
        assert a.sent == [{"event": "hello"}]
        assert b.sent == [{"event": "hello"}]

    async def test_broadcast_prunes_dead_connections(self):
        mgr = ConnectionManager()
        good = FakeWebSocket()
        dead = FakeWebSocket(fail_on_send=True)
        await mgr.connect(good)
        await mgr.connect(dead)
        await mgr.broadcast({"event": "x"})
        # The failing socket is pruned; the healthy one remains and received it.
        assert mgr.connection_count == 1
        assert good.sent == [{"event": "x"}]

    async def test_broadcast_event_shape(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        await mgr.connect(ws)
        await mgr.broadcast_event("step_completed", "mix-1", {"step": "analyze"})
        assert ws.sent == [
            {"event": "step_completed", "mix_id": "mix-1", "data": {"step": "analyze"}}
        ]

    async def test_broadcast_event_defaults_data(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        await mgr.connect(ws)
        await mgr.broadcast_event("pipeline_started", "mix-1", None)
        assert ws.sent[0]["data"] == {}
