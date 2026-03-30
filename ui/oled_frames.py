"""Frame renderers for the 128x64 OLED display.

Each renderer draws onto a PIL.ImageDraw.Draw canvas.
Only uses PIL built-in default font — no external font files required.
"""

from __future__ import annotations

import math
import time
from typing import Any

from PIL import ImageDraw, ImageFont


def _default_font() -> ImageFont.ImageFont:
    """Return PIL's built-in bitmap font."""
    return ImageFont.load_default()


def render_idle_frame(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    context: dict[str, Any],
) -> None:
    """Clock + date + weather info on the idle screen."""
    font = _default_font()
    now = time.localtime()

    # Large time string centred near the top
    time_str = time.strftime("%H:%M:%S", now)
    time_bbox = draw.textbbox((0, 0), time_str, font=font)
    tw = time_bbox[2] - time_bbox[0]
    draw.text(((width - tw) // 2, 4), time_str, fill="white", font=font)

    # Date string below the clock
    date_str = time.strftime("%Y-%m-%d  %a", now)
    date_bbox = draw.textbbox((0, 0), date_str, font=font)
    dw = date_bbox[2] - date_bbox[0]
    draw.text(((width - dw) // 2, 22), date_str, fill="white", font=font)

    # Thin separator line
    draw.line([(10, 38), (width - 10, 38)], fill="white", width=1)

    # Weather info from context (if available)
    weather_temp = context.get("weather_temp")
    weather_desc = context.get("weather_desc", "")
    if weather_temp is not None:
        weather_str = f"{weather_temp}C  {weather_desc}"
    else:
        weather_str = "Jarvis Ready"

    wb = draw.textbbox((0, 0), weather_str, font=font)
    ww = wb[2] - wb[0]
    draw.text(((width - ww) // 2, 44), weather_str, fill="white", font=font)


def render_listening_frame(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    frame_num: int,
) -> None:
    """Animated waveform bars that pulse based on frame_num."""
    font = _default_font()

    # Label at the top
    label = "Listening..."
    lb = draw.textbbox((0, 0), label, font=font)
    lw = lb[2] - lb[0]
    draw.text(((width - lw) // 2, 2), label, fill="white", font=font)

    # Draw animated bars across the middle
    num_bars = 16
    bar_width = 4
    gap = 2
    total_w = num_bars * (bar_width + gap) - gap
    start_x = (width - total_w) // 2
    base_y = height // 2 + 8
    max_bar_h = 28

    for i in range(num_bars):
        # Each bar gets a sine-wave height offset by its position
        phase = (frame_num * 0.3) + (i * 0.5)
        bar_h = int((math.sin(phase) * 0.5 + 0.5) * max_bar_h) + 3
        x = start_x + i * (bar_width + gap)
        y_top = base_y - bar_h
        draw.rectangle([x, y_top, x + bar_width, base_y], fill="white")


def render_thinking_frame(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    frame_num: int,
) -> None:
    """Three dots that animate in sequence — a thinking indicator."""
    font = _default_font()

    # Label
    label = "Thinking"
    lb = draw.textbbox((0, 0), label, font=font)
    lw = lb[2] - lb[0]
    draw.text(((width - lw) // 2, 8), label, fill="white", font=font)

    # Three dots with bounce animation
    dot_radius = 5
    dot_spacing = 24
    cx = width // 2
    cy = height // 2 + 6
    positions = [cx - dot_spacing, cx, cx + dot_spacing]

    for i, x in enumerate(positions):
        # Each dot bounces at a different phase
        phase = (frame_num * 0.25) - (i * 1.0)
        bounce = max(0.0, math.sin(phase)) * 12
        y = int(cy - bounce)
        r = dot_radius
        draw.ellipse([x - r, y - r, x + r, y + r], fill="white")


def render_speaking_frame(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    text: str,
    scroll_offset: int,
) -> None:
    """Scrolling response text with a speaker icon indicator."""
    font = _default_font()

    # Header
    label = "Jarvis:"
    draw.text((4, 2), label, fill="white", font=font)

    # Small speaker icon (triangle + arcs) in top-right
    sx, sy = width - 20, 4
    draw.polygon([(sx, sy + 2), (sx, sy + 8), (sx + 5, sy + 5)], fill="white")
    draw.arc([sx + 4, sy, sx + 12, sy + 10], start=-60, end=60, fill="white")

    # Separator
    draw.line([(0, 15), (width, 15)], fill="white", width=1)

    # Word-wrap the text into lines that fit the display width
    if not text:
        return

    max_line_w = width - 8
    words = text.split()
    lines: list[str] = []
    current_line = ""

    for word in words:
        test = f"{current_line} {word}".strip()
        tb = draw.textbbox((0, 0), test, font=font)
        if (tb[2] - tb[0]) <= max_line_w:
            current_line = test
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    # Render visible lines with scroll offset
    line_height = 12
    visible_area_top = 18
    visible_lines = (height - visible_area_top) // line_height

    start_line = scroll_offset // line_height
    for idx in range(visible_lines):
        li = start_line + idx
        if li >= len(lines):
            break
        y = visible_area_top + idx * line_height
        draw.text((4, y), lines[li], fill="white", font=font)


def render_face_frame(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    frame_num: int,
) -> None:
    """Simple animated eyes (Cozmo-style) that occasionally blink."""
    # Eye parameters
    eye_w = 28
    eye_h = 20
    eye_gap = 20
    left_cx = width // 2 - eye_gap
    right_cx = width // 2 + eye_gap
    eye_cy = height // 2

    # Blink every ~60 frames, blink lasts ~4 frames
    blink_cycle = frame_num % 80
    is_blinking = blink_cycle >= 76

    if is_blinking:
        # Squished eyes during blink
        blink_h = 3
        for cx in (left_cx, right_cx):
            draw.ellipse(
                [cx - eye_w // 2, eye_cy - blink_h,
                 cx + eye_w // 2, eye_cy + blink_h],
                fill="white",
            )
    else:
        # Normal open eyes with a subtle look-around
        look_phase = math.sin(frame_num * 0.05) * 3
        pupil_offset_x = int(look_phase)
        pupil_offset_y = int(math.cos(frame_num * 0.03) * 2)

        for cx in (left_cx, right_cx):
            # Outer eye (white oval)
            draw.ellipse(
                [cx - eye_w // 2, eye_cy - eye_h // 2,
                 cx + eye_w // 2, eye_cy + eye_h // 2],
                fill="white",
            )
            # Pupil (black circle inside)
            pr = 6
            px = cx + pupil_offset_x
            py = eye_cy + pupil_offset_y
            draw.ellipse(
                [px - pr, py - pr, px + pr, py + pr],
                fill="black",
            )

    # Small mouth — a gentle curve below the eyes
    mouth_y = eye_cy + eye_h // 2 + 8
    mouth_w = 12
    draw.arc(
        [width // 2 - mouth_w, mouth_y - 4,
         width // 2 + mouth_w, mouth_y + 4],
        start=0, end=180, fill="white", width=1,
    )
