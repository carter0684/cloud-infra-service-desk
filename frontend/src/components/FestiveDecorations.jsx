import React, { useMemo } from 'react'
import { getActiveFestival } from '../utils/festivals.js'

// Purely cosmetic seasonal touch — gently falling emoji particles across
// the very top of the page plus a small greeting ribbon, active only
// during Christmas / New Year's / Chinese New Year windows (see
// utils/festivals.js). No festival active -> renders nothing at all.
// Keyframes are injected once via a <style> tag scoped to this component
// (no global CSS file exists in this project yet, see main.jsx).

const STYLE_ID = 'festive-decorations-keyframes'

function ensureKeyframes() {
  if (document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = `
    @keyframes festive-fall {
      0%   { transform: translateY(-10px) rotate(0deg); opacity: 0; }
      10%  { opacity: 1; }
      100% { transform: translateY(70px) rotate(25deg); opacity: 0; }
    }
    @keyframes festive-pulse {
      0%, 100% { transform: scale(1); }
      50%      { transform: scale(1.15); }
    }
  `
  document.head.appendChild(style)
}

const STRIP = {
  position: 'relative', height: 30, overflow: 'hidden', pointerEvents: 'none',
}
const RIBBON = (gradient) => ({
  background: gradient, color: '#fff', textAlign: 'center', fontSize: 12.5,
  fontWeight: 600, padding: '5px 0', letterSpacing: 0.3,
})

export default function FestiveDecorations() {
  const festival = useMemo(() => getActiveFestival(), [])
  const particlePositions = useMemo(() => (
    Array.from({ length: 14 }, () => ({
      left: Math.random() * 100,
      size: 12 + Math.random() * 10,
      duration: 4 + Math.random() * 4,
      delay: Math.random() * 5,
    }))
  ), [])

  React.useEffect(() => { if (festival) ensureKeyframes() }, [festival])

  if (!festival) return null

  return (
    <div>
      <div style={STRIP}>
        {particlePositions.map((p, i) => (
          <span key={i} style={{
            position: 'absolute', left: `${p.left}%`, top: -10, fontSize: p.size,
            animation: `festive-fall ${p.duration}s linear ${p.delay}s infinite`,
          }}>
            {festival.particles[i % festival.particles.length]}
          </span>
        ))}
      </div>
      <div style={RIBBON(festival.gradient)}>
        <span style={{ display: 'inline-block', animation: 'festive-pulse 2s ease-in-out infinite' }}>
          {festival.navIcon}
        </span>
        {' '}{festival.greeting}{' '}
        <span style={{ display: 'inline-block', animation: 'festive-pulse 2s ease-in-out infinite' }}>
          {festival.navIcon}
        </span>
      </div>
    </div>
  )
}
