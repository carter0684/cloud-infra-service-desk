import { isTauri } from './tauriEnv.js'

// ── target="_blank" links inside the Tauri desktop webview ────────────────
// Tauri's webview has no OS-level handler for "open a new browser
// window/tab" by default — clicking a plain <a target="_blank"> just
// silently does nothing (no error, no popup, no navigation). This
// affects every such link across the app: Engineer Notes attachments
// (JobDetail.jsx's NoteAttachment), "Open in AWS Console"/Freshservice/
// AMS links, the Security Team Portal's "Open Security Scan Portal"
// button, etc. — none of them worked in the desktop build even though
// they work fine in a normal browser tab.
//
// Fix: install one document-level click listener (see
// installExternalLinkHandler(), called once from App.jsx) that
// intercepts clicks on any target="_blank" anchor and hands the URL to
// @tauri-apps/plugin-shell's open() instead, which asks the OS to open
// it in the user's actual default browser. Only active inside a real
// Tauri window (isTauri() guard) — in the plain web build this listener
// is never installed, so normal browser <a target="_blank"> behavior is
// completely untouched there.
//
// This needs the "shell:allow-open" permission in
// src-tauri/capabilities/desktop.json and the tauri-plugin-shell crate
// registered in src-tauri/src/lib.rs — without both, the dynamic import
// below would still resolve but the actual `open()` call would be
// rejected by Tauri's ACL with the same kind of "not allowed by ACL"
// error seen previously for the updater plugin.
export function installExternalLinkHandler() {
  if (!isTauri()) return () => {}

  const handleClick = (event) => {
    const anchor = event.target?.closest?.('a[target="_blank"]')
    if (!anchor || !anchor.href) return

    event.preventDefault()
    import('@tauri-apps/plugin-shell')
      .then(({ open }) => open(anchor.href))
      .catch((e) => {
        // eslint-disable-next-line no-console
        console.error('Failed to open external link in desktop app:', e)
      })
  }

  document.addEventListener('click', handleClick)
  return () => document.removeEventListener('click', handleClick)
}
