// pet-overlay.js — Liquid Glass floating command/chat panel for Pet Mode.
//
// Raycast-inspired compact panel that hosts a text input + recent chat history.
// Toggled globally by ⌘Space (wired by desktop/main.js → IPC). Panel also
// handles a small whitelist of local commands that route to main via
// `window.jarvis.runLocalCommand(action, arg)`; unmatched text falls through
// to the existing `apiClient.sendTextMessage` path.
//
// Aesthetic: refined Liquid Glass, SF Pro fallback, coral accent used with
// restraint (10% user bubble bg + 30% focus ring + caret only).
// Signature motion: "paper settling" entry per message.
//
// Integration strategy (Option B): reuse existing #chatStream — on panel open
// we project its last ~20 `.chat-message` into panel rows; new messages
// dispatched via `jarvis:message-added` custom event are streamed in live.

import { getApiClient } from '../core/api-client.js';

// ── Command registry ──────────────────────────────────────────────────────────
// Strict full-string regex (NO prefix match). Unmatched text → cloud LLM.
// Commands with `action` go through main via IPC whitelist.
// Commands with `local(arg)` execute directly in renderer (for live2d zoom etc).

// Canonical case for model names (live2dManager.switchModel keys are case-sensitive).
const MODEL_CANON = {
    'hiyori_pro_zh': 'hiyori_pro_zh',
    'natori_pro_zh': 'natori_pro_zh',
    'mao': 'Mao',
    'haru': 'Haru',
    'rice': 'Rice',
    'murasame_yukata': 'Murasame_Yukata',
    'senko_normals': 'Senko_Normals',
};
const canonModel = (raw) => MODEL_CANON[String(raw || '').toLowerCase()] || raw;

const LOCAL_COMMANDS = [
    { regex: /^(退出|quit|exit)$/i, action: 'quit', feedback: '✓ 再见，小月下线了' },
    { regex: /^(藏起来|消失一下|hide)$/i, action: 'hide', feedback: '✓ 已躲进菜单栏' },
    { regex: /^(web|窗口|window|切到web)$/i, action: 'toWindow', feedback: '✓ 已切到窗口模式' },
    { regex: /^(pet|悬浮|桌面)$/i, action: 'toPet', feedback: '✓ 已切到悬浮模式' },
    {
        regex: /^(?:模型|model)\s+(hiyori_pro_zh|natori_pro_zh|Mao|Haru|Rice|Murasame_Yukata|Senko_Normals)$/i,
        action: 'switchModel',
        argFn: (m) => canonModel(m[1]),
        feedbackFn: (m) => `✓ 模型已切换到 ${canonModel(m[1])}`,
    },
    {
        regex: /^(放大|zoom\s*in)$/i,
        local: () => window.chatApp?.live2dManager?.zoomIn(),
        feedback: '✓ 已放大',
    },
    {
        regex: /^(缩小|zoom\s*out)$/i,
        local: () => window.chatApp?.live2dManager?.zoomOut(),
        feedback: '✓ 已缩小',
    },
    {
        regex: /^(重置大小|reset\s*size|zoom\s*reset)$/i,
        local: () => window.chatApp?.live2dManager?.zoomReset(),
        feedback: '✓ 大小已重置',
    },
    {
        regex: /^(居中|回来|center|recenter)$/i,
        local: () => window.chatApp?.live2dManager?.centerModel(),
        feedback: '✓ 已居中',
    },
    {
        regex: /^(重置|reset)$/i,
        local: () => window.chatApp?.live2dManager?.resetTransform(),
        feedback: '✓ 已全部重置',
    },
];

// ── Slash-command palette (Claude Code style) ────────────────────────────────
// Typing "/" opens an inline picker; each item either runs directly or — for
// commands that take a pick-list argument — transitions the palette into a
// second-level chooser (e.g. /model → model list).

