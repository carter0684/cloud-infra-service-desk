import React, { useRef, useState } from 'react'

// Shared circular avatar — renders a user's photo_data_url if present,
// otherwise falls back to their initial letter on a solid background
// (the original placeholder look). Optionally clickable to upload a new
// photo (used for "my own avatar" in the sidebar, and per-row in the
// Admin page's user table for admin-managed uploads).
//
// onUpload(file): if provided, the avatar becomes clickable (a small
// camera badge appears on hover) and clicking it opens a native file
// picker; the selected File is handed to onUpload, which the caller is
// responsible for actually sending to the backend (api.js) and updating
// state with the result.
export default function Avatar({
  photoUrl, name, email, size = 30, fontSize = 13, bg = '#1a4080',
  onUpload, uploading = false, title,
}) {
  const inputRef = useRef(null)
  const [hover, setHover] = useState(false)
  const initial = (name || email || '?').charAt(0).toUpperCase()
  const clickable = typeof onUpload === 'function'

  const handleFileChange = (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-selecting the same file next time
    if (file) onUpload(file)
  }

  return (
    <div
      title={title}
      onMouseEnter={() => clickable && setHover(true)}
      onMouseLeave={() => clickable && setHover(false)}
      onClick={() => clickable && !uploading && inputRef.current?.click()}
      style={{
        position: 'relative', width: size, height: size, borderRadius: '50%',
        background: photoUrl ? 'transparent' : bg, color: '#fff', display: 'flex',
        alignItems: 'center', justifyContent: 'center', fontSize, fontWeight: 700,
        flexShrink: 0, overflow: 'hidden', cursor: clickable ? (uploading ? 'default' : 'pointer') : 'default',
      }}
    >
      {photoUrl ? (
        <img src={photoUrl} alt={name || email || 'avatar'} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      ) : (
        initial
      )}

      {clickable && (hover || uploading) && (
        <div style={{
          position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: Math.max(10, size * 0.4),
        }}>
          {uploading ? '⏳' : '📷'}
        </div>
      )}

      {clickable && (
        <input ref={inputRef} type="file" accept="image/*" onChange={handleFileChange}
          style={{ display: 'none' }} />
      )}
    </div>
  )
}
