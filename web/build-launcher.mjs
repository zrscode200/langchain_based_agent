// Release/editable build only. The installed launcher never runs npm or Vite.
import { build } from 'vite';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('.', import.meta.url));
await build({ root, build: { outDir: '../src/lc_factory/_web/dist', emptyOutDir: true, sourcemap: false } });
await build({ root, configFile: false, publicDir: false, ssr: { noExternal: true }, build: {
  ssr: 'server/main.ts', outDir: '../src/lc_factory/_web/server', emptyOutDir: true,
  rollupOptions: { external: ['vite'], output: { entryFileNames: 'main.mjs' } },
} });
