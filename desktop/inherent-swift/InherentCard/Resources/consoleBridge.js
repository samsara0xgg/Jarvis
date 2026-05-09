(function() {
  const post = (level, args) => {
    try {
      const text = args.map(a => {
        if (a == null) return String(a);
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch (_) { return String(a); }
      }).join(' ');
      window.webkit.messageHandlers.console.postMessage({level, text});
    } catch (e) { /* ignore */ }
  };
  ['log', 'warn', 'error', 'info', 'debug'].forEach(level => {
    const orig = console[level].bind(console);
    console[level] = function(...args) { post(level, args); orig(...args); };
  });
})();
