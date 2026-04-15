import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import legacy from '@vitejs/plugin-legacy'
import path from 'path'

export default defineConfig({
  plugins: [
    react(),
    legacy({
      targets: ['safari >= 12', 'ios >= 12'],
      modernPolyfills: true,
      renderModernChunks: false,
    }),
  ],
  base: '/compat/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '../frontend/src'),
      'diff': path.resolve(__dirname, 'node_modules/diff/libesm/index.js'),
      'react-syntax-highlighter/dist/esm/styles/prism': path.resolve(__dirname, 'src/shims/react-syntax-highlighter-style.ts'),
      'react-syntax-highlighter/dist/cjs/styles/prism': path.resolve(__dirname, 'src/shims/react-syntax-highlighter-style.ts'),
      'react-syntax-highlighter': path.resolve(__dirname, 'src/shims/react-syntax-highlighter.tsx'),
      'remark-gfm': path.resolve(__dirname, 'src/shims/remark-gfm.ts'),
      '@/components/MessageList': path.resolve(__dirname, 'src/shims/MessageList.tsx'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5433,
    proxy: {
      '/api': {
        target: 'http://localhost:8765',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
