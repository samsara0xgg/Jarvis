"""Tests for the EventBus."""

from core.event_bus import EventBus


class TestEventBus:
    def test_basic_emit_and_listen(self):
        bus = EventBus()
        received = []
        bus.on("test.event", lambda data: received.append(data))
        bus.emit("test.event", {"key": "value"})
        assert received == [{"key": "value"}]

    def test_multiple_listeners(self):
        bus = EventBus()
        results = []
        bus.on("evt", lambda d: results.append("a"))
        bus.on("evt", lambda d: results.append("b"))
        bus.emit("evt")
        assert results == ["a", "b"]

    def test_wildcard_subscription(self):
        bus = EventBus()
        received = []
        bus.on("device.*", lambda d: received.append(d))
        bus.emit("device.state_changed", "light_on")
        bus.emit("device.error", "timeout")
        bus.emit("jarvis.state_changed", "listening")
        assert received == ["light_on", "timeout"]

    def test_off_removes_listener(self):
        bus = EventBus()
        received = []
        cb = lambda d: received.append(d)
        bus.on("evt", cb)
        bus.emit("evt", 1)
        bus.off("evt", cb)
        bus.emit("evt", 2)
        assert received == [1]

    def test_listener_exception_does_not_break_others(self):
        bus = EventBus()
        results = []

        def bad_listener(d):
            raise RuntimeError("boom")

        bus.on("evt", bad_listener)
        bus.on("evt", lambda d: results.append("ok"))
        bus.emit("evt")
        assert results == ["ok"]

    def test_clear(self):
        bus = EventBus()
        received = []
        bus.on("evt", lambda d: received.append(d))
        bus.clear()
        bus.emit("evt", 1)
        assert received == []

    def test_emit_no_listeners(self):
        bus = EventBus()
        bus.emit("nothing")  # should not raise

    def test_off_nonexistent_callback(self):
        bus = EventBus()
        bus.off("evt", lambda d: None)  # should not raise

    def test_none_data(self):
        bus = EventBus()
        received = []
        bus.on("evt", lambda d: received.append(d))
        bus.emit("evt")
        assert received == [None]
