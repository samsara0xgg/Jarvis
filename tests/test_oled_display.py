"""Tests for the OLED display state machine and frame selection."""

from unittest.mock import MagicMock, patch

from core.event_bus import EventBus


class TestOledDisplayStateMachine:
    """Test state transitions without requiring luma installed."""

    def _make_display(self):
        """Create an OledDisplay with mocked luma device."""
        bus = EventBus()
        config = {
            "oled": {
                "enabled": True,
                "width": 128,
                "height": 64,
                "emulator": True,
                "fps": 10,
            }
        }
        with patch("ui.oled_display.OledDisplay._create_device", return_value=MagicMock()):
            from ui.oled_display import OledDisplay
            display = OledDisplay(config, bus)
        return display, bus

    def test_initial_state_is_idle(self):
        display, _ = self._make_display()
        assert display._state == "idle"

    def test_set_state_transitions(self):
        display, _ = self._make_display()
        for state in ["listening", "thinking", "speaking", "face", "idle"]:
            display.set_state(state)
            assert display._state == state

    def test_set_state_resets_frame_num(self):
        display, _ = self._make_display()
        display._frame_num = 42
        display.set_state("listening")
        assert display._frame_num == 0

    def test_invalid_state_ignored(self):
        display, _ = self._make_display()
        display.set_state("listening")
        display.set_state("invalid_state")
        assert display._state == "listening"

    def test_event_bus_triggers_state_change(self):
        display, bus = self._make_display()
        bus.emit("jarvis.state_changed", {"state": "thinking"})
        assert display._state == "thinking"

    def test_event_bus_ignores_bad_data(self):
        display, bus = self._make_display()
        bus.emit("jarvis.state_changed", "not a dict")
        assert display._state == "idle"
        bus.emit("jarvis.state_changed", {"no_state_key": True})
        assert display._state == "idle"

    def test_set_speaking_text(self):
        display, _ = self._make_display()
        display.set_speaking_text("Hello world")
        assert display._speaking_text == "Hello world"
        assert display._scroll_offset == 0

    def test_set_idle_context(self):
        display, _ = self._make_display()
        display.set_idle_context({"weather_temp": 22, "weather_desc": "Sunny"})
        assert display._idle_context["weather_temp"] == 22

    def test_start_stop(self):
        display, _ = self._make_display()
        display.start()
        assert display._running is True
        assert display._thread is not None
        display.stop()
        assert display._running is False


class TestOledFrames:
    """Test frame renderers produce output without errors."""

    def _make_draw(self):
        from PIL import Image, ImageDraw
        img = Image.new("1", (128, 64), "black")
        return ImageDraw.Draw(img)

    def test_render_idle_frame(self):
        from ui.oled_frames import render_idle_frame
        draw = self._make_draw()
        render_idle_frame(draw, 128, 64, {})
        render_idle_frame(draw, 128, 64, {"weather_temp": 20, "weather_desc": "Clear"})

    def test_render_listening_frame(self):
        from ui.oled_frames import render_listening_frame
        draw = self._make_draw()
        for i in range(5):
            render_listening_frame(draw, 128, 64, i)

    def test_render_thinking_frame(self):
        from ui.oled_frames import render_thinking_frame
        draw = self._make_draw()
        for i in range(10):
            render_thinking_frame(draw, 128, 64, i)

    def test_render_speaking_frame(self):
        from ui.oled_frames import render_speaking_frame
        draw = self._make_draw()
        render_speaking_frame(draw, 128, 64, "", 0)
        render_speaking_frame(draw, 128, 64, "Hello this is a test response", 0)
        render_speaking_frame(draw, 128, 64, "Long " * 50, 24)

    def test_render_face_frame(self):
        from ui.oled_frames import render_face_frame
        draw = self._make_draw()
        for i in range(80):
            render_face_frame(draw, 128, 64, i)
