// Jarvis inherent-mode card renderer.
// Loaded by scratch preview shell and (later) by desktop/main.js production shell.

import MarkdownItAsync from 'https://esm.sh/markdown-it-async@2'
import { codeToHtml } from 'https://esm.sh/shiki@3'

const root = document.getElementById('answer')
const card = document.getElementById('card')
const cardWrap = document.getElementById('card-wrap')
const cardInput = document.getElementById('card-input')
const attachmentsWrap = document.getElementById('attachments')
const questionText = document.getElementById('question-text')
const statePill = document.getElementById('state-pill')
const chipsWrap = document.getElementById('chips-wrap')
const chipsContainer = document.getElementById('chips')
const popover = document.getElementById('popover')
const popoverQ = popover.querySelector('.popover-q')
const popoverA = popover.querySelector('.popover-a')
const hoverRegion = document.getElementById('hover-region')
const pillHistory = document.getElementById('pill-history')
const pillClear = document.getElementById('pill-clear')
const pillCount = pillHistory.querySelector('.count')

function setSubmittedQuestion(text) {
  if (!questionText) return
  questionText.textContent = String(text || '').trim()
}

function clearSubmittedQuestion() {
  if (!questionText) return
  questionText.textContent = ''
}

// ─── History strip + popover (Q2.9 ship v1) ────────────────
// In-memory turn log; persists for the lifetime of the card window. Each
// completed turn (siri:done w/ non-empty answer) appends a chip. Strip auto-
// reveals on first chip and stays open. Click any chip → popover slides in
// to the LEFT of the card; window widens via card:setWidth IPC.
const POPOVER_WIDTH = 300
const POPOVER_GAP = 18
const CARD_WIDTH = 360
const CARD_WIDTH_WITH_POPOVER = CARD_WIDTH + POPOVER_GAP + POPOVER_WIDTH
const POPOVER_HIDE_DELAY_MS = 450

let popoverHideTimer = null
function cancelPopoverHide() {
  if (popoverHideTimer) { clearTimeout(popoverHideTimer); popoverHideTimer = null }
}

// Smooth height growth: drives flushHeight() on every frame for `durationMs`
// so the BrowserWindow expands/contracts in step with CSS transitions (chip
// strip max-height open/close = 380ms). Without this, scrollHeight is read
// once and the window snaps at the end instead of growing alongside.
let flushPollHandle = null
function flushHeightFor(durationMs) {
  if (flushPollHandle) cancelAnimationFrame(flushPollHandle)
  const start = performance.now()
  const tick = () => {
    flushHeight()
    if (performance.now() - start < durationMs) {
      flushPollHandle = requestAnimationFrame(tick)
    } else {
      flushPollHandle = null
    }
  }
  flushPollHandle = requestAnimationFrame(tick)
}
function clearSourceActive() {
  chipsContainer.querySelectorAll('.chip.source-active').forEach(c => c.classList.remove('source-active'))
}
function showPopover(chip) {
  cancelPopoverHide()
  popoverQ.textContent = chip.dataset.q || ''
  popoverA.textContent = chip.dataset.fullA || ''
  // Anchor popover top to the clicked chip's top — popover follows whichever
  // chip the user is previewing, so the visual link between source-active
  // chip + popover content reads naturally.
  const chipRect = chip.getBoundingClientRect()
  const wrapRect = cardWrap.getBoundingClientRect()
  popover.style.top = (chipRect.top - wrapRect.top) + 'px'
  popover.classList.add('visible')
  clearSourceActive()
  chip.classList.add('source-active')
  window.cardAPI?.setWidth?.(CARD_WIDTH_WITH_POPOVER)
  // Window widening shifts card position; re-flush so height accounts for popover
  flushHeight()
}
function hidePopoverNow() {
  cancelPopoverHide()
  popover.classList.remove('visible')
  clearSourceActive()
  window.cardAPI?.setWidth?.(CARD_WIDTH)
  flushHeight()
}
function schedulePopoverHide() {
  cancelPopoverHide()
  popoverHideTimer = setTimeout(() => {
    popover.classList.remove('visible')
    clearSourceActive()
    window.cardAPI?.setWidth?.(CARD_WIDTH)
    flushHeight()
    popoverHideTimer = null
  }, POPOVER_HIDE_DELAY_MS)
}
popover.addEventListener('mouseenter', cancelPopoverHide)
popover.addEventListener('mouseleave', schedulePopoverHide)

function buildChip(q, a) {
  const chip = document.createElement('div')
  chip.className = 'chip tally-fresh'
  // chip-a in strip = single-line answer preview (no markdown). Use first line.
  const previewA = String(a).split(/\r?\n/)[0]
  chip.innerHTML =
    `<span class="chip-q"></span><span class="chip-arrow">→</span><span class="chip-a"></span>`
  chip.querySelector('.chip-q').textContent = q
  chip.querySelector('.chip-a').textContent = previewA
  chip.dataset.q = q
  chip.dataset.fullA = a
  chip.addEventListener('click', () => showPopover(chip))
  chip.addEventListener('mouseleave', schedulePopoverHide)
  // tally-fresh stripe auto-fades over 1.1s — strip the class so it doesn't
  // re-trigger if the chip is re-rendered.
  setTimeout(() => chip.classList.remove('tally-fresh'), 1100)
  return chip
}

function updatePillCount() {
  const n = chipsContainer.querySelectorAll('.chip:not(.fading)').length
  pillCount.textContent = String(n)
  // Pill stays hover-only — no auto-reveal. The user must hover the region
  // above the card top to see / interact with the history affordance.
}
function setPillCount(n) {
  pillCount.textContent = String(n)
}

function pushTurn(q, a) {
  if (!q || !a) return
  const chip = buildChip(q, a)
  chipsContainer.appendChild(chip)
  card.classList.add('history-shown')
  updatePillCount()
  requestAnimationFrame(() => {
    chipsWrap.scrollTop = chipsWrap.scrollHeight
  })
  // Drive resize per-frame so the window grows in step with the strip's
  // 380ms max-height transition instead of snapping at the end.
  flushHeightFor(420)
}

function cascadeClear() {
  hidePopoverNow()
  const chips = Array.from(chipsContainer.querySelectorAll('.chip'))
  card.classList.remove('history-shown')
  if (chips.length === 0) {
    updatePillCount()
    flushHeightFor(420)
    return
  }
  setPillCount(0)
  // Stagger the fade-out from newest to oldest, then wipe DOM after the last
  // chip's transition finishes. Drive resize per-frame for the full duration.
  chips.slice().reverse().forEach((c, i) => {
    setTimeout(() => c.classList.add('fading'), i * 50)
  })
  const totalMs = chips.length * 50 + 380
  setTimeout(() => {
    chips.forEach(c => c.remove())
    updatePillCount()
  }, totalMs)
  flushHeightFor(totalMs + 60)
}

