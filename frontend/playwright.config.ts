import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// Browser workflow test. Starts the REAL backend (uvicorn + FastAPI + SQLite) through
// backend/tests/e2e_server.py with a fresh temporary data folder and deterministic
// test-only provider adapters. Never touches the owner's .env, database or accounts.
const PORT = 8765;
const python = process.platform === 'win32'
  ? path.resolve(here, '..', 'venv', 'Scripts', 'python.exe')
  : path.resolve(here, '..', 'venv', 'bin', 'python');

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `"${python}" "${path.resolve(here, '..', 'backend', 'tests', 'e2e_server.py')}" --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'pipe',
  },
});
