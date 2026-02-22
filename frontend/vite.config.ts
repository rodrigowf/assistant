import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'

const certsDir = path.resolve(__dirname, '..', 'certs')
const hasLocalCerts = fs.existsSync(path.join(certsDir, 'key.pem'))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0', // Listen on all network interfaces
    https: hasLocalCerts
      ? { key: fs.readFileSync(path.join(certsDir, 'key.pem')), cert: fs.readFileSync(path.join(certsDir, 'cert.pem')) }
      : undefined,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        ws: true,
      },
    },
  },
})