// Pill wiring (idempotent: card.js loads once per renderer)
pillHistory.addEventListener('click', () => {
  pillHistory.classList.add('click-flash')
  setTimeout(() => pillHistory.classList.remove('click-flash'), 240)
  if (card.classList.contains('history-shown')) {
    card.classList.remove('history-shown')
    hidePopoverNow()
  } else if (chipsContainer.querySelectorAll('.chip').length > 0) {
    card.classList.add('history-shown')
    requestAnimationFrame(() => { chipsWrap.scrollTop = chipsWrap.scrollHeight })
  }
  // Smooth growth: per-frame flushHeight for the strip's full 380ms transition.
  flushHeightFor(420)
})
pillClear.addEventListener('click', () => {
  pillClear.classList.add('click-flash')
  setTimeout(() => pillClear.classList.remove('click-flash'), 240)
  cascadeClear()
})

// Track Q for the in-flight turn so we can pair it with A on siri:done.
let inFlightQ = null
let stagedImage = null

const IMAGE_MAX_BYTES = 15 * 1024 * 1024
const IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])

function attachmentQuestionLabel(text) {
  return text || '请看这张图片'
}

function formatImageBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return ''
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))}KB`
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)}MB`
}

function normalizeImageName(file, source) {
  const raw = String(file?.name || '').trim()
  if (source === 'paste' || !raw || /^image\.(png|jpe?g|webp|gif)$/i.test(raw)) return 'screen'
  return raw.length > 28 ? `${raw.slice(0, 24)}…` : raw
}

function imageMimeType(file) {
  const mime = String(file?.type || '').split(';')[0].trim().toLowerCase()
  if (mime === 'image/jpg') return 'image/jpeg'
  return mime
}

function flashAttachmentEdge() {
  card.classList.remove('edge-flash')
  void card.offsetWidth
  card.classList.add('edge-flash')
  setTimeout(() => card.classList.remove('edge-flash'), 720)
}

function imageStageError(message = 'image failed') {
  card.classList.remove('drop-target')
  setState(message, 'warn')
  flashAttachmentEdge()
  flushHeightFor(420)
}

async function imageDimensions(file) {
  if (!file) return null
  if (typeof createImageBitmap === 'function') {
    try {
      const bitmap = await createImageBitmap(file)
      const dims = { width: bitmap.width, height: bitmap.height }
      bitmap.close?.()
      return dims
    } catch {
      // Fall back to HTMLImageElement below.
    }
  }
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      const dims = { width: img.naturalWidth || img.width, height: img.naturalHeight || img.height }
      URL.revokeObjectURL(url)
      resolve(dims.width && dims.height ? dims : null)
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      resolve(null)
    }
    img.src = url
  })
}

function renderAttachmentChip() {
  if (!attachmentsWrap) return
  attachmentsWrap.innerHTML = ''
  if (!stagedImage) return

  const chip = document.createElement('span')
  chip.className = 'attach-chip'
  chip.innerHTML =
    '<span class="a-icon"></span>' +
    '<span class="a-name"></span>' +
    '<span class="a-meta"></span>' +
    '<button class="a-remove" type="button" aria-label="Remove image">×</button>'
  chip.querySelector('.a-name').textContent = stagedImage.label
  chip.querySelector('.a-meta').textContent = stagedImage.meta || ''
  chip.querySelector('.a-remove').addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    clearStagedImage()
    requestAnimationFrame(() => cardInput.focus())
  })
  attachmentsWrap.appendChild(chip)
  requestAnimationFrame(() => chip.classList.add('in'))
}

function clearStagedImage() {
  stagedImage = null
  if (attachmentsWrap) attachmentsWrap.innerHTML = ''
  flushHeight()
}

function restoreStagedImage(snapshot) {
  stagedImage = snapshot || null
  renderAttachmentChip()
}

function canStageImage() {
  return !['submitting', 'streaming', 'listening', 'transcribing', 'transition'].includes(turnPhase)
}

async function stageImageFile(file, source = 'drop') {
  if (!file || !canStageImage()) return false
  const mime = imageMimeType(file)
  if (!IMAGE_MIME_TYPES.has(mime)) {
    imageStageError('image only')
    return false
  }
  if (file.size > IMAGE_MAX_BYTES) {
    imageStageError(`image > ${formatImageBytes(IMAGE_MAX_BYTES)}`)
    return false
  }
  if (cardInput.disabled && canEnterFollowupInput()) {
    enterInputMode({ followup: true })
    await delay(FOLLOWUP_ENTER_MS + 40)
  } else if (cardInput.disabled) {
    enterInputMode()
    await delay(80)
  }

  const dims = await imageDimensions(file)
  const buffer = await file.arrayBuffer()
  const meta = dims?.width && dims?.height
    ? `${dims.width}×${dims.height}`
    : formatImageBytes(file.size)
  stagedImage = {
    label: normalizeImageName(file, source),
    meta,
    mime,
    name: file.name || `${source}.png`,
    bytes: file.size,
    buffer,
  }
  renderAttachmentChip()
  flashAttachmentEdge()
  inputActive = true
  turnPhase = 'input'
  flushHeightFor(420)
  requestAnimationFrame(() => cardInput.focus())
  return true
}

function firstImageFile(fileList) {
  for (const file of Array.from(fileList || [])) {
    if (IMAGE_MIME_TYPES.has(imageMimeType(file))) return file
  }
  return null
}

function firstImageItem(itemList) {
  for (const item of Array.from(itemList || [])) {
    if (item?.kind !== 'file') continue
    const type = String(item.type || '').toLowerCase()
    if (!IMAGE_MIME_TYPES.has(type)) continue
    const file = item.getAsFile?.()
    if (file) return file
  }
  return null
}

function firstImageFromTransfer(transfer) {
  return firstImageFile(transfer?.files) || firstImageItem(transfer?.items)
}

function transferHasFile(transfer) {
  return Array.from(transfer?.items || []).some(item => item?.kind === 'file') ||
    (transfer?.files?.length || 0) > 0
}

function arrayBufferFromBase64(base64) {
  const binary = atob(String(base64 || ''))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes.buffer
}

window.jarvisStageImageAttachment = async (payload = {}) => {
  const mime = String(payload.mime || 'image/png')
  const name = String(payload.name || 'image.png')
  const source = String(payload.source || 'drop')
  const buffer = arrayBufferFromBase64(payload.base64 || '')
  const file = new File([buffer], name, { type: mime })
  return stageImageFile(file, source)
}

window.jarvisImageAttachmentError = (payload = {}) => {
  const message = String(payload.message || 'image failed')
  imageStageError(message)
  return true
}

function dragHasImage(e) {
  const items = Array.from(e.dataTransfer?.items || [])
  if (items.some(item => item.kind === 'file' && IMAGE_MIME_TYPES.has(String(item.type || '').toLowerCase()))) {
    return true
  }
  return !!firstImageFile(e.dataTransfer?.files)
}