const MODEL_OPTIONS = [
    { value: 'hiyori_pro_zh', label: 'Hiyori Pro' },
    { value: 'natori_pro_zh', label: 'Natori Pro' },
    { value: 'Mao', label: 'Mao' },
    { value: 'Haru', label: 'Haru' },
    { value: 'Rice', label: 'Rice' },
    { value: 'Murasame_Yukata', label: 'Murasame Yukata' },
    { value: 'Senko_Normals', label: 'Senko' },
];

const liveCall = (fn) => {
    const live = window.chatApp?.live2dManager;
    if (live) fn(live);
};

const SLASH_COMMANDS = [
    {
        name: 'model',
        desc: 'Switch Live2D model',
        picker: { placeholder: 'pick a model…', options: MODEL_OPTIONS },
        run: (arg) => ({ kind: 'ipc', action: 'switchModel', arg, feedback: `✓ 模型已切换到 ${arg}` }),
    },
    { name: 'window', desc: 'Switch to window mode', run: () => ({ kind: 'ipc', action: 'toWindow', feedback: '✓ 已切到窗口模式' }) },
    { name: 'pet', desc: 'Switch to pet (floating) mode', run: () => ({ kind: 'ipc', action: 'toPet', feedback: '✓ 已切到悬浮模式' }) },
    { name: 'hide', desc: 'Hide into menu bar', run: () => ({ kind: 'ipc', action: 'hide', feedback: '✓ 已躲进菜单栏' }) },
    { name: 'quit', desc: 'Quit Jarvis', run: () => ({ kind: 'ipc', action: 'quit', feedback: '✓ 再见，小月下线了' }) },
    { name: 'zoom-in', desc: 'Zoom Live2D in (+4%)', run: () => ({ kind: 'local', fn: () => liveCall(m => m.zoomIn()), feedback: '✓ 已放大' }) },
    { name: 'zoom-out', desc: 'Zoom Live2D out (-4%)', run: () => ({ kind: 'local', fn: () => liveCall(m => m.zoomOut()), feedback: '✓ 已缩小' }) },
    { name: 'reset-size', desc: 'Reset Live2D scale to default', run: () => ({ kind: 'local', fn: () => liveCall(m => m.zoomReset()), feedback: '✓ 大小已重置' }) },
    { name: 'center', desc: 'Recenter Live2D in window', run: () => ({ kind: 'local', fn: () => liveCall(m => m.centerModel()), feedback: '✓ 已居中' }) },
    { name: 'reset', desc: 'Reset Live2D scale + position', run: () => ({ kind: 'local', fn: () => liveCall(m => m.resetTransform()), feedback: '✓ 已全部重置' }) },
    {
        name: 'llm',
        desc: 'Switch cloud LLM preset (fast / deep / ...)',
        picker: {
            placeholder: 'pick an LLM preset…',
            prefetch: () => getApiClient().getLLMPresets().catch(() => {}),
            getOptions: () => {
                const data = getApiClient().getCachedLLMPresets();
                return (data.presets || []).map((p) => ({
                    value: p.name,
                    label: `${p.name === data.active ? '● ' : '  '}${p.name} — ${p.model || ''}`,
                }));
            },
        },
        run: (arg) => ({
            kind: 'local',
            fn: () => {
                getApiClient().switchLLM(arg)
                    .then((data) => {
                        window.petOverlay?.appendMessage?.({
                            text: `✓ LLM ${data.message || 'switched'}`,
                            role: 'system',
                        });
                    })
                    .catch((err) => {
                        window.petOverlay?.appendMessage?.({
                            text: `LLM 切换失败：${err.message || err}`,
                            role: 'system',
                        });
                    });
            },
            feedback: `→ 切换 LLM 到 ${arg}…`,
        }),
    },
    {
        name: 'emote',
        desc: 'Trigger a motion/expression on current model',
        picker: {
            placeholder: 'pick a motion…',
            getOptions: () => {
                const live = window.chatApp?.live2dManager;
                const groups = typeof live?.getMotionGroups === 'function' ? live.getMotionGroups() : [];
                return groups.map((g) => ({ value: g, label: g }));
            },
        },
        run: (arg) => ({
            kind: 'local',
            fn: () => { window.chatApp?.live2dManager?.live2dModel?.motion(arg); },
            feedback: `✓ 触发动作 ${arg}`,
        }),
    },
];

