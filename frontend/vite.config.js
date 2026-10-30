import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5200,
    proxy: {
      // NOTE: temporarily on 8101, not 8100 — 8100 has a stuck/zombie
      // Windows TCP listener from a crashed process (PID with no live
      // owner, but the OS still won't release the socket) that keeps
      // absorbing every request instead of the real backend. Switch
      // back to 8100 once that clears (a reboot fixes it) or leave on
      // 8101 permanently — either works, just keep this in sync with
      // whatever port the local `uvicorn --port` below uses.
      '/api': 'http://localhost:8101',
    },
  },
})