async function handleImagePaste(e) {
  if (!canStageImage()) return
  const file = firstImageFromTransfer(e.clipboardData)
  if (!file) return
  e.preventDefault()
  await stageImageFile(file, 'paste')
}

function pasteTextIntoActiveElement(text) {
  const value = String(text || '')
  if (!value) return false
  const el = document.activeElement
  if (!el || !('value' in el)) return false
  const start = Number.isFinite(el.selectionStart) ? el.selectionStart : String(el.value || '').length
  const end = Number.isFinite(el.selectionEnd) ? el.selectionEnd : start
  const before = String(el.value || '').slice(0, start)
  const after = String(el.value || '').slice(end)
  el.value = `${before}${value}${after}`
  const next = start + value.length
  el.setSelectionRange?.(next, next)
  el.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    inputType: 'insertFromPaste',
    data: value,
  }))
  return true
}

window.jarvisPastePlainText = (payload = {}) => {
  const text = typeof payload === 'string' ? payload : payload.text
  return pasteTextIntoActiveElement(text)
}

async function handleNativePasteShortcut(e) {
  if (keyboardEventIsComposing(e)) return
  if (!(e.metaKey || e.ctrlKey)) return
  if (String(e.key || '').toLowerCase() !== 'v') return
  if (!window.cardAPI?.pasteClipboard) return
  e.preventDefault()
  try {
    const result = await window.cardAPI.pasteClipboard()
    if (result?.text) pasteTextIntoActiveElement(result.text)
  } catch (err) {
    console.warn('[card] native paste failed:', err?.message || err)
  }
}

function handleImageDragOver(e) {
  if (!canStageImage() || (!dragHasImage(e) && !transferHasFile(e.dataTransfer))) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  card.classList.add('drop-target')
}

function handleImageDragLeave(e) {
  if (card.contains(e.relatedTarget)) return
  card.classList.remove('drop-target')
}

async function handleImageDrop(e) {
  card.classList.remove('drop-target')
  if (!canStageImage() || (!dragHasImage(e) && !transferHasFile(e.dataTransfer))) return
  e.preventDefault()
  const file = firstImageFromTransfer(e.dataTransfer)
  if (!file) return
  await stageImageFile(file, 'drop')
}

const md = new MarkdownItAsync({
  html: false,
  linkify: true,
  breaks: true,
  highlight: async (code, lang) => {
    if (!lang) return null
    try {
      return await codeToHtml(code.trimEnd(), {
        lang,
        theme: 'github-dark-default'
      })
    } catch (e) {
      console.warn('[card] shiki highlight failed:', lang, e?.message)
      return null
    }
  }
})

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const params = new URLSearchParams(window.location.search)
const demo = params.get('demo')
const animateChars = params.get('animateChars') === '1'

let fadeTimer = null
let contentGen = 0
const FADE_OUT_MS = 280

// ─── Streaming state (Mode A: token-stream → drip-rendered) ───
// target = full text received from backend so far
// shown  = text currently rendered to DOM (drip lags target)
// dripTimer steps shown forward 1 char at a time, re-rendering markdown
// each step. Decouples bursty backend chunks from steady frontend cadence.
let target = ''
let shown = ''
let dripTimer = null
const DRIP_MS = 30
const CATCHUP_MS = 8
const CATCHUP_THRESHOLD = 40
const FADE_CHAR_MS = 250

