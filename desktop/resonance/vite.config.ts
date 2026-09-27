import { defineConfig } from 'vite';
// Four pages: the app itself, the first launch the companion shows once before she moves in, dictation (ADR 0058) and
// the Agents window (ADR 0072).
export default defineConfig({ base: './', server: { host: '127.0.0.1' }, build: { rollupOptions: { input: { index: 'index.html', firstrun: 'firstrun.html', dictation: 'dictation.html', agents: 'agents.html' } } } });