const PANEL_WIDTH = 485;
const PANEL_EDGE_MARGIN = 16;
const MODEL_GAP = 16;
const HISTORY_HYDRATE_MAX = 20;

class PetOverlay {
    constructor() {
        this.isOpen = false;
        this.initialized = false;
        this.rootEl = null;
        this.listEl = null;
        this.inputEl = null;
        this.messages = []; // {text, role: 'user'|'ai'|'system'}
        this._resizeObserver = null;
        this._resizeHandler = null;
        this._messageListener = null;
        // Drag-to-reposition state（session-only，Jarvis 重启复位）
        this._userPositioned = false;
        this._isDraggingPanel = false;
        this._panelDragStart = null;
        // Slash-command palette state
        this._slashEl = null;
        this._slashOpen = false;
        this._slashMode = 'command'; // 'command' | 'picker'
        this._slashActiveCmd = null;
        this._slashItems = [];
        this._slashIndex = 0;
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    init() {
        if (this.initialized) return;
        this.initialized = true;
        this._buildDom();
        this._wireEvents();

        // Hover bridge — when panel is open, make sure main treats it as
        // interactive (otherwise Pet mode's click-through swallows events).
        if (window.jarvis && typeof window.jarvis.updateHover === 'function') {
            this.rootEl.addEventListener('pointerenter', () => {
                window.jarvis.updateHover('pet-overlay', true);
            });
            this.rootEl.addEventListener('pointerleave', () => {
                window.jarvis.updateHover('pet-overlay', false);
            });
        }

        // Global shortcut arrives as IPC → renderer.
        if (window.jarvis && typeof window.jarvis.onToggleInputPanel === 'function') {
            window.jarvis.onToggleInputPanel(() => this.toggle());
        }

        // win.on('hide') 通知：Jarvis 被隐藏时强制收面板，下次 ⌘Space restore
        // 就是 closed → toggle open 的干净流程
        if (window.jarvis && typeof window.jarvis.onCloseInputPanel === 'function') {
            window.jarvis.onCloseInputPanel(() => {
                if (this.isOpen) this.hide();
            });
        }

        // Sync with chatStream — when controller dispatches a custom event,
        // mirror the message into the panel (only when open, to keep
        // browser-only mode untouched and avoid DOM churn).
        this._messageListener = (e) => {
            if (!this.isOpen) return;
            const { text, isUser } = e.detail || {};
            if (typeof text !== 'string') return;
            this.appendMessage({ text, role: isUser ? 'user' : 'ai' });
        };
        document.addEventListener('jarvis:message-added', this._messageListener);

        // 面板拖动：左键在面板空白区按住拖。点到 input/button 不触发（保证打字 + send 正常）。
        // 拖过一次 _userPositioned=true，后续 _positionPanel() 自动跳过 anchor 逻辑。
        this.rootEl.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            const tag = e.target.tagName;
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON') return;
            const rect = this.rootEl.getBoundingClientRect();
            this._isDraggingPanel = true;
            this._panelDragStart = {
                pointerX: e.clientX,
                pointerY: e.clientY,
                panelLeft: rect.left,
                panelTop: rect.top,
            };
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!this._isDraggingPanel || !this._panelDragStart) return;
            const newLeft = this._panelDragStart.panelLeft + (e.clientX - this._panelDragStart.pointerX);
            const newTop = this._panelDragStart.panelTop + (e.clientY - this._panelDragStart.pointerY);
            const rect = this.rootEl.getBoundingClientRect();
            const maxLeft = window.innerWidth - rect.width - PANEL_EDGE_MARGIN;
            const maxTop = window.innerHeight - rect.height - PANEL_EDGE_MARGIN;
            this.rootEl.style.left = `${Math.max(PANEL_EDGE_MARGIN, Math.min(newLeft, maxLeft))}px`;
            this.rootEl.style.top = `${Math.max(PANEL_EDGE_MARGIN, Math.min(newTop, maxTop))}px`;
        });

        document.addEventListener('mouseup', (e) => {
            if (e.button !== 0 || !this._isDraggingPanel) return;
            this._isDraggingPanel = false;
            this._panelDragStart = null;
            this._userPositioned = true;
        });
    }

    toggle() {
        if (this.isOpen) {
            this.hide();
        } else {
            this.show();
        }
    }

    show() {
        if (this.isOpen) return;
        this.isOpen = true;

        // Hydrate from existing #chatStream so first-open shows history.
        this._hydrateFromChatStream();

        // Flip on focusable BEFORE asking the input to focus, because macOS
        // Pet mode starts with setFocusable(false).
        if (window.jarvis && typeof window.jarvis.signalOverlayShown === 'function') {
            window.jarvis.signalOverlayShown();
        }

        this.rootEl.style.display = 'flex';
        // force reflow so the CSS animation actually runs
        void this.rootEl.offsetWidth;
        this.rootEl.classList.add('is-open');
        this.rootEl.classList.remove('is-closing');

        this._positionPanel();
        this._attachResizeHooks();

        // Give the window a moment to become focusable before focusing input.
        // Two rAFs is enough on macOS in practice.
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                if (this.inputEl) this.inputEl.focus();
                this._scrollToBottom();
            });
        });
    }

    hide() {
        if (!this.isOpen) return;
        this.isOpen = false;

        this.rootEl.classList.remove('is-open');
        this.rootEl.classList.add('is-closing');

        this._detachResizeHooks();

        // Match CSS disappear duration (180ms). Small safety margin.
        setTimeout(() => {
            if (!this.isOpen) {
                this.rootEl.style.display = 'none';
                this.rootEl.classList.remove('is-closing');
            }
        }, 200);

        // Drop focusable so clicks through to underlying desktop resume.
        if (window.jarvis && typeof window.jarvis.signalOverlayHidden === 'function') {
            window.jarvis.signalOverlayHidden();
        }
        if (window.jarvis && typeof window.jarvis.updateHover === 'function') {
            window.jarvis.updateHover('pet-overlay', false);
        }
    }

    // ── public append ─────────────────────────────────────────────────────────

    appendMessage({ text, role = 'ai' }) {
        if (!text) return;
        this.messages.push({ text, role });
        const bubble = this._renderBubble({ text, role });
        this.listEl.appendChild(bubble);
        this._scrollToBottom();
    }

    // ── DOM construction ──────────────────────────────────────────────────────

    _buildDom() {
        const root = document.createElement('div');
        root.className = 'pet-overlay';
        root.style.display = 'none';

        const list = document.createElement('div');
        list.className = 'pet-overlay__list';

        const inputRow = document.createElement('form');
        inputRow.className = 'pet-overlay__input-row';
        inputRow.setAttribute('autocomplete', 'off');
        inputRow.addEventListener('submit', (e) => {
            e.preventDefault();
            this._handleSubmit();
        });

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'pet-overlay__input';
        input.placeholder = '问我点什么…';
        input.setAttribute('autocomplete', 'off');
        input.setAttribute('autocapitalize', 'off');
        input.setAttribute('spellcheck', 'false');

        // Guard CJK IME — mirror existing messageInput behaviour.
        let composing = false;
        input.addEventListener('compositionstart', () => { composing = true; });
        input.addEventListener('compositionend', () => {
            composing = false;
            this._updateSlashMenu();
        });
        input.addEventListener('input', () => {
            if (!composing) this._updateSlashMenu();
        });
        input.addEventListener('keydown', (e) => {
            // Slash palette takes precedence over submit when open
            if (this._slashOpen && !composing && !e.isComposing) {
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this._moveSlashIndex(1);
                    return;
                }
                if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this._moveSlashIndex(-1);
                    return;
                }
                // Claude-Code-style back: ← returns from picker to command list.
                // Drops the trailing space + any typed arg, keeps `/<name>` so
                // the command is still the highlighted row.
                if (e.key === 'ArrowLeft' && this._slashMode === 'picker' && this._slashActiveCmd) {
                    e.preventDefault();
                    this.inputEl.value = '/' + this._slashActiveCmd.name;
                    this._slashIndex = 0;
                    this._updateSlashMenu();
                    return;
                }
                if (e.key === 'Escape') {
                    e.preventDefault();
                    this._closeSlashMenu();
                    return;
                }
                if (e.key === 'Tab') {
                    e.preventDefault();
                    this._acceptSlashItem({ execute: false });
                    return;
                }
                if (e.key === 'Enter') {
                    e.preventDefault();
                    this._acceptSlashItem({ execute: true });
                    return;
                }
            }
            if (e.key === 'Enter' && !composing && !e.isComposing) {
                e.preventDefault();
                this._handleSubmit();
            }
        });

        inputRow.appendChild(input);

        // Slash-command palette — absolutely positioned above input row
        const slash = document.createElement('div');
        slash.className = 'pet-overlay__slash';
        slash.style.display = 'none';

        const hintStrip = document.createElement('div');
        hintStrip.className = 'pet-overlay__hint';
        hintStrip.innerHTML = '<span class="pet-overlay__hint-left"><kbd>⌘Space</kbd> close</span>'
            + '<span class="pet-overlay__hint-right"><kbd>/</kbd> commands · <kbd>↵</kbd> send</span>';

        root.appendChild(list);
        root.appendChild(slash);
        root.appendChild(inputRow);
        root.appendChild(hintStrip);

        document.body.appendChild(root);

        this.rootEl = root;
        this.listEl = list;
        this.inputEl = input;
        this._slashEl = slash;
    }

    _wireEvents() {
        // Wheel-scroll the history body. (Default behaviour already works for
        // overflow; we attach non-passive listener to make sure Pet-mode
        // click-through doesn't swallow it.)
        this.listEl.addEventListener('wheel', (e) => {
            e.stopPropagation();
        }, { passive: true });
    }

    _renderBubble({ text, role }) {
        const wrap = document.createElement('div');
        wrap.className = `pet-overlay__msg pet-overlay__msg--${role}`;

        const bubble = document.createElement('div');
        bubble.className = 'pet-overlay__bubble';
        // Text content — use textContent for untrusted strings. The existing
        // chatStream inserts HTML, but panel stays conservative; LLM output
        // is rendered as plain text inside bubbles here.
        bubble.textContent = text;

        wrap.appendChild(bubble);
        return wrap;
    }

    // ── submit / command routing ──────────────────────────────────────────────

    _handleSubmit() {
        const raw = this.inputEl.value;
        const text = raw.trim();
        if (!text) return;
        this.inputEl.value = '';

        // Try local commands first.
        for (const cmd of LOCAL_COMMANDS) {
            const match = text.match(cmd.regex);
            if (!match) continue;

            const arg = cmd.argFn ? cmd.argFn(match) : (match[1] || null);
            const feedback = cmd.feedbackFn ? cmd.feedbackFn(match) : cmd.feedback;

            if (typeof cmd.local === 'function') {
                cmd.local(arg);
            } else if (window.jarvis && typeof window.jarvis.runLocalCommand === 'function') {
                window.jarvis.runLocalCommand(cmd.action, arg);
            } else {
                // Browser-only fallback — we still show feedback so user sees
                // the panel is alive, but no actual action happens.
                // (Intentional: allows :8006 in Chrome without crashing.)
            }

            // Insert a system bubble into the panel AND into #chatStream (so
            // history stays consistent across modes).
            this.appendMessage({ text: feedback, role: 'system' });
            this._pushSystemToChatStream(feedback);
            return;
        }

        // No command match — send to Jarvis LLM via existing pipeline.
        try {
            const api = getApiClient();
            if (!api.isConnected()) {
                this.appendMessage({ text: '请先点击拨号连接', role: 'system' });
                return;
            }
            // Write user bubble to chatStream via controller — the
            // `jarvis:message-added` event then mirrors it back into this
            // panel, so we DON'T appendMessage directly here to avoid dupes.
            const controller = window.chatApp?.uiController;
            if (controller && typeof controller.addChatMessage === 'function') {
                controller.addChatMessage(text, true);
            } else {
                // Fallback if controller missing — show locally only.
                this.appendMessage({ text, role: 'user' });
            }
            api.sendTextMessage(text);
        } catch (err) {
            this.appendMessage({ text: `发送失败：${err.message || err}`, role: 'system' });
        }
    }

    _pushSystemToChatStream(text) {
        const chatStream = document.getElementById('chatStream');
        if (!chatStream) return;
        const div = document.createElement('div');
        div.className = 'chat-message system';
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        bubble.textContent = text;
        div.appendChild(bubble);
        chatStream.appendChild(div);
        chatStream.scrollTop = chatStream.scrollHeight;
    }

    // ── slash-command palette ────────────────────────────────────────────────

    _updateSlashMenu() {
        if (!this.inputEl || !this._slashEl) return;
        const raw = this.inputEl.value;
        if (!raw.startsWith('/')) {
            this._closeSlashMenu();
            return;
        }
        const body = raw.slice(1);
        const spaceIdx = body.indexOf(' ');

        if (spaceIdx === -1) {
            // Command mode — filter full command list by prefix
            this._slashMode = 'command';
            this._slashActiveCmd = null;
            const q = body.toLowerCase();
            const matches = SLASH_COMMANDS.filter((c) =>
                c.name.toLowerCase().includes(q) || (c.desc || '').toLowerCase().includes(q),
            );
            this._renderSlashItems(matches.map((c) => ({
                name: '/' + c.name,
                desc: c.desc,
                kind: 'command',
                cmd: c,
            })));
            return;
        }

        // Arg mode — only valid for commands with a picker
        const cmdName = body.slice(0, spaceIdx).toLowerCase();
        const argQuery = body.slice(spaceIdx + 1).toLowerCase();
        const cmd = SLASH_COMMANDS.find((c) => c.name.toLowerCase() === cmdName);
        if (!cmd || !cmd.picker) {
            this._closeSlashMenu();
            return;
        }
        this._slashMode = 'picker';
        this._slashActiveCmd = cmd;

        // Fire prefetch once per picker entry; re-render when it settles so
        // async-populated caches (e.g. /llm presets) show up without requiring
        // the user to keystroke again.
        if (this._slashLastPrefetchCmd !== cmd && typeof cmd.picker.prefetch === 'function') {
            this._slashLastPrefetchCmd = cmd;
            Promise.resolve(cmd.picker.prefetch()).finally(() => {
                if (this._slashOpen && this._slashActiveCmd === cmd) {
                    this._updateSlashMenu();
                }
            });
        }

        const pickerOpts = typeof cmd.picker.getOptions === 'function'
            ? cmd.picker.getOptions()
            : (cmd.picker.options || []);
        const opts = pickerOpts.filter((o) =>
            o.value.toLowerCase().includes(argQuery)
            || o.label.toLowerCase().includes(argQuery),
        );
        this._renderSlashItems(opts.map((o) => ({
            name: o.value,
            desc: o.label,
            kind: 'picker',
            option: o,
        })));
    }

    _renderSlashItems(items) {
        if (!this._slashEl) return;
        this._slashItems = items;
        if (items.length === 0) {
            this._closeSlashMenu();
            return;
        }
        this._slashOpen = true;
        if (this._slashIndex >= items.length) this._slashIndex = 0;
        if (this._slashIndex < 0) this._slashIndex = 0;

        this._slashEl.innerHTML = '';
        items.forEach((it, i) => {
            const row = document.createElement('div');
            row.className = 'pet-overlay__slash-item' + (i === this._slashIndex ? ' is-active' : '');
            const name = document.createElement('span');
            name.className = 'pet-overlay__slash-name';
            name.textContent = it.name;
            const desc = document.createElement('span');
            desc.className = 'pet-overlay__slash-desc';
            desc.textContent = it.desc || '';
            row.appendChild(name);
            row.appendChild(desc);
            row.addEventListener('mousedown', (e) => {
                e.preventDefault(); // keep focus on input
                this._slashIndex = i;
                this._acceptSlashItem({ execute: true });
            });
            row.addEventListener('mouseenter', () => {
                this._slashIndex = i;
                this._refreshSlashActive();
            });
            this._slashEl.appendChild(row);
        });
        this._slashEl.style.display = 'block';
    }

    _refreshSlashActive() {
        if (!this._slashEl) return;
        const rows = this._slashEl.querySelectorAll('.pet-overlay__slash-item');
        rows.forEach((r, i) => r.classList.toggle('is-active', i === this._slashIndex));
        const active = rows[this._slashIndex];
        if (active && typeof active.scrollIntoView === 'function') {
            active.scrollIntoView({ block: 'nearest' });
        }
    }

    _moveSlashIndex(delta) {
        if (!this._slashOpen || this._slashItems.length === 0) return;
        const n = this._slashItems.length;
        this._slashIndex = (this._slashIndex + delta + n) % n;
        this._refreshSlashActive();
    }

    _acceptSlashItem({ execute }) {
        if (!this._slashOpen || !this._slashItems[this._slashIndex]) return;
        const it = this._slashItems[this._slashIndex];

        if (it.kind === 'command') {
            const cmd = it.cmd;
            if (cmd.picker) {
                // Transition to picker mode even on Enter (matches Claude Code flow)
                this.inputEl.value = '/' + cmd.name + ' ';
                this._slashIndex = 0;
                this._updateSlashMenu();
                return;
            }
            if (execute) {
                this._runSlashCommand(cmd, null);
                this.inputEl.value = '';
                this._closeSlashMenu();
            } else {
                // Tab — complete the name, stay in menu
                this.inputEl.value = '/' + cmd.name;
                this._updateSlashMenu();
            }
            return;
        }

        if (it.kind === 'picker' && this._slashActiveCmd) {
            if (execute) {
                this._runSlashCommand(this._slashActiveCmd, it.option.value);
                this.inputEl.value = '';
                this._closeSlashMenu();
            } else {
                this.inputEl.value = '/' + this._slashActiveCmd.name + ' ' + it.option.value;
                this._updateSlashMenu();
            }
        }
    }

    _runSlashCommand(cmd, arg) {
        let result;
        try {
            result = cmd.run(arg);
        } catch (err) {
            this.appendMessage({ text: `命令执行失败：${err.message || err}`, role: 'system' });
            return;
        }
        if (!result) return;
        if (result.kind === 'ipc') {
            if (window.jarvis?.runLocalCommand) {
                window.jarvis.runLocalCommand(result.action, result.arg ?? null);
            }
        } else if (result.kind === 'local' && typeof result.fn === 'function') {
            result.fn();
        }
        if (result.feedback) {
            this.appendMessage({ text: result.feedback, role: 'system' });
            this._pushSystemToChatStream(result.feedback);
        }
    }

    _closeSlashMenu() {
        this._slashOpen = false;
        this._slashMode = 'command';
        this._slashActiveCmd = null;
        this._slashLastPrefetchCmd = null;
        this._slashItems = [];
        this._slashIndex = 0;
        if (this._slashEl) this._slashEl.style.display = 'none';
    }

    // ── hydration / sync ──────────────────────────────────────────────────────

    _hydrateFromChatStream() {
        // Rebuild list from the trailing N messages of the canonical stream,
        // so the panel reflects history even across multiple open/close cycles.
        this.listEl.innerHTML = '';
        this.messages = [];
        const chatStream = document.getElementById('chatStream');
        if (!chatStream) return;

        const all = chatStream.querySelectorAll('.chat-message');
        const start = Math.max(0, all.length - HISTORY_HYDRATE_MAX);
        for (let i = start; i < all.length; i += 1) {
            const el = all[i];
            const text = el.textContent?.trim() || '';
            if (!text) continue;
            let role = 'ai';
            if (el.classList.contains('user')) role = 'user';
            else if (el.classList.contains('system')) role = 'system';
            this.appendMessage({ text, role });
        }
    }

    _scrollToBottom() {
        // rAF so layout has settled before we measure.
        requestAnimationFrame(() => {
            if (this.listEl) this.listEl.scrollTop = this.listEl.scrollHeight;
        });
    }

    // ── positioning ───────────────────────────────────────────────────────────

    _positionPanel() {
        if (!this.rootEl) return;
        if (this._userPositioned) return;  // 用户拖过，尊重他的位置，不再 anchor 模型

        const rect = this.rootEl.getBoundingClientRect();
        const panelW = rect.width || PANEL_WIDTH;
        const panelH = rect.height || 240;

        // Ask the Live2D model for its DOM bounds. Pet mode window spans the
        // virtual desktop across ALL displays, so these coordinates are in
        // the same (document) space as our absolutely-positioned overlay —
        // no extra screen-to-document translation needed.
        const live2d = window.chatApp?.live2dManager;
        const model = live2d?.live2dModel;

        let anchorLeft;
        let anchorTop;

        if (model && typeof model.getBounds === 'function') {
            const b = model.getBounds(); // PIXI bounds — {x, y, width, height}
            const modelCenterX = b.x + b.width / 2;
            const modelBottom = b.y + b.height;

            // Default: 16px below model bottom, horizontally centered.
            anchorLeft = modelCenterX - panelW / 2;
            anchorTop = modelBottom + MODEL_GAP;

            // Fallback: if overflow bottom, anchor above model.
            if (anchorTop + panelH > window.innerHeight - PANEL_EDGE_MARGIN) {
                anchorTop = b.y - MODEL_GAP - panelH;
            }
        } else {
            // No model — center near bottom of viewport.
            anchorLeft = (window.innerWidth - panelW) / 2;
            anchorTop = window.innerHeight - panelH - 64;
        }

        // Edge clamp horizontally — keep inside viewport with margin.
        anchorLeft = Math.max(
            PANEL_EDGE_MARGIN,
            Math.min(anchorLeft, window.innerWidth - panelW - PANEL_EDGE_MARGIN),
        );
        // Vertical clamp as final safety net.
        anchorTop = Math.max(
            PANEL_EDGE_MARGIN,
            Math.min(anchorTop, window.innerHeight - panelH - PANEL_EDGE_MARGIN),
        );

        this.rootEl.style.left = `${Math.round(anchorLeft)}px`;
        this.rootEl.style.top = `${Math.round(anchorTop)}px`;
    }

    _attachResizeHooks() {
        if (this._resizeHandler) return;
        this._resizeHandler = () => this._positionPanel();
        window.addEventListener('resize', this._resizeHandler);

        const live2d = window.chatApp?.live2dManager;
        const canvas = document.getElementById('live2d-stage');
        if (canvas && 'ResizeObserver' in window) {
            this._resizeObserver = new ResizeObserver(() => this._positionPanel());
            this._resizeObserver.observe(canvas);
        }
        // Also re-position when the model potentially switches — listen on a
        // custom event if the live2d manager ever emits one, otherwise rely
        // on resize observer + manual reposition on next show().
        if (live2d && typeof live2d.on === 'function') {
            // best-effort — no-op if the manager doesn't support it
            try { live2d.on('model-switched', this._resizeHandler); } catch { /* ignore */ }
        }
    }

    _detachResizeHooks() {
        if (this._resizeHandler) {
            window.removeEventListener('resize', this._resizeHandler);
            this._resizeHandler = null;
        }
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
            this._resizeObserver = null;
        }
    }
}

export const petOverlay = new PetOverlay();
export { PetOverlay };
export default petOverlay;

// Debug hook: let non-module scripts (live2d.js) push messages into the panel
if (typeof window !== 'undefined') {
    window.petOverlay = petOverlay;
}
