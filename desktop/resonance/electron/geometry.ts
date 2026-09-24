export interface Rect { x: number; y: number; width: number; height: number }
export function clampBounds(bounds: Rect, area: Rect): Rect {
  const width = Math.min(bounds.width, area.width);
  const height = Math.min(bounds.height, area.height);
  return { width, height, x: Math.round(Math.max(area.x, Math.min(bounds.x, area.x + area.width - width))), y: Math.round(Math.max(area.y, Math.min(bounds.y, area.y + area.height - height))) };
}

// Coordinates are Electron DIPs, including displays left of or above the primary.
export function notchBounds(bounds: Rect, display: Rect): Rect {
  const width = Math.min(bounds.width, display.width);
  return { width, height: Math.min(bounds.height, display.height), x: Math.round(display.x + (display.width - width) / 2), y: display.y };
}

export function nearNotch(bounds: Rect, display: Rect, topInset: number): boolean {
  return Math.abs(bounds.x + bounds.width / 2 - (display.x + display.width / 2)) <= 96
    && bounds.y >= display.y - 40 && bounds.y <= display.y + topInset + 40;
}
