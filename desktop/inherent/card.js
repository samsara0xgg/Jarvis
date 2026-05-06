// Jarvis inherent-mode card renderer.
// Loaded by scratch preview shell and (later) by desktop/main.js production shell.

import MarkdownItAsync from 'https://esm.sh/markdown-it-async@2'
import { codeToHtml } from 'https://esm.sh/shiki@3'

const root = document.getElementById('card-root')

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
  // Lift card-root max-height before measuring: scrollHeight respects max-height,
  // so without this long content reports the clamped value and the window never
  // grows to the main-process 800px cap.
  const prevMax = root.style.maxHeight
  root.style.maxHeight = 'none'
  void root.offsetHeight
  const height = Math.ceil(document.documentElement.scrollHeight)
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

window.jarvisCard = { setContent, cancelFade, scheduleFade }

// ─── IPC: main → renderer ──────────────────────────────────
// In production (inherent mode), main process pushes content via these channels.
// scratch preview shell may or may not expose these — guard with optional chaining.
if (window.cardAPI?.onSiriOpen) {
  window.cardAPI.onSiriOpen(async (payload) => {
    clearDrip()
    target = ''
    shown = ''
    prevVisibleText = ''
    charBirthTimes = []
    const streaming = !!payload?.streaming
    const content = payload?.content ?? ''
    if (streaming) {
      // Mode A: empty start, await siri:append events
      await setContent('')
    } else if (content) {
      // Mode B: full content arrives in one shot
      target = content
      shown = content
      await setContent(content)
    }
  })
}
if (window.cardAPI?.onSiriAppend) {
  window.cardAPI.onSiriAppend((payload) => {
    if (payload?.token == null) return
    target += payload.token
    if (!dripTimer) startDrip()
  })
}
if (window.cardAPI?.onSiriDone) {
  window.cardAPI.onSiriDone((payload) => {
    // If drip hasn't caught up to target yet, defer fade until it would.
    const remainingChars = Math.max(0, target.length - shown.length)
    const dripRemaining = remainingChars * DRIP_MS
    scheduleFade((payload?.fadeMs ?? 5000) + dripRemaining)
  })
}
if (window.cardAPI?.onSiriReset) {
  window.cardAPI.onSiriReset(async () => {
    clearDrip()
    target = ''
    shown = ''
    prevVisibleText = ''
    charBirthTimes = []
    await setContent('')
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
