// `npm run companion -- --demo` prints this once the ball is on screen, so a first-time visitor knows where to look.
const write = (text: string) => process.stdout.write(text);

export async function demoBanner() {
  const lines = ['', '  \x1b[1mJarvis is ready.\x1b[0m', '', '  \x1b[96m↑ Look at the top of your screen, at the notch.\x1b[0m',
    '    Move your cursor near it and it reacts.', '    This is the demo, built-in sample data only. Press Ctrl+C here to quit.', ''];
  if (!process.stdout.isTTY) return write(lines.join('\n') + '\n');
  const sparks = ['·', '✧', '✦', '✶', '✦', '✧'];
  for (let i = 0; i < 18; i++) {
    write(`\r  \x1b[96m${sparks[i % sparks.length]}\x1b[0m starting up${'.'.repeat(i % 4)}\x1b[K`);
    await new Promise(done => setTimeout(done, 70));
  }
  write('\r\x1b[K');
  for (const line of lines) { write(line + '\n'); await new Promise(done => setTimeout(done, 90)); }
}