// Per-char birth-time tracking (drip-fade animation).
// charBirthTimes[i] = ms timestamp when visible char at index i first appeared.
// Indexed by *current* visible char position; updated each render by diffing
// previous visible text against current via prefix + suffix(with marker-trim).
// Marker-trim strips trailing inline-md markers from prev so a `**bold**`
// closure that drops the `**` chars still aligns "bold" suffix between prev
// and curr — preserving birth times for chars that survived the structure
// change. Without this, those chars would get fresh `now` birth times and
// re-fade, which is the visible flicker.
let prevVisibleText = ''
let charBirthTimes = []
const INLINE_MARKER_TAIL = /[*_~`\[\]()!]+$/u

function clearDrip() {
  if (dripTimer) {
    clearTimeout(dripTimer)
    dripTimer = null
  }
}

function startDrip() {
  if (dripTimer) return
  const tick = async () => {
    dripTimer = null
    if (shown.length >= target.length) return
    shown = target.slice(0, shown.length + 1)
    await setContent(shown, { animate: animateChars })
    if (shown.length >= target.length) return
    const lag = target.length - shown.length
    const delay = lag > CATCHUP_THRESHOLD ? CATCHUP_MS : DRIP_MS
    dripTimer = setTimeout(tick, delay)
  }
  tick()
}

async function setContent(markdown, { animate = false } = {}) {
  // gen token guards against late-arriving md.renderAsync results overwriting fresher content
  const myGen = ++contentGen
  cancelFade()
  if (!markdown) {
    root.innerHTML = ''
    return
  }
  let html
  try {
    html = await md.renderAsync(markdown)
  } catch (e) {
    console.warn('[card] markdown render failed; falling back to <pre>:', e?.message)
    html = `<pre>${escapeHtml(markdown)}</pre>`
  }
  if (myGen !== contentGen) return
  root.innerHTML = html
  if (animate) {
    const currVisible = root.textContent
    charBirthTimes = diffBirthTimes(prevVisibleText, charBirthTimes, currVisible)
    prevVisibleText = currVisible
    applyCharAnimations(root, charBirthTimes)
  }
  await flushHeight()
}

// Diff prev/curr visible text via common-prefix + (marker-trimmed) common-suffix
// to produce a birth-time array aligned to curr's char positions.
//   - chars in the common prefix: inherit birth time at same index
//   - chars in the common suffix: inherit birth time from prev's mapped index
//     (after stripping trailing inline-md markers from prev so a closing `**`
//     doesn't break suffix alignment for "Bold" that survives the structure
//     change)
//   - chars in the middle (genuinely new in curr): get a fresh `now` timestamp
function diffBirthTimes(prevText, prevTimes, currText) {
  const now = Date.now()
  const out = new Array(currText.length)

  // Common prefix
  let pre = 0
  const minLen = Math.min(prevText.length, currText.length)
  while (pre < minLen && prevText[pre] === currText[pre]) pre += 1
  for (let i = 0; i < pre; i += 1) out[i] = prevTimes[i]

  // Trim trailing inline markers from prev for fairer suffix match
  let prevTrimmed = prevText
  const tail = INLINE_MARKER_TAIL.exec(prevTrimmed.slice(pre))
  if (tail && tail[0].length > 0) {
    prevTrimmed = prevTrimmed.slice(0, prevTrimmed.length - tail[0].length)
  }

  // Common suffix (between pre and end of each)
  let suf = 0
  while (
    suf < prevTrimmed.length - pre &&
    suf < currText.length - pre &&
    prevTrimmed[prevTrimmed.length - 1 - suf] === currText[currText.length - 1 - suf]
  ) suf += 1
  for (let i = 0; i < suf; i += 1) {
    const currIdx = currText.length - 1 - i
    const prevIdx = prevTrimmed.length - 1 - i
    if (out[currIdx] === undefined) out[currIdx] = prevTimes[prevIdx]
  }

  // Middle (genuinely new in curr): fresh timestamp
  for (let i = pre; i < currText.length - suf; i += 1) {
    if (out[i] === undefined) out[i] = now
  }
  // Defensive: any unset slot (shouldn't happen) gets `now`
  for (let i = 0; i < out.length; i += 1) {
    if (out[i] === undefined) out[i] = now
  }
  return out
}

// Walk text nodes, wrap each visible char in a span with animation-delay
// based on its age (now - birthTime). Chars whose age >= FADE_CHAR_MS skip
// the wrap entirely and stay as plain text — keeps DOM compact for long
// content (only the trailing ~8 chars wear spans at any time).
function applyCharAnimations(rootElem, times) {
  const now = Date.now()
  const walker = document.createTreeWalker(rootElem, NodeFilter.SHOW_TEXT)
  const textNodes = []
  while (walker.nextNode()) {
    if (walker.currentNode.textContent.length > 0) {
      textNodes.push(walker.currentNode)
    }
  }
  let charIdx = 0
  for (const node of textNodes) {
    const text = node.textContent
    const fragment = document.createDocumentFragment()
    let dirty = false
    for (const ch of text) {
      const t = times[charIdx]
      const age = t === undefined ? FADE_CHAR_MS : (now - t)
      if (age >= FADE_CHAR_MS) {
        fragment.appendChild(document.createTextNode(ch))
      } else {
        const span = document.createElement('span')
        span.className = 'char-fadein'
        span.style.animationDelay = `-${age}ms`
        span.textContent = ch
        fragment.appendChild(span)
        dirty = true
      }
      charIdx += 1
    }
    // Only replace if at least one char in this node animates — keeps
    // already-stable text nodes untouched (their identity persists across
    // renders, helping the browser preserve paint state).
    if (dirty) node.parentNode.replaceChild(fragment, node)
  }
}

async function flushHeight() {
  if (document.fonts?.ready) {
    try { await document.fonts.ready } catch {}
  }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
  // Lift answer max-height before measuring: scrollHeight respects max-height,
  // so without this long content reports the clamped value and the window never
  // grows to the main-process 800px cap.
  const prevMax = root.style.maxHeight
  root.style.maxHeight = 'none'
  void root.offsetHeight
  // Measure body (content-driven). documentElement.scrollHeight can be pinned
  // to viewport in Chromium and won't shrink when content is smaller than the
  // current window bounds; body.scrollHeight tracks the actual content extent.
  let height = Math.ceil(document.body.scrollHeight)
  // Popover is position:absolute → doesn't contribute to scrollHeight. When
  // visible, ensure window is tall enough to fit it (popover.bottom relative
  // to body top).
  if (popover.classList.contains('visible')) {
    const popRect = popover.getBoundingClientRect()
    height = Math.max(height, Math.ceil(popRect.bottom + 4))
  }
  root.style.maxHeight = prevMax
  window.cardAPI?.resize(height)
  window.cardAPI?.show?.()
}

function scheduleFade(ms = 5000) {
  cancelFade()
  fadeTimer = setTimeout(() => {
    // Hand fade-out to main process so glass + content fade together
    window.cardAPI?.fadeOut(FADE_OUT_MS)
  }, ms)
}

function cancelFade() {
  if (fadeTimer) {
    clearTimeout(fadeTimer)
    fadeTimer = null
  }
}

document.addEventListener('mouseenter', () => {
  cancelFade()
  // Also abort any in-flight main-process fade tick chain
  window.cardAPI?.cancelFade?.()
})
document.addEventListener('mouseleave', () => scheduleFade(2500))

window.jarvisCard = { setContent, cancelFade, scheduleFade, pushTurn, hidePopoverNow }

// ─── State pill (motion-reel L1806-1817 port) ──────────────
const STATE_VARIANTS = ['thinking', 'idle', 'warn', 'error', 'success', 'neutral']
function setState(label, variant = '') {
  if (!label) {
    statePill.classList.remove('visible')
    statePill.textContent = ''
    STATE_VARIANTS.forEach(v => statePill.classList.remove(v))
    return
  }
  statePill.textContent = label
  STATE_VARIANTS.forEach(v => statePill.classList.remove(v))
  if (variant) statePill.classList.add(variant)
  statePill.classList.add('visible')
}

// Thinking aura helpers (motion-reel L1864-1874 port)
function thinkFluent() {
  card.classList.remove('thinking-deep')
  card.classList.add('thinking-fluent')
}
function thinkOff() {
  card.classList.remove('thinking-fluent', 'thinking-deep')
}

// ─── Input lifecycle ───────────────────────────────────────
// inputActive tracks whether the most recent siri:open was triggered by a
// local submit (true) or arrived from a non-input source like voice (false).
// This lets siri:open differentiate "user typed → continue showing breadcrumb"
// from "external response → just render answer".
let inputActive = false
// streamingStarted flips true when the first token appends in the current
// turn — used to defer thinkOff + state→streaming until real content arrives
// (response.start fires when the LLM API connection opens, often noticeably
// before the first token, so transitioning at .start makes the thinking aura
// look like it stops "too early").
let streamingStarted = false
let turnPhase = 'idle'
let inputTransitionGen = 0
let followupSnapshot = null
let followupDraftActive = false
const FOLLOWUP_ENTER_MS = 240
const FOLLOWUP_RESTORE_MS = 340
const VOICE_HOLD_MS = 220
const VOICE_FOLLOWUP_START_DELAY_MS = FOLLOWUP_ENTER_MS + 80
const VOICE_MIN_WAV_BYTES = 48

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

class InherentVoiceCapture {
  constructor() {
    this.context = null
    this.workletReady = false
    this.stream = null
    this.source = null
    this.node = null
    this.sink = null
    this.chunks = []
    this.sampleRate = 16000
    this.recording = false
  }

  async start() {
    if (this.recording) return
    const AudioContextCtor = window.AudioContext || window.webkitAudioContext
    if (!AudioContextCtor) throw new Error('AudioContext unavailable')
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('getUserMedia unavailable')

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    })
    if (!this.context) this.context = new AudioContextCtor()
    if (this.context.state === 'suspended') await this.context.resume()
    if (!this.workletReady) {
      const workletCode = `
        class VoiceCaptureProcessor extends AudioWorkletProcessor {
          process(inputs) {
            const input = inputs[0]
            const channel = input && input[0]
            if (!channel) return true
            const samples = new Int16Array(channel.length)
            for (let i = 0; i < channel.length; i += 1) {
              const s = Math.max(-1, Math.min(1, channel[i]))
              samples[i] = s < 0 ? s * 0x8000 : s * 0x7fff
            }
            this.port.postMessage(samples, [samples.buffer])
            return true
          }
        }
        registerProcessor('voice-capture-processor', VoiceCaptureProcessor)
      `
      const workletUrl = URL.createObjectURL(new Blob([workletCode], { type: 'application/javascript' }))
      try {
        await this.context.audioWorklet.addModule(workletUrl)
        this.workletReady = true
      } finally {
        URL.revokeObjectURL(workletUrl)
      }
    }

    this.chunks = []
    this.sampleRate = this.context.sampleRate || 16000
    this.source = this.context.createMediaStreamSource(this.stream)
    this.node = new AudioWorkletNode(this.context, 'voice-capture-processor')
    this.node.port.onmessage = (event) => {
      const chunk = event.data instanceof Int16Array
        ? event.data
        : new Int16Array(event.data?.buffer || event.data)
      if (chunk.length) this.chunks.push(chunk)
    }
    this.sink = this.context.createGain()
    this.sink.gain.value = 0
    this.source.connect(this.node)
    this.node.connect(this.sink)
    this.sink.connect(this.context.destination)
    this.recording = true
  }

  async stop() {
    if (!this.recording && !this.stream) return null
    this.recording = false
    try { this.source?.disconnect?.() } catch (err) {}
    try { this.node?.disconnect?.() } catch (err) {}
    try { this.sink?.disconnect?.() } catch (err) {}
    if (this.node) this.node.port.onmessage = null
    this.stream?.getTracks?.().forEach(track => track.stop())
    const blob = this.buildWav()
    this.stream = null
    this.source = null
    this.node = null
    this.sink = null
    this.chunks = []
    return blob
  }

  buildWav() {
    const totalSamples = this.chunks.reduce((sum, chunk) => sum + chunk.length, 0)
    const buffer = new ArrayBuffer(44 + totalSamples * 2)
    const view = new DataView(buffer)
    const writeString = (offset, value) => {
      for (let i = 0; i < value.length; i += 1) {
        view.setUint8(offset + i, value.charCodeAt(i))
      }
    }
    const sampleRate = Math.max(1, Math.round(this.sampleRate || 16000))
    writeString(0, 'RIFF')
    view.setUint32(4, 36 + totalSamples * 2, true)
    writeString(8, 'WAVE')
    writeString(12, 'fmt ')
    view.setUint32(16, 16, true)
    view.setUint16(20, 1, true)
    view.setUint16(22, 1, true)
    view.setUint32(24, sampleRate, true)
    view.setUint32(28, sampleRate * 2, true)
    view.setUint16(32, 2, true)
    view.setUint16(34, 16, true)
    writeString(36, 'data')
    view.setUint32(40, totalSamples * 2, true)
    let offset = 44
    for (const chunk of this.chunks) {
      for (let i = 0; i < chunk.length; i += 1) {
        view.setInt16(offset, chunk[i], true)
        offset += 2
      }
    }
    return new Blob([buffer], { type: 'audio/wav' })
  }
}

let enterHoldState = null
let voiceCapture = null
let voiceListening = false
let voiceStartGen = 0
let voiceInputSnapshot = null
let voiceAudioDucked = false

function keyboardEventIsComposing(e) {
  // IME commit emits keydown with keyCode=229 on WebKit even after
  // compositionend has fired (isComposing is false at that point), so both
  // checks are needed to avoid pinyin text getting submitted.
  return e.isComposing || e.keyCode === 229
}

function isEditableTarget(target) {
  return !!target?.closest?.('input, textarea, [contenteditable="true"]')
}

function canEnterFollowupInput() {
  if (!cardInput.disabled) return false
  if (turnPhase === 'submitting' || turnPhase === 'streaming' || streamingStarted) return false
  if (card.classList.contains('thinking-fluent') || card.classList.contains('thinking-deep')) return false
  if (turnPhase === 'done' || turnPhase === 'error') return true
  // DOM recovery path: hot reloads or module re-evaluation can reset JS-only
  // turnPhase while leaving the rendered completed answer on screen. Treat a
  // disabled submitted card with visible answer content as a completed turn.
  return root.textContent.trim().length > 0
}

function clearAnswerContent() {
  contentGen += 1
  root.innerHTML = ''
  prevVisibleText = ''
  charBirthTimes = []
}

function currentStateVariant() {
  return STATE_VARIANTS.find(v => statePill.classList.contains(v)) || ''
}

function captureResponseSnapshot() {
  const visibleText = root.textContent || ''
  const answerText = target || shown || visibleText
  if (!answerText.trim() && !visibleText.trim()) return null
  return {
    inputValue: cardInput.value,
    inputPlaceholder: cardInput.placeholder,
    answerHtml: root.innerHTML,
    answerText,
    visibleText,
    prevVisibleText,
    charBirthTimes: charBirthTimes.slice(),
    attachment: stagedImage,
    stateLabel: statePill.textContent || (turnPhase === 'error' ? 'error' : 'done'),
    stateVariant: currentStateVariant() || (turnPhase === 'error' ? 'error' : 'success'),
    phase: turnPhase === 'error' ? 'error' : 'done'
  }
}

function clearFollowupDraft() {
  followupSnapshot = null
  followupDraftActive = false
}

function restoreFollowupSnapshot() {
  if (!followupSnapshot) return false
  const snapshot = followupSnapshot
  const gen = ++inputTransitionGen
  cancelFade()
  window.cardAPI?.cancelFade?.()
  clearDrip()
  hidePopoverNow()

  followupDraftActive = false
  cardInput.value = snapshot.inputValue || ''
  setSubmittedQuestion(cardInput.value)
  restoreStagedImage(snapshot.attachment || null)
  cardInput.placeholder = snapshot.inputPlaceholder || '问点什么…'
  cardInput.disabled = true
  cardInput.blur()
  card.classList.remove('followup-entering', 'followup-input', 'listening', 'warn', 'error')
  card.classList.add('submitted', 'followup-restoring')
  thinkOff()
  setState(snapshot.stateLabel || 'done', snapshot.stateVariant || 'success')

  contentGen += 1
  target = snapshot.answerText || snapshot.visibleText || ''
  shown = target
  prevVisibleText = snapshot.prevVisibleText || snapshot.visibleText || target
  charBirthTimes = snapshot.charBirthTimes.slice()
  root.innerHTML = snapshot.answerHtml || ''
  if (!root.innerHTML && target) root.textContent = target

  inputActive = false
  streamingStarted = false
  turnPhase = snapshot.phase || 'done'
  flushHeight()
  flushHeightFor(FOLLOWUP_RESTORE_MS + 220)
  setTimeout(() => {
    if (gen === inputTransitionGen) card.classList.remove('followup-restoring')
  }, FOLLOWUP_RESTORE_MS)
  scheduleFade(5000)
  return true
}

function resetInputState({ placeholder = '问点什么…' } = {}) {
  cardInput.value = ''
  clearStagedImage()
  clearSubmittedQuestion()
  cardInput.disabled = false
  cardInput.placeholder = placeholder
  card.classList.remove('submitted', 'listening', 'warn', 'error', 'followup-entering', 'followup-input', 'followup-restoring')
  thinkOff()
}

function enterInputMode({ followup = false } = {}) {
  const gen = ++inputTransitionGen
  if (followup) {
    followupSnapshot = captureResponseSnapshot()
    followupDraftActive = false
  } else {
    clearFollowupDraft()
  }
  cancelFade()
  window.cardAPI?.cancelFade?.()
  clearDrip()
  target = ''
  shown = ''
  prevVisibleText = ''
  charBirthTimes = []
  hidePopoverNow()

  const finish = () => {
    if (gen !== inputTransitionGen) return
    resetInputState({ placeholder: followup ? '继续问…' : '问点什么…' })
    if (followup) card.classList.add('followup-input')
    setState(followup ? 'input' : 'idle', followup ? '' : 'idle')
    clearAnswerContent()
    flushHeight()
    // .row has a 0.32s height transition and .answer a 0.5s max-height
    // transition. Poll through the transition so the native panel shrinks in
    // step instead of measuring one mid-animation frame and leaving overflow.
    flushHeightFor(followup ? 700 : 540)
    requestAnimationFrame(() => cardInput.focus())
    inputActive = true
    turnPhase = 'input'
    followupDraftActive = followup && !!followupSnapshot
  }

  if (followup && card.classList.contains('submitted')) {
    card.classList.add('followup-entering')
    setState('input')
    turnPhase = 'transition'
    flushHeightFor(FOLLOWUP_ENTER_MS + 160)
    setTimeout(finish, FOLLOWUP_ENTER_MS)
  } else {
    finish()
  }
}

function submitInputText() {
  const text = cardInput.value.trim()
  const image = stagedImage
  if (!text && !image) {
    if (followupDraftActive) restoreFollowupSnapshot()
    return
  }
  const displayText = attachmentQuestionLabel(text)
  inputTransitionGen += 1
  clearFollowupDraft()
  inFlightQ = displayText
  setSubmittedQuestion(displayText)
  cardInput.disabled = true
  card.classList.remove('followup-entering', 'followup-input', 'followup-restoring')
  card.classList.add('submitted')
  setState('thinking', 'thinking')
  thinkFluent()
  turnPhase = 'submitting'
  // Width-morph (Q2 = B): the input element itself shrinks into the breadcrumb
  // via .submitted CSS — no separate node, no DOM swap. The streaming answer
  // expands below in the existing #answer pane.
  const submitPromise = image
    ? window.cardAPI?.submitImage?.({
        text,
        name: image.name,
        mime: image.mime,
        buffer: image.buffer,
      })
    : window.cardAPI?.submit?.(text)
  Promise.resolve(submitPromise).then(result => {
    // Surface backend reachability problems to the user. Without this the
    // pill stays at "thinking" forever when the POST fails (server down,
    // network gone, http error), since no siri:* events will ever arrive.
    if (!result || result.ok === false) {
      thinkOff()
      const reason = result?.reason || 'unknown'
      const label = reason === 'network' ? 'offline' : `error · ${reason}`
      setState(label, 'error')
      turnPhase = 'error'
      // Auto-clear after a beat so the user can try again without a manual reset.
      scheduleFade(3000)
    }
  }).catch(err => {
    thinkOff()
    setState('error', 'error')
    turnPhase = 'error'
    scheduleFade(3000)
    console.warn('[card] submit failed:', err?.message)
  })
}

function getVoiceCapture() {
  if (!voiceCapture) voiceCapture = new InherentVoiceCapture()
  return voiceCapture
}

async function duckSystemAudioForVoice() {
  if (voiceAudioDucked) return
  voiceAudioDucked = true
  try {
    const result = await window.cardAPI?.duckAudio?.()
    if (result && result.ok === false) {
      voiceAudioDucked = false
      console.warn('[card] audio duck unavailable:', result.reason || 'unknown')
    }
  } catch (err) {
    voiceAudioDucked = false
    console.warn('[card] audio duck failed:', err?.message)
  }
}

async function restoreSystemAudioForVoice() {
  if (!voiceAudioDucked) return
  voiceAudioDucked = false
  try {
    await window.cardAPI?.restoreAudio?.()
  } catch (err) {
    console.warn('[card] audio restore failed:', err?.message)
  }
}

window.addEventListener('pagehide', () => {
  if (!voiceAudioDucked) return
  voiceAudioDucked = false
  window.cardAPI?.restoreAudio?.()
})

function canStartVoiceHold() {
  if (voiceListening) return false
  if (stagedImage) return false
  if (turnPhase === 'submitting' || turnPhase === 'streaming') return false
  if (turnPhase === 'listening' || turnPhase === 'transcribing') return false
  if (card.classList.contains('thinking-fluent') || card.classList.contains('thinking-deep')) return false
  return true
}

function restoreVoiceDraftForRetry() {
  const snapshot = voiceInputSnapshot
  cardInput.value = snapshot?.value || ''
  cardInput.placeholder = snapshot?.placeholder || (followupDraftActive ? '继续问…' : '问点什么…')
  voiceInputSnapshot = null
}

function returnToVoiceRetryState(label, variant) {
  thinkOff()
  card.classList.remove('listening')
  restoreVoiceDraftForRetry()
  cardInput.disabled = false
  setState(label, variant)
  inputActive = true
  turnPhase = 'input'
  flushHeight()
  requestAnimationFrame(() => cardInput.focus())
}

async function beginEnterVoiceCapture() {
  if (!canStartVoiceHold()) return false
  const gen = ++voiceStartGen
  try {
    cancelFade()
    window.cardAPI?.cancelFade?.()
    hidePopoverNow()

    if (canEnterFollowupInput()) {
      enterInputMode({ followup: true })
      await delay(VOICE_FOLLOWUP_START_DELAY_MS)
    } else if (cardInput.disabled) {
      enterInputMode()
      await delay(80)
    }
    if (gen !== voiceStartGen) return false

    voiceInputSnapshot = {
      value: cardInput.value,
      placeholder: cardInput.placeholder
    }
    voiceListening = true
    cardInput.value = ''
    cardInput.placeholder = '正在听…'
    cardInput.disabled = true
    card.classList.remove('warn', 'error')
    card.classList.add('listening')
    setState('listening')
    turnPhase = 'listening'
    flushHeight()

    await duckSystemAudioForVoice()
    await getVoiceCapture().start()
    if (gen !== voiceStartGen) {
      await getVoiceCapture().stop()
      await restoreSystemAudioForVoice()
      return false
    }
    return true
  } catch (err) {
    await restoreSystemAudioForVoice()
    voiceListening = false
    card.classList.remove('listening')
    restoreVoiceDraftForRetry()
    cardInput.disabled = false
    thinkOff()
    const label = err?.name === 'NotAllowedError' ? 'mic denied' : 'mic error'
    setState(label, 'error')
    turnPhase = 'input'
    flushHeight()
    requestAnimationFrame(() => cardInput.focus())
    console.warn('[card] voice capture failed:', err?.message)
    return false
  }
}

async function finishEnterVoiceCapture() {
  if (!voiceListening) return
  voiceListening = false
  voiceStartGen += 1
  card.classList.remove('listening')

  let wavBlob = null
  try {
    wavBlob = await getVoiceCapture().stop()
  } catch (err) {
    returnToVoiceRetryState('mic error', 'error')
    console.warn('[card] voice stop failed:', err?.message)
    return
  } finally {
    await restoreSystemAudioForVoice()
  }

  if (!wavBlob || wavBlob.size <= VOICE_MIN_WAV_BYTES) {
    returnToVoiceRetryState('no speech', 'warn')
    return
  }

  cardInput.value = ''
  cardInput.placeholder = '识别中…'
  cardInput.disabled = true
  setState('transcribing', 'neutral')
  turnPhase = 'transcribing'
  flushHeight()

  try {
    const result = await window.cardAPI?.submitVoice?.(await wavBlob.arrayBuffer())
    if (!result || result.ok === false) {
      const reason = result?.reason || 'unknown'
      const label = reason === 'network' ? 'offline' : `error · ${reason}`
      returnToVoiceRetryState(label, 'error')
      return
    }

    const text = String(result.text || '').trim()
    if (result.status === 'empty' || !text) {
      returnToVoiceRetryState('no speech', 'warn')
      return
    }

    voiceInputSnapshot = null
    clearFollowupDraft()
    inFlightQ = text
    cardInput.value = text
    setSubmittedQuestion(text)
    cardInput.placeholder = '问点什么…'
    cardInput.disabled = true
    card.classList.remove('followup-entering', 'followup-input', 'followup-restoring', 'warn', 'error')
    card.classList.add('submitted')
    inputActive = true
    streamingStarted = false
    setState('thinking', 'thinking')
    thinkFluent()
    turnPhase = 'submitting'
    flushHeight()
  } catch (err) {
    returnToVoiceRetryState('error', 'error')
    console.warn('[card] voice submit failed:', err?.message)
  }
}

function beginExternalVoiceCapture() {
  inputTransitionGen += 1
  voiceStartGen += 1
  voiceInputSnapshot = null
  clearFollowupDraft()
  cancelFade()
  window.cardAPI?.cancelFade?.()
  clearDrip()
  hidePopoverNow()
  clearStagedImage()
  clearSubmittedQuestion()
  target = ''
  shown = ''
  prevVisibleText = ''
  charBirthTimes = []
  cardInput.value = ''
  cardInput.placeholder = '正在听…'
  cardInput.disabled = true
  card.classList.remove('submitted', 'warn', 'error', 'followup-entering', 'followup-input', 'followup-restoring')
  card.classList.add('listening')
  thinkOff()
  setState('listening')
  inputActive = false
  streamingStarted = false
  turnPhase = 'listening'
  clearAnswerContent()
  flushHeight()
  flushHeightFor(540)
}

function setExternalVoiceTranscribing() {
  card.classList.remove('listening', 'warn', 'error')
  cardInput.value = ''
  cardInput.placeholder = '识别中…'
  cardInput.disabled = true
  thinkOff()
  setState('transcribing', 'neutral')
  turnPhase = 'transcribing'
  flushHeight()
}

function acceptExternalVoiceText(text) {
  const trimmed = String(text || '').trim()
  if (!trimmed) {
    failExternalVoiceState('no speech', 'warn')
    return
  }
  inputTransitionGen += 1
  clearFollowupDraft()
  clearDrip()
  target = ''
  shown = ''
  prevVisibleText = ''
  charBirthTimes = []
  clearStagedImage()
  inFlightQ = trimmed
  cardInput.value = trimmed
  setSubmittedQuestion(trimmed)
  cardInput.placeholder = '问点什么…'
  cardInput.disabled = true
  card.classList.remove('listening', 'followup-entering', 'followup-input', 'followup-restoring', 'warn', 'error')
  card.classList.add('submitted')
  inputActive = true
  streamingStarted = false
  setState('thinking', 'thinking')
  thinkFluent()
  turnPhase = 'submitting'
  clearAnswerContent()
  flushHeight()
  flushHeightFor(700)
}

function failExternalVoiceState(label, variant = 'warn') {
  card.classList.remove('listening')
  cardInput.value = ''
  cardInput.placeholder = '问点什么…'
  cardInput.disabled = true
  thinkOff()
  setState(label, variant)
  inputActive = false
  streamingStarted = false
  turnPhase = variant === 'error' ? 'error' : 'idle'
  flushHeight()
  scheduleFade(1800)
}

function handleExternalVoiceState(payload = {}) {
  const phase = payload?.phase
  if (phase === 'listening') {
    beginExternalVoiceCapture()
  } else if (phase === 'transcribing') {
    setExternalVoiceTranscribing()
  } else if (phase === 'accepted') {
    acceptExternalVoiceText(payload?.text)
  } else if (phase === 'empty') {
    failExternalVoiceState('no speech', 'warn')
  } else if (phase === 'error') {
    failExternalVoiceState('error', 'error')
  }
}

function cancelEnterHoldTimer() {
  if (!enterHoldState) return false
  clearTimeout(enterHoldState.timer)
  enterHoldState = null
  return true
}

function cancelActiveVoiceCapture() {
  voiceStartGen += 1
  if (!voiceListening) return
  voiceListening = false
  card.classList.remove('listening')
  getVoiceCapture().stop().catch(err => {
    console.warn('[card] voice cancel failed:', err?.message)
  }).finally(() => {
    restoreSystemAudioForVoice()
  })
}

function scheduleEnterHold(e, shortAction = null) {
  if (e.repeat && enterHoldState) {
    e.preventDefault()
    return
  }
  if (enterHoldState) return
  e.preventDefault()
  const state = {
    holdFired: false,
    shortAction,
    startedAt: performance.now(),
    timer: null,
    startPromise: null
  }
  state.timer = setTimeout(() => {
    state.holdFired = true
    state.startPromise = beginEnterVoiceCapture()
  }, VOICE_HOLD_MS)
  enterHoldState = state
}

function handleEnterKeyUp(e) {
  if (e.key !== 'Enter' || !enterHoldState) return
  e.preventDefault()
  const state = enterHoldState
  enterHoldState = null
  clearTimeout(state.timer)
  const heldMs = performance.now() - state.startedAt
  if (!state.holdFired && heldMs >= VOICE_HOLD_MS) {
    state.holdFired = true
    state.startPromise = beginEnterVoiceCapture()
  }
  if (!state.holdFired) {
    state.shortAction?.()
    return
  }
  Promise.resolve(state.startPromise).then((started) => {
    if (started) return finishEnterVoiceCapture()
    return null
  }).catch(err => {
    returnToVoiceRetryState('error', 'error')
    console.warn('[card] enter voice lifecycle failed:', err?.message)
  })
}

cardInput.addEventListener('keydown', (e) => {
  if (keyboardEventIsComposing(e)) return
  if (e.key === 'Enter') {
    scheduleEnterHold(e, submitInputText)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    cancelEnterHoldTimer()
    cancelActiveVoiceCapture()
    window.cardAPI?.close?.()
  }
})

document.addEventListener('keydown', (e) => {
  if (keyboardEventIsComposing(e) || isEditableTarget(e.target)) return
  if (e.key === 'Enter') {
    const shortAction = canEnterFollowupInput()
      ? () => enterInputMode({ followup: true })
      : null
    scheduleEnterHold(e, shortAction)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    cancelEnterHoldTimer()
    cancelActiveVoiceCapture()
    window.cardAPI?.close?.()
  }
})
document.addEventListener('keyup', handleEnterKeyUp, true)
document.addEventListener('keydown', handleNativePasteShortcut, true)
document.addEventListener('paste', handleImagePaste)
card.addEventListener('dragenter', handleImageDragOver)
card.addEventListener('dragover', handleImageDragOver)
card.addEventListener('dragleave', handleImageDragLeave)
card.addEventListener('drop', handleImageDrop)

// ─── IPC: main → renderer ──────────────────────────────────
// In production (inherent mode), main process pushes content via these channels.
// scratch preview shell may or may not expose these — guard with optional chaining.
if (window.cardAPI?.onVoiceState) {
  window.cardAPI.onVoiceState(handleExternalVoiceState)
}
if (window.cardAPI?.onSiriOpen) {
  window.cardAPI.onSiriOpen(async (payload) => {
    inputTransitionGen += 1
    clearFollowupDraft()
    card.classList.remove('followup-entering', 'followup-input', 'followup-restoring')
    clearDrip()
    target = ''
    shown = ''
    prevVisibleText = ''
    charBirthTimes = []
    streamingStarted = false
    // If a local submit hasn't already moved the card into .submitted state,
    // do it now — siri:open from voice / cli paths still needs the answer
    // pane to expand. Local-submit path (inputActive=true) already added
    // .submitted; we just leave it.
    const q = String(payload?.q || '').trim()
    if (q && !inFlightQ) inFlightQ = q
    if (q) setSubmittedQuestion(q)
    if (!inputActive) {
      cardInput.value = q
      if (!q) clearSubmittedQuestion()
      cardInput.disabled = true
      card.classList.add('submitted')
    }
    // Hold thinking aura: response.start fires when the LLM connection opens,
    // often noticeably before the first token. Stay in thinking state; flip
    // to streaming when the first token actually arrives (onSiriAppend below).
    setState('thinking', 'thinking')
    thinkFluent()
    turnPhase = 'submitting'
    const streaming = !!payload?.streaming
    const content = payload?.content ?? ''
    if (streaming) {
      // Mode A: empty start, await siri:append events
      await setContent('')
    } else if (content) {
      // Mode B: full content arrives in one shot — no streaming gap
      target = content
      shown = content
      streamingStarted = true
      thinkOff()
      setState('streaming', 'thinking')
      turnPhase = 'streaming'
      await setContent(content)
    }
  })
}
if (window.cardAPI?.onSiriAppend) {
  window.cardAPI.onSiriAppend((payload) => {
    if (payload?.token == null) return
    if (!streamingStarted) {
      streamingStarted = true
      thinkOff()
      setState('streaming', 'thinking')
      turnPhase = 'streaming'
    }
    target += payload.token
    if (!dripTimer) startDrip()
  })
}
if (window.cardAPI?.onSiriDone) {
  window.cardAPI.onSiriDone((payload) => {
    // If drip hasn't caught up to target yet, defer fade until it would.
    const remainingChars = Math.max(0, target.length - shown.length)
    const dripRemaining = remainingChars * DRIP_MS
    thinkOff()
    setState('done', 'success')
    inputActive = false
    streamingStarted = false
    turnPhase = 'done'
    // Pair the in-flight Q with the final A and append a history chip.
    // External voice sets inFlightQ when ASR accepts the transcript; backend
    // response.start can also include q for non-local submit paths.
    if (target) {
      pushTurn(inFlightQ || '语音', target)
    }
    inFlightQ = null
    // Force a final reflow + window resize after the .submitted transitions
    // settle (max-height 0.5s, padding 0.32s). Without this, intermediate
    // flushHeight readings during streaming can leave the window stuck on a
    // tall bound (visible as empty space below the answer).
    setTimeout(() => { flushHeight() }, 600 + dripRemaining)
    scheduleFade((payload?.fadeMs ?? 5000) + dripRemaining)
  })
}
if (window.cardAPI?.onSiriReset) {
  window.cardAPI.onSiriReset(async () => {
    inputTransitionGen += 1
    clearFollowupDraft()
    clearDrip()
    target = ''
    shown = ''
    prevVisibleText = ''
    charBirthTimes = []
    setState('')
    inputActive = false
    streamingStarted = false
    turnPhase = 'idle'
    // Dismiss popover on turn boundary so the new turn isn't confused with history.
    hidePopoverNow()
    // resetInputState removes .submitted/.listening/.warn/.error so .row /
    // .answer drop back to idle dimensions. Without this the card kept its
    // submitted breadcrumb geometry while content was empty, producing a
    // window-vs-body size mismatch (oversized window, transparent area
    // showing through after switching apps and back).
    resetInputState()
    await setContent('')
    // setContent('') early-returns without flushHeight; force one so the
    // BrowserWindow shrinks back to row-only height.
    flushHeight()
  })
}
if (window.cardAPI?.onOpenInput) {
  window.cardAPI.onOpenInput(() => {
    enterInputMode()
  })
}

// ─── Demo mode ─────────────────────────────────────────────
const fixtures = {
  short: `# 现在 23°

bedroom · 客厅 22°`,

  long: `今天 3 件事：

- 14:00 — 健身房
- 16:00 — review jarvis
- 19:00 — 晚餐 with Sarah
- 21:00 — 写代码

要不要我提醒下一项？`,

  code: `好，写一个 Python 函数解析 JSON：

\`\`\`python
import json
from pathlib import Path

def parse_json(s: str) -> dict | None:
    """安全解析 JSON 字符串"""
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        print(f"parse error: {e}")
        return None
\`\`\`

要直接发到 editor 吗？`,

  mixed: `## 任务总结

完成了 **3 个文件** 的重构：

- \`card.html\` — 卡片 shell
- \`card.css\` — 视觉样式
- \`card.js\` — markdown 渲染 + 状态机

核心 diff：

\`\`\`javascript
// 之前: 同步 markdown-it
const html = md.render(text)

// 现在: 异步 + shiki streaming
const html = await md.renderAsync(text)
\`\`\`

[查看完整 diff](#)`
}

if (demo && fixtures[demo]) {
  setContent(fixtures[demo]).then(() => {
    // Don't auto-fade in demo mode — keep visible to inspect
    if (params.get('autofade') === '1') {
      scheduleFade(8000)
    }
  })
}
