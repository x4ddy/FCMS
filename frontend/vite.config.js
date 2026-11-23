import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/auth': 'http://127.0.0.1:8001',
      '/households': { target: 'http://127.0.0.1:8002', ws: true },
      '/products': 'http://127.0.0.1:8002',
    },
  },
})
