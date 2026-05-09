// Jarvis inherent-mode card renderer.
// Loaded by scratch preview shell and (later) by desktop/main.js production shell.

import MarkdownItAsync from 'https://esm.sh/markdown-it-async@2'
import { codeToHtml } from 'https://esm.sh/shiki@3'

const root = document.getElementById('answer')
const card = document.getElementById('card')
const cardWrap = document.getElementById('card-wrap')
const cardInput = document.getElementById('card-input')
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
  if (!text) {
    if (followupDraftActive) restoreFollowupSnapshot()
    return
  }
  inputTransitionGen += 1
  clearFollowupDraft()
  inFlightQ = text
  cardInput.disabled = true
  card.classList.remove('followup-entering', 'followup-input', 'followup-restoring')
  card.classList.add('submitted')
  setState('thinking', 'thinking')
  thinkFluent()
  turnPhase = 'submitting'
  // Width-morph (Q2 = B): the input element itself shrinks into the breadcrumb
  // via .submitted CSS — no separate node, no DOM swap. The streaming answer
  // expands below in the existing #answer pane.
  Promise.resolve(window.cardAPI?.submit?.(text)).then(result => {
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

cardInput.addEventListener('keydown', (e) => {
  if (keyboardEventIsComposing(e)) return
  if (e.key === 'Enter') {
    e.preventDefault()
    submitInputText()
  } else if (e.key === 'Escape') {
    e.preventDefault()
    window.cardAPI?.close?.()
  }
})

document.addEventListener('keydown', (e) => {
  if (keyboardEventIsComposing(e) || isEditableTarget(e.target)) return
  if (e.key === 'Enter' && canEnterFollowupInput()) {
    e.preventDefault()
    enterInputMode({ followup: true })
  } else if (e.key === 'Escape') {
    e.preventDefault()
    window.cardAPI?.close?.()
  }
})

// ─── IPC: main → renderer ──────────────────────────────────
// In production (inherent mode), main process pushes content via these channels.
// scratch preview shell may or may not expose these — guard with optional chaining.
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
    if (!inputActive) {
      cardInput.value = ''
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
    // Voice-path Q isn't surfaced here yet (siri:open payload has no
    // transcript) — those turns get a placeholder until the bridge sends Q.
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
