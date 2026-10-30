import React, { useState } from 'react'
import { isTauri } from '../utils/tauriEnv.js'

// "Check for Updates" widget for the Windows/Mac/Linux desktop build
// (Tauri) only — completely absent from the plain web build and the
// Capacitor mobile shell, since @tauri-apps/plugin-updater only works
// inside an actual Tauri window.
//
// Flow: click "Check for Updates" -> plugin hits the manifest at
// desktop-updates/latest.json on CloudFront -> if the version there is
// newer than this running build, download the signed installer and run
// it automatically, then relaunch. No installer file ever needs to be
// manually sent to users again — publishing a new release just means
// building + uploading the new installer/signature/manifest (see
// job-portal/infra-cdk's DesktopUpdatesStack / the release script) and
// every existing install will pick it up next time someone clicks this
// button (or the app's own periodic silent check, if that's ever added).
//
// Dynamic imports are used so this module's code only actually executes
// (and only ever calls into Tauri's `invoke`, which doesn't exist in a
// browser) when running inside a real Tauri window — importing
// @tauri-apps/plugin-updater in the plain web build is otherwise
// harmless (it's just JS), but there's no reason to ship/execute it
// there at all.
export default function AppUpdater() {
  const [status, setStatus] = useState('idle') // idle | checking | uptodate | available | downloading | installing | error
  const [progress, setProgress] = useState(0)
  const [errorMsg, setErrorMsg] = useState('')
  const [newVersion, setNewVersion] = useState('')

  if (!isTauri()) return null

  const handleCheck = async () => {
    setStatus('checking')
    setErrorMsg('')
    try {
      const { check } = await import('@tauri-apps/plugin-updater')
      const update = await check()
      if (!update) {
        setStatus('uptodate')
        return
      }
      setNewVersion(update.version)
      setStatus('available')

      // Immediately proceed to download + install — this is the "auto
      // help user update" part: one click, no separate download/run-
      // installer step for the user to perform themselves.
      setStatus('downloading')
      let total = 0
      let downloaded = 0
      await update.downloadAndInstall((event) => {
        switch (event.event) {
          case 'Started':
            total = event.data.contentLength || 0
            break
          case 'Progress':
            downloaded += event.data.chunkLength || 0
            setProgress(total ? Math.min(100, Math.round((downloaded / total) * 100)) : 0)
            break
          case 'Finished':
            setStatus('installing')
            break
        }
      })

      const { relaunch } = await import('@tauri-apps/plugin-process')
      await relaunch()
    } catch (e) {
      setStatus('error')
      setErrorMsg(e?.message || String(e))
    }
  }

  const label = {
    idle: 'Check for Updates',
    checking: 'Checking…',
    uptodate: '✓ Up to date',
    available: `Update to v${newVersion} found…`,
    downloading: `Downloading… ${progress}%`,
    installing: 'Installing — app will restart…',
    error: 'Check for Updates',
  }[status]

  const busy = status === 'checking' || status === 'downloading' || status === 'installing'

  return (
    <div style={{ marginTop: 8 }}>
      <button
        onClick={handleCheck}
        disabled={busy}
        style={{
          width: '100%', background: 'rgba(255,255,255,0.08)', color: '#fff',
          border: '1px solid rgba(255,255,255,0.15)', borderRadius: 6, padding: '6px 10px',
          fontSize: 11.5, cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.85 : 1,
        }}
      >
        {label}
      </button>
      {status === 'uptodate' && (
        <div style={{ color: '#86efac', fontSize: 10.5, marginTop: 4, textAlign: 'center' }}>
          You're on the latest version
        </div>
      )}
      {status === 'error' && (
        <div style={{ color: '#fca5a5', fontSize: 10.5, marginTop: 4 }}>
          Update check failed: {errorMsg}
        </div>
      )}
    </div>
  )
}
