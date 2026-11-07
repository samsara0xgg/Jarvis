// Her face in the browser tab, alive like the app's: she blinks now and then while you read, sleeps while her tab is out
// of sight, and is glad for a moment when you come back. Her faces sit beside favicon.svg (favicon-blink, -sleep and
// -glad.svg). With reduced motion she does not blink. Every page loads this: Head.astro, and the docs through
// astro.config.mjs.
(() => {
  const link = document.querySelector('link[rel~="icon"][type="image/svg+xml"]');
  if (!link) return;
  const awake = link.href;
  const face = Object.fromEntries(['blink', 'sleep', 'glad'].map((name) => [name, awake.replace(/favicon\.svg$/, `favicon-${name}.svg`)]));
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  const show = (href) => { if (link.href !== href) link.href = href; };
  let timer = 0;
  const after = (ms, then) => { clearTimeout(timer); timer = setTimeout(then, ms); };

  // A blink every four to nine seconds, now and then a double one, so it never falls into a beat.
  const blinkSoon = () => {
    if (still.matches) return;
    after(4000 + Math.random() * 5000, () => {
      show(face.blink);
      after(130, () => {
        show(awake);
        if (Math.random() < 0.22) after(170, () => { show(face.blink); after(130, () => { show(awake); blinkSoon(); }); });
        else blinkSoon();
      });
    });
  };
  const sleep = () => { clearTimeout(timer); show(face.sleep); };
  const wake = () => { show(face.glad); after(1300, () => { show(awake); blinkSoon(); }); };

  document.addEventListener('visibilitychange', () => (document.hidden ? sleep() : wake()));
  if (document.hidden) sleep();
  else blinkSoon();
  // The other faces load once the page has, so her first blink or nap never shows an empty tab.
  addEventListener('load', () => { for (const href of Object.values(face)) new Image().src = href; }, { once: true });
})();
