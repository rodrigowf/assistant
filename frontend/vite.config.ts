import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import legacy from '@vitejs/plugin-legacy'
import fs from 'fs'
import path from 'path'

const certsDir = path.resolve(__dirname, '..', 'context', 'certs')
const hasLocalCerts = fs.existsSync(path.join(certsDir, 'key.pem'))

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    legacy({
      targets: ['defaults', 'safari >= 12', 'ios >= 12'],
      modernPolyfills: true,
      renderLegacyChunks: true,
      additionalLegacyPolyfills: ['regenerator-runtime/runtime'],
    }),
  ],
  base: '/',
  server: {
    host: '0.0.0.0', // Listen on all network interfaces
    port: 5432,
    strictPort: true,
    https: hasLocalCerts
      ? { key: fs.readFileSync(path.join(certsDir, 'key.pem')), cert: fs.readFileSync(path.join(certsDir, 'cert.pem')) }
      : undefined,
    proxy: {
      '/api': {
        target: 'http://localhost:8765',
        ws: true,
      },
    },
  },
})
