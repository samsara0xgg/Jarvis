// A poll of the daemon that slows down while the daemon refuses it. A brain's terminal answers some routes with a
// 401 or a 404 for good (the night run or the plugin service is not where it looks), and every refusal is a round trip
// to the brain: the wait doubles up to 30 s and returns to 1.5 s on the first answer.
export const POLL_MS = 1500;
export const MAX_POLL_MS = 30_000;

/** A fetch error of this app names its status last (`/inherent/night 404`); 401 and 404 are the refusals. */
export const refusal = (e: unknown): boolean => e instanceof Error && /\b(401|404)$/.test(e.message);

/** The wait before the next poll: the base once it answered, doubled (to the cap) while it was refused. */
export const nextPoll = (wait: number, refused: boolean): number => refused ? Math.min(wait * 2, MAX_POLL_MS) : POLL_MS;

/** Run `tick` now and after each wait; `tick` resolves true when the daemon refused it. Returns the stop. */
export function poll(tick: () => Promise<boolean>): () => void {
  let stopped = false, wait = POLL_MS, timer: ReturnType<typeof setTimeout> | undefined;
  const run = async () => {
    wait = nextPoll(wait, await tick());
    if (!stopped) timer = setTimeout(run, wait);
  };
  void run();
  return () => { stopped = true; clearTimeout(timer); };
}
