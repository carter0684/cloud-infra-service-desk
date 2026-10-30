import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api.js'
import { formatDateHK } from '../utils/time.js'

const CARD = {
  maxWidth: 800, margin: '28px auto', background: '#fff',
  borderRadius: 10, padding: '32px 36px', boxShadow: '0 1px 8px rgba(0,0,0,0.08)',
}
const LABEL = { fontSize: 13, fontWeight: 600, color: '#374151', display: 'block', marginBottom: 5, marginTop: 16 }
const INPUT = { width: '100%', padding: '9px 12px', borderRadius: 7, border: '1px solid #d1d5db', fontSize: 14, boxSizing: 'border-box' }

const FILE_ICON = { pdf: '📄', docx: '📝', doc: '📝', txt: '📃', md: '📃', csv: '📊', eml: '✉️', log: '🗒️' }

function formatBytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export default function KBPage() {
  const fileRef = useRef()
  const [docs, setDocs] = useState([])
  const [loading, setLoading] = useState(true)
  const [dragOver, setDragOver] = useState(false)

  // Upload state
  const [description, setDescription] = useState('')
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')
  const [uploadErr, setUploadErr] = useState(false)

  // Delete confirm
  const [deletingId, setDeletingId] = useState(null)

  const loadDocs = () => {
    setLoading(true)
    api.kbList()
      .then(setDocs)
      .catch(() => setDocs([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadDocs() }, [])

  const handleUpload = async (file) => {
    if (!file) return
    setUploading(true)
    setUploadMsg('')
    setUploadErr(false)
    try {
      const result = await api.kbUpload(file, description.trim())
      setUploadMsg(`✅ "${result.filename}" uploaded — ${result.chunk_count} chunks indexed (${result.char_count.toLocaleString()} chars)`)
      setUploadErr(false)
      setDescription('')
      if (fileRef.current) fileRef.current.value = ''
      loadDocs()
    } catch (e) {
      setUploadMsg(e?.response?.data?.detail || 'Upload failed')
      setUploadErr(true)
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (id) => {
    try {
      await api.kbDelete(id)
      setDocs(d => d.filter(x => x.id !== id))
    } catch {
      // ignore
    } finally {
      setDeletingId(null)
    }
  }

  const totalChunks = docs.reduce((s, d) => s + (d.chunk_count || 0), 0)
  const totalChars  = docs.reduce((s, d) => s + (d.char_count || 0), 0)

  return (
    <div style={{ padding: '8px 20px' }}>
      <div style={CARD}>
        {/* ── Header ── */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 4 }}>
          <div>
            <h2 style={{ margin: '0 0 4px', color: '#0f2e5a', fontSize: 20 }}>
              🗂️ Knowledge Base
            </h2>
            <p style={{ margin: 0, color: '#64748b', fontSize: 13 }}>
              Upload reference documents — runbooks, SOPs, KB articles — that AI will use as context when analysing jobs.
            </p>
          </div>
          {docs.length > 0 && (
            <div style={{ textAlign: 'right', fontSize: 12, color: '#94a3b8', flexShrink: 0, marginLeft: 16 }}>
              <div><strong style={{ color: '#374151' }}>{docs.length}</strong> documents</div>
              <div><strong style={{ color: '#374151' }}>{totalChunks}</strong> chunks indexed</div>
              <div style={{ marginTop: 2 }}>{totalChars.toLocaleString()} chars</div>
            </div>
          )}
        </div>

        <hr style={{ border: 'none', borderTop: '1px solid #e5e7eb', margin: '20px 0' }} />

        {/* ── Upload Panel ── */}
        <div style={{ background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 9, padding: '18px 20px', marginBottom: 28 }}>
          <div style={{ fontWeight: 700, color: '#0369a1', fontSize: 14, marginBottom: 12 }}>
            📤 Upload Document to KB
          </div>

          {/* Optional label */}
          <label style={LABEL}>Description / Label (optional)</label>
          <input
            style={INPUT}
            placeholder="e.g. AD Migration Runbook, AWS VPN Troubleshooting Guide…"
            value={description}
            onChange={e => setDescription(e.target.value)}
            maxLength={300}
          />

          {/* Drop zone */}
          <div
            onDragOver={e => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={e => { e.preventDefault(); setDragOver(false); handleUpload(e.dataTransfer.files[0]) }}
            onClick={() => fileRef.current?.click()}
            style={{
              marginTop: 12,
              border: `2px dashed ${dragOver ? '#0369a1' : '#93c5fd'}`,
              borderRadius: 8, padding: '28px 16px', textAlign: 'center',
              cursor: 'pointer', background: dragOver ? '#e0f2fe' : '#f8fafc',
              transition: 'all 0.15s',
            }}>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.txt,.md,.csv,.eml,.log"
              style={{ display: 'none' }}
              onChange={e => handleUpload(e.target.files[0])}
            />
            <div style={{ fontSize: 32, marginBottom: 6 }}>📁</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#1e40af' }}>
              {uploading ? '⏳ Uploading & indexing…' : 'Click or drag & drop a document here'}
            </div>
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
              Supports: PDF, DOCX, TXT, MD, CSV, EML, LOG — max 20 MB
            </div>
          </div>

          {/* Feedback */}
          {uploadMsg && (
            <div style={{
              marginTop: 12, padding: '9px 12px', borderRadius: 6, fontSize: 13,
              background: uploadErr ? '#fee2e2' : '#dcfce7',
              color: uploadErr ? '#dc2626' : '#15803d',
              border: `1px solid ${uploadErr ? '#fca5a5' : '#86efac'}`,
            }}>
              {uploadMsg}
            </div>
          )}

          <div style={{ marginTop: 10, fontSize: 12, color: '#64748b' }}>
            💡 Documents are chunked into ~500-char segments and searched by keyword similarity during AI analysis.
            The most relevant chunks are injected into the Bedrock prompt as context.
          </div>
        </div>

        {/* ── Document List ── */}
        <div style={{ fontWeight: 700, color: '#374151', fontSize: 15, marginBottom: 14 }}>
          Indexed Documents
        </div>

        {loading ? (
          <div style={{ color: '#94a3b8', fontSize: 13, textAlign: 'center', padding: '20px 0' }}>Loading…</div>
        ) : docs.length === 0 ? (
          <div style={{
            textAlign: 'center', padding: '32px 20px',
            background: '#f8fafc', border: '1px dashed #e2e8f0', borderRadius: 8,
            color: '#94a3b8', fontSize: 13,
          }}>
            No documents yet. Upload your first KB document above.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {docs.map(doc => (
              <div key={doc.id} style={{
                border: '1px solid #e5e7eb', borderRadius: 8, padding: '12px 16px',
                display: 'flex', alignItems: 'center', gap: 14,
                background: '#fafafa',
              }}>
                {/* Icon */}
                <div style={{ fontSize: 26, flexShrink: 0 }}>
                  {FILE_ICON[doc.file_type] || '📄'}
                </div>

                {/* Info */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, color: '#1e293b', fontSize: 14, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {doc.filename}
                  </div>
                  {doc.description && (
                    <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>{doc.description}</div>
                  )}
                  <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4, display: 'flex', gap: 12 }}>
                    <span>{doc.chunk_count} chunks</span>
                    <span>{(doc.char_count || 0).toLocaleString()} chars</span>
                    <span>by {doc.uploaded_by?.split('@')[0]}</span>
                    <span>{formatDateHK(doc.uploaded_at)}</span>
                  </div>
                </div>

                {/* Chunks badge */}
                <div style={{
                  flexShrink: 0, background: '#eff6ff', color: '#1d4ed8',
                  border: '1px solid #bfdbfe', borderRadius: 99, fontSize: 11,
                  fontWeight: 700, padding: '3px 10px',
                }}>
                  {doc.chunk_count} chunks
                </div>

                {/* Delete */}
                {deletingId === doc.id ? (
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    <button onClick={() => handleDelete(doc.id)}
                      style={{ padding: '4px 10px', borderRadius: 5, border: 'none', background: '#dc2626', color: '#fff', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
                      Confirm
                    </button>
                    <button onClick={() => setDeletingId(null)}
                      style={{ padding: '4px 10px', borderRadius: 5, border: '1px solid #d1d5db', background: '#fff', fontSize: 12, cursor: 'pointer' }}>
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button onClick={() => setDeletingId(doc.id)}
                    style={{ flexShrink: 0, padding: '4px 10px', borderRadius: 5, border: '1px solid #fca5a5', background: '#fff', color: '#dc2626', fontSize: 12, cursor: 'pointer' }}>
                    🗑 Remove
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
