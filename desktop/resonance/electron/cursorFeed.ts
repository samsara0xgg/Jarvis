// Which pointer samples the companion's page is sent. electron/companion.ts asks once per 16 ms tick; the key and mouse-button
// polls of that tick are not part of this and stay at 16 ms (a double tap is read from instantaneous key states).
//
// Near the window (its frame and MARGIN around it, BELOW it more) or while the page has anything on show or under way (`awake`: a pane,
// the Dashboard, a card, a menu, the field, her out): every change, as ever. Far from it with nothing going on, the pointer touches
// nothing of hers: at home she follows a pointer only within 260 pt of her (src/CompanionBall.tsx; she sits 16 pt below the top of the
// window and 195 pt from its left), which BELOW reaches, and the zones, the pill and the panes all lie inside the window and its margin.
// The page then hears of it at most every FAR_MS and only after it moved FAR_STEP (under a pixel of her gaze at that distance), and the
// first sample past the margin is always sent so that what the pointer left (a zone, the pill) closes on time. An unsent last position
// is sent once FAR_MS have passed, because each tick asks again.
export const MARGIN = 120, BELOW = 240, FAR_MS = 125, FAR_STEP = 12;

type Point = { x: number; y: number };
type Rect = { x: number; y: number; width: number; height: number };
export type Feed = { sent: boolean; x: number; y: number; at: number; far: boolean };

export const start = (): Feed => ({ sent: false, x: 0, y: 0, at: 0, far: false });
/** The next sample is sent whatever it is (the page was told something else, or the window moved under the pointer). */
export const reset = (feed: Feed) => { feed.sent = false; };

/** `point` and `frame` in screen coordinates; `now` in ms. True: send the page `point` relative to `frame`. */
export function shouldSend(feed: Feed, point: Point, frame: Rect, awake: boolean, now: number): boolean {
  const x = point.x - frame.x, y = point.y - frame.y;
  if (feed.sent && x === feed.x && y === feed.y) return false;
  const near = awake || (x >= -MARGIN && x <= frame.width + MARGIN && y >= -MARGIN && y <= frame.height + BELOW);
  if (feed.sent && !near && feed.far && (now - feed.at < FAR_MS || Math.hypot(x - feed.x, y - feed.y) < FAR_STEP)) return false;
  feed.sent = true; feed.x = x; feed.y = y; feed.at = now; feed.far = !near;
  return true;
}
