import React, { useRef, useState } from 'react'
import { COLORS } from '../utils/theme.js'
import { api } from '../api.js'

// job-portal-chat-voice Task 14.4 — file/photo attachment picker +
// direct-to-S3 presigned upload flow.
//
// Flow (matches app/chat_attachments.py's presigned-PUT design exactly,
// same pattern as note attachments elsewhere in this app, just a
// different bucket/key convention):
//   1. User picks a file -> client-side pre-check against the SAME
//      10MB (photo) / 50MB (file) limits chat_attachments.py enforces
//      server-side (Requirement 4.1/4.2/4.3) — this is a UX shortcut
//      only; the backend re-validates on generate_presigned_put_url()
//      regardless, so a bypass here can never actually exceed the cap.
//   2. POST .../attachments/presign -> {attachment_id, s3_key, put_url,
//      content_type, is_photo, filename, size_bytes}
//   3. PUT the raw file bytes to put_url (S3 itself, not the backend)
//      with upload progress via axios onUploadProgress.
//   4. Caller (ChatPage.jsx's composer) then calls onUploaded(attachment)
//      — a plain dict {s3_key, filename, content_type, size_bytes,
//      is_photo} — which becomes the Message's `attachment` field via
//      api.sendChatMessage(). Nothing here posts the Message itself:
//      the composer decides whether to bundle attachment + typed text
//      into ONE send, matching Requirement 2.1's "content and/or
//      Attachment" wording.
//
// message_id: chat_attachments.py's S3 key convention is
// chat/{conversation_id}/{message_id}/{attachment_id}-{filename} — but
// the Message doesn't exist yet at presign time (it's created only
// once send is clicked). Following note_attachments.py's own
// established pattern for this exact ordering problem (see
// AddNoteAttachments' pending-id convention elsewhere in this app),
// this generates a client-side placeholder id up front and passes it
// as message_id to the presign call; the backend's key structure only
// cares that message_id is a stable, unique-enough string scoping the
// key namespace, not that it match a real message_id row that already
// exists at upload time.
function pendingMessageId() {
  return `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

const PHOTO_CONTENT_TYPES = new Set(['image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp'])
const MAX_PHOTO_BYTES = 10 * 1024 * 1024
const MAX_FILE_BYTES = 50 * 1024 * 1024

function isPhoto(file) {
  return PHOTO_CONTENT_TYPES.has((file.type || '').toLowerCase())
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// AttachmentPicker: a small "📎" button + hidden file input. Renders its
// own upload-progress/error state inline underneath (so the composer
// stays a plain <textarea> + send button, unaware of upload internals),
// and calls onUploaded(attachment) exactly once per successful upload.
// onRemove() lets the composer clear a staged attachment before sending.
export default function AttachmentUpload({ conversationId, onUploaded, disabled = false }) {
  const inputRef = useRef(null)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState('')
  const [staged, setStaged] = useState(null) // {filename, size_bytes} while uploading, for the progress line

  const pickFile = () => {
    if (disabled || uploading) return
    setError('')
    inputRef.current?.click()
  }

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-selecting the same file next time
    if (!file) return

    const photo = isPhoto(file)
    const maxBytes = photo ? MAX_PHOTO_BYTES : MAX_FILE_BYTES
    if (file.size > maxBytes) {
      const limitMb = maxBytes / (1024 * 1024)
      const kind = photo ? 'photo' : 'file'
      setError(`This ${kind} exceeds the ${limitMb} MB limit for ${kind}s.`)
      return
    }
    if (file.size <= 0) {
      setError('Attachment cannot be empty.')
      return
    }

    setError('')
    setUploading(true)
    setProgress(0)
    setStaged({ filename: file.name, size_bytes: file.size })

    try {
      const presign = await api.presignChatAttachment(
        conversationId, pendingMessageId(), file.name, file.type, file.size,
      )
      await api.uploadChatAttachmentToS3(presign.put_url, file, presign.content_type, setProgress)
      onUploaded({
        s3_key: presign.s3_key,
        filename: presign.filename,
        content_type: presign.content_type,
        size_bytes: presign.size_bytes,
        is_photo: presign.is_photo,
      })
      setStaged(null)
    } catch (err) {
      const detail = err?.response?.data?.detail || err?.message || 'Upload failed. Please try again.'
      setError(detail)
      setStaged(null)
    } finally {
      setUploading(false)
      setProgress(0)
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={pickFile}
        disabled={disabled || uploading}
        title="Attach a photo or file"
        style={{
          width: 36, height: 36, borderRadius: 8, flexShrink: 0,
          border: `1px solid ${COLORS.border}`, background: COLORS.pageBg,
          color: COLORS.textSecondary, fontSize: 16,
          cursor: disabled || uploading ? 'default' : 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
        📎
      </button>
      <input ref={inputRef} type="file" onChange={handleFileChange} style={{ display: 'none' }} />

      {(uploading || error) && (
        <div style={{
          position: 'absolute', bottom: '100%', left: 0, marginBottom: 6,
          background: COLORS.cardBg, border: `1px solid ${error ? COLORS.danger + '60' : COLORS.border}`,
          borderRadius: 8, padding: '8px 12px', fontSize: 12, minWidth: 220,
          boxShadow: '0 4px 12px rgba(0,0,0,0.12)', color: error ? COLORS.danger : COLORS.textSecondary,
        }}>
          {error ? (
            <>⚠️ {error}</>
          ) : (
            <>
              ⏳ Uploading {staged?.filename} ({formatBytes(staged?.size_bytes || 0)})…
              {progress > 0 && ` ${progress}%`}
            </>
          )}
        </div>
      )}
    </div>
  )
}
