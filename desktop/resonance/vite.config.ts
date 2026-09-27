import { defineConfig } from 'vite';
// Three pages: the app itself, the first launch the companion shows once before she moves in, and dictation (ADR 0058).
export default defineConfig({ base: './', server: { host: '127.0.0.1' }, build: { rollupOptions: { input: { index: 'index.html', firstrun: 'firstrun.html', dictation: 'dictation.html' } } } });
