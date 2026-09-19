import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: the backend runs on :8500 and everything under /api is proxied there.
// Prod: the backend serves frontend/dist itself, so there is one port and one process.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: { '/api': 'http://127.0.0.1:8500' },
  },
  build: { outDir: 'dist', sourcemap: true },
})
