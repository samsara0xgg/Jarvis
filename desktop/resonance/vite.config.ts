import { defineConfig } from 'vite';
// Two pages: the app itself, and the first launch the companion shows once before she moves in.
export default defineConfig({ base: './', server: { host: '127.0.0.1' }, build: { rollupOptions: { input: { index: 'index.html', firstrun: 'firstrun.html' } } } });
