// True only when this bundle is actually running inside a Tauri desktop
// window (not the plain browser build, and not the Capacitor mobile
// shell). Tauri v2 injects this global into every window it creates, so
// checking for it is the standard way to branch desktop-only code (like
// the updater) without breaking `npm run dev`/the CloudFront-hosted web
// build, which never has this global and must never try to import the
// @tauri-apps/plugin-updater JS (it would just no-op/throw there anyway,
// but gating it explicitly keeps things predictable and avoids shipping
// dead network calls to non-desktop clients).
export function isTauri() {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}
