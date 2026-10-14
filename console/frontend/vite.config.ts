import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const webOutput = fileURLToPath(new URL('../web/v2', import.meta.url));

export default defineConfig({
  base: '/v2/',
  plugins: [react()],
  build: {
    outDir: webOutput,
    emptyOutDir: true,
    manifest: '.vite/manifest.json',
    assetsDir: 'assets',
  },
});
