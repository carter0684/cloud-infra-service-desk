import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import { api } from '../api.js'

const CARD = {
  maxWidth: 420,
  margin: '80px auto',
  background: '#fff',
  borderRadius: 12,
  padding: '40px 36px',
  boxShadow: '0 4px 24px rgba(0,0,0,0.1)',
}

export default function LoginPage() {
  const { user, login, authMode, checkingSession, startOidcLogin } = useAuth()
  const navigate = useNavigate()
  const [team, setTeam] = useState([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (user) navigate('/dashboard')
    if (authMode === 'local') api.getTeam().then(setTeam).catch(() => {})
  }, [user])

  const handleLogin = async () => {
    if (!selected) return
    setLoading(true)
    setError('')
    try {
      const u = await api.login(selected)
      // One-time flag consumed by the post-login welcome banner (see
      // App.jsx's WelcomeBanner) — must be set *before* login()/navigate
      // so it's already in sessionStorage by the time Shell re-renders
      // with the new `user`.
      sessionStorage.setItem('job_portal_just_logged_in', '1')
      login(u)
      navigate('/dashboard')
    } catch (e) {
      setError(e?.response?.data?.detail || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const handleOidcLogin = () => {
    // Survives the ALB/Azure AD redirect round-trip since sessionStorage
    // is tied to the browser tab, not the page — set it right before
    // leaving so it's already there when the tab comes back post-auth.
    sessionStorage.setItem('job_portal_just_logged_in', '1')
    startOidcLogin()
  }

  if (authMode === 'oidc') {
    return (
      <div style={{ background: '#f0f2f5', minHeight: '100vh' }}>
        <div style={CARD} className="login-card-animate">
          <div style={{ textAlign: 'center', marginBottom: 28 }}>
            <img
              src="/logo-256.png"
              alt="Cloud Infra Service Desk"
              className="login-logo-animate"
              style={{ width: 64, height: 64, marginBottom: 8, objectFit: 'contain' }}
            />
            <h2 style={{ margin: 0, color: '#0f2e5a', fontSize: 22 }}>Cloud Infra Service Desk</h2>
            <p style={{ color: '#64748b', fontSize: 14, marginTop: 6 }}>Powered by IT Cloud Infra Team</p>
          </div>

          {checkingSession ? (
            <p style={{ textAlign: 'center', color: '#64748b', fontSize: 14 }}>Checking your session…</p>
          ) : (
            <button
              onClick={handleOidcLogin}
              style={{
                width: '100%', padding: '11px', borderRadius: 7, border: 'none',
                background: '#1a4080', color: '#fff', fontWeight: 600, fontSize: 15, cursor: 'pointer',
              }}
            >
              Sign in with Microsoft
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div style={{ background: '#f0f2f5', minHeight: '100vh' }}>
      <div style={CARD} className="login-card-animate">
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <img
            src="/logo-256.png"
            alt="Cloud Infra Service Desk"
            className="login-logo-animate"
            style={{ width: 64, height: 64, marginBottom: 8, objectFit: 'contain' }}
          />
          <h2 style={{ margin: 0, color: '#0f2e5a', fontSize: 22 }}>Cloud Infra Service Desk</h2>
          <p style={{ color: '#64748b', fontSize: 14, marginTop: 6 }}>Powered by IT Cloud Infra Team</p>
        </div>

        <div style={{ marginBottom: 6, color: '#374151', fontSize: 13, fontWeight: 600 }}>
          Sign in with your account
        </div>
        <div style={{ background: '#fef3c7', border: '1px solid #f59e0b', borderRadius: 6, padding: '8px 12px', fontSize: 12, color: '#92400e', marginBottom: 16 }}>
          🔒 <strong>Local test mode</strong> — select your account below. Azure AD SSO will be used in production.
        </div>

        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          style={{ width: '100%', padding: '10px 12px', borderRadius: 7, border: '1px solid #d1d5db', fontSize: 14, marginBottom: 14, background: '#fff' }}
        >
          <option value="">— Select your account —</option>
          {team.map(t => (
            <option key={t.email} value={t.email}>{t.name} ({t.email})</option>
          ))}
        </select>

        {error && <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 10 }}>{error}</div>}

        <button
          onClick={handleLogin}
          disabled={!selected || loading}
          style={{
            width: '100%', padding: '11px', borderRadius: 7, border: 'none',
            background: selected ? '#1a4080' : '#cbd5e1',
            color: '#fff', fontWeight: 600, fontSize: 15, cursor: selected ? 'pointer' : 'default',
          }}
        >
          {loading ? 'Signing in…' : 'Sign In'}
        </button>
      </div>
    </div>
  )
}
