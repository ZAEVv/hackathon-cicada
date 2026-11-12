import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // Serve only files inside the workspace root (Vite's default, pinned explicitly).
    fs: { strict: true },
    // Plain string target: no changeOrigin / xfwd, so the API sees a loopback Host and no forwarding headers.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
  // `vite preview` binds to loopback only, like the dev server.
  preview: { host: '127.0.0.1', strictPort: true },
  // Do not ship source maps with the production bundle.
  build: { sourcemap: false },
})
