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
      // Force Babel to transpile node_modules that ship modern syntax
      renderModernChunks: false,
    }),
  ],
  base: '/compat/',
  resolve: {
    alias: {
      // Share all source components from the main frontend
      '@': path.resolve(__dirname, '../frontend/src'),
      // Shim out react-syntax-highlighter (uses named capture groups unsupported in Safari 12)
      'react-syntax-highlighter/dist/esm/styles/prism': path.resolve(__dirname, 'src/shims/react-syntax-highlighter-style.ts'),
      'react-syntax-highlighter/dist/cjs/styles/prism': path.resolve(__dirname, 'src/shims/react-syntax-highlighter-style.ts'),
      'react-syntax-highlighter': path.resolve(__dirname, 'src/shims/react-syntax-highlighter.tsx'),
      // Shim out remark-gfm (uses lookbehind regexes unsupported in Safari 12)
      'remark-gfm': path.resolve(__dirname, 'src/shims/remark-gfm.ts'),
      // Compat MessageList: stops iOS momentum scroll before programmatic scrollTop assignments
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
