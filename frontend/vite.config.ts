/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on the LAN when started with --host for phone testing.
    proxy: {
      '/api': {
        // Where the Python API is running. Override with: $env:API_TARGET="http://localhost:8001"; npm run dev
        target: process.env.API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    // Vitest runs component tests only; Playwright browser tests live in e2e/ and use their own runner.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    restoreMocks: true,
  },
})
