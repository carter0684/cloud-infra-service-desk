import { useEffect, useState } from 'react'

// True on phone-sized screens. Same 768px breakpoint as the sidebar drawer in
// App.jsx and the rules in src/mobile.css, so a page that switches layout in
// JS (e.g. table -> cards) flips at exactly the same width as the CSS does.
export const MOBILE_MAX_WIDTH = 768

// `maxWidth` lets a page that has a wide, fixed-column table switch to its card
// layout earlier than the global phone breakpoint (e.g. tablets with the sidebar
// still open leave the content column only ~530-840px wide).
export default function useIsMobile(maxWidth = MOBILE_MAX_WIDTH) {
  const query = `(max-width: ${maxWidth}px)`
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(query).matches
  )
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined
    const mql = window.matchMedia(query)
    const onChange = (e) => setIsMobile(e.matches)
    setIsMobile(mql.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  return isMobile
}
