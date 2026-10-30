// ── Browser-native voice chat helpers (Web Speech API) ──────────────────────
// Zero backend/AWS involvement by design — speech-to-text and text-to-
// speech both run entirely inside the browser's own built-in engine.
// Chrome/Edge (Chromium) support both APIs well; Safari has partial
// support; Firefox does not implement SpeechRecognition at all. Every
// export here is defensive (feature-detected) so callers can just hide
// the relevant UI control when unsupported rather than crashing.
//
// Note: Chromium's SpeechRecognition implementation streams audio to
// Google's speech service to do the actual recognition (it is not fully
// on-device) — acceptable for "Plan A" (see AiAssistantPage.jsx's
// docstring for the full options considered), but if that's ever a
// concern, "Plan B" (AWS Transcribe/Polly via the backend) is the
// documented upgrade path.

export function isSpeechRecognitionSupported() {
  return typeof window !== 'undefined' && !!(window.SpeechRecognition || window.webkitSpeechRecognition)
}

export function isSpeechSynthesisSupported() {
  return typeof window !== 'undefined' && !!window.speechSynthesis
}

/**
 * Create a one-shot speech recognizer. Returns null if unsupported.
 * Call .start() to begin listening, .stop() to end early.
 *
 * onInterim(text): fired repeatedly with the best-guess transcript so far
 *   (not yet final — lets the caller show live "as you speak" text).
 * onFinal(text): fired once recognition settles on a final transcript.
 * onEnd(): fired when the recognizer stops listening (silence timeout,
 *   .stop() called, or after a final result) — always fires eventually.
 * onError(event): fired on failure (e.g. mic permission denied).
 */
export function createSpeechRecognizer({ lang = 'en-US', onInterim, onFinal, onEnd, onError } = {}) {
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!SpeechRecognitionImpl) return null

  const recognizer = new SpeechRecognitionImpl()
  recognizer.lang = lang
  recognizer.continuous = false
  recognizer.interimResults = true
  recognizer.maxAlternatives = 1

  let finalTranscript = ''
  recognizer.onresult = (event) => {
    let interim = ''
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript
      if (event.results[i].isFinal) finalTranscript += transcript
      else interim += transcript
    }
    if (interim) onInterim?.(interim)
    if (finalTranscript) onFinal?.(finalTranscript.trim())
  }
  recognizer.onerror = (event) => onError?.(event)
  recognizer.onend = () => onEnd?.()

  return recognizer
}

/** Speak `text` aloud. Cancels any speech already in progress first, so
 * only one utterance is ever audible at a time. */
export function speak(text, { lang = 'en-US', rate = 1, onEnd, onError } = {}) {
  if (!isSpeechSynthesisSupported() || !text?.trim()) {
    onEnd?.()
    return
  }
  window.speechSynthesis.cancel()
  const utterance = new window.SpeechSynthesisUtterance(text)
  utterance.lang = lang
  utterance.rate = rate
  utterance.onend = () => onEnd?.()
  utterance.onerror = (e) => { onError?.(e); onEnd?.() }
  window.speechSynthesis.speak(utterance)
}

export function stopSpeaking() {
  if (isSpeechSynthesisSupported()) window.speechSynthesis.cancel()
}

// Voice language choices exposed in the UI — English (US) and Cantonese
// (Hong Kong). Chromium's recognizer + synthesizer both support zh-HK.
export const VOICE_LANGUAGES = [
  { value: 'en-US', label: 'English' },
  { value: 'zh-HK', label: '粵語' },
]
