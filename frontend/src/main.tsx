import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// Detect low-end devices and add a class to disable animations
const cores = navigator.hardwareConcurrency ?? 4;
const memory = (navigator as { deviceMemory?: number }).deviceMemory ?? 4;
if (cores <= 2 || memory <= 1) {
  document.documentElement.classList.add('low-end');
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
