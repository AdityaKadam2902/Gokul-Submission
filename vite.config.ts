import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite root is frontend/ (sources live there) while the npm manifest stays at
// the repository root (M0 conformance tests require package.json at root).
export default defineConfig({
  root: 'frontend',
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
