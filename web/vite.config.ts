import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  esbuild: { jsx: 'automatic' },
  server: { host: '127.0.0.1', hmr: false },
  build: { outDir: 'dist', sourcemap: true, rollupOptions: { input: { workspace: 'index.html', preview: 'chat-preview.html' } } },
});
