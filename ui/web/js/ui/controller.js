// UI controller module
import { loadConfig, saveConfig, getServerUrl } from '../config/manager.js';
import { getAudioPlayer } from '../core/audio/player.js';
import { getAudioRecorder } from '../core/audio/recorder.js';
import { getApiClient } from '../core/api-client.js';
import { petOverlay } from './pet-overlay.js';

class UIController {
    constructor() {
        this.currentBackgroundIndex = localStorage.getItem('backgroundIndex') ? parseInt(localStorage.getItem('backgroundIndex')) : 0;
        this.backgroundImages = ['1.png', '2.png', '3.png'];

        this.init = this.init.bind(this);
        this.addChatMessage = this.addChatMessage.bind(this);
        this.switchBackground = this.switchBackground.bind(this);
        this.switchLive2DModel = this.switchLive2DModel.bind(this);
        this.showModal = this.showModal.bind(this);
        this.hideModal = this.hideModal.bind(this);
        this.switchTab = this.switchTab.bind(this);
    }

    init() {
        this.initEventListeners();
        loadConfig();
        this.updateConnectionUI(false);
        this.updateDialButton(false);

        const backgroundContainer = document.querySelector('.background-container');
        if (backgroundContainer) {
            backgroundContainer.style.backgroundImage = `url('./images/${this.backgroundImages[this.currentBackgroundIndex]}')`;
        }

        // Wire API client callbacks
        const apiClient = getApiClient();
        apiClient.onChatMessage = (text, isUser) => this.addChatMessage(text, isUser);
        apiClient.onSentence = async (sentence) => {
            if (sentence.audio_url) {
                const player = getAudioPlayer();
                const fullUrl = getServerUrl() + sentence.audio_url;
                if (sentence.index === 0) this.startLive2DTalking();
                await player.enqueue(fullUrl);
                if (player._queue.length === 0) this.stopLive2DTalking();
            }
            if (sentence.emotion && sentence.emotion !== 'neutral') {
                this.triggerLive2DEmotionAction(sentence.emotion);
            }
        };

        const audioRecorder = getAudioRecorder();
        audioRecorder.onRecordingStart = (seconds) => {
            this.updateRecordButtonState(true, seconds);
        };

        // Electron Pet Mode IPC integration — no-op when loaded in plain browser.
        // Two-phase mode switch ported from OLV:
        //   Phase 1 (pre-mode-changed): toggle body class so CSS hides chrome,
        //     then signal main we're ready for bounds/flags to flip.
        //   Phase 2 (mode-changed): window bounds have changed — resize PIXI
        //     canvas to the new window dimensions, then signal main to fade in.
        if (window.jarvis) {
            window.jarvis.onPreModeChanged((mode) => {
                document.body.classList.toggle('pet-mode', mode === 'pet');
                // Wait two rAF ticks so the CSS change has been laid out + painted
                // before telling main to flip bounds.
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        window.jarvis.sendModeReady();
                    });
                });
            });

            window.jarvis.onModeChanged((mode) => {
                const live2d = window.chatApp?.live2dManager;
                if (live2d && typeof live2d.resizeCanvas === 'function') {
                    live2d.resizeCanvas(window.innerWidth, window.innerHeight);
                }
                // Auto-hide the floating panel when leaving Pet mode —
                // ⌘Space is gated to Pet-only by main.js, so without this the
                // panel would be stuck open and unreachable.
                if (mode === 'window' && petOverlay && petOverlay.isOpen) {
                    petOverlay.hide();
                }
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        window.jarvis.sendModeRendered();
                    });
                });
            });

            // Hover-based click-through (replaces the old cursor-polling loop).
            // Renderer runs hit-tests locally and reports to main, which
            // aggregates across components and flips setIgnoreMouseEvents.
            document.addEventListener('mousemove', (e) => {
                const live2d = window.chatApp?.live2dManager;
                const hit = live2d && typeof live2d.isHitOnModel === 'function'
                    ? live2d.isHitOnModel(e.clientX, e.clientY)
                    : false;
                window.jarvis.updateHover('live2d', hit);
            });

            // Main relays `⌘Space model <name>` commands from the Pet overlay
            // through here — actual switch happens on the live2d manager.
            if (typeof window.jarvis.onSwitchModel === 'function') {
                window.jarvis.onSwitchModel((name) => {
                    this.switchLive2DModelByName(name);
                });
            }
        }

        // Pet overlay — Liquid Glass floating input/chat panel. Init is
        // idempotent and guarded by `if (window.jarvis)` for IPC-dependent
        // parts, so browser-only usage stays untouched.
        try {
            petOverlay.init();
        } catch (err) {
            // Non-fatal — controller must keep working.
            // eslint-disable-next-line no-console
            console.warn('[pet-overlay] init failed:', err);
        }
    }

    switchLive2DModelByName(name) {
        if (typeof name !== 'string' || !name) return;
        const app = window.chatApp;
        if (!app || !app.live2dManager) return;
        // Keep the select in sync so the "Apply" pattern still reflects truth.
        const modelSelect = document.getElementById('live2dModelSelect');
        if (modelSelect) modelSelect.value = name;
        app.live2dManager.switchModel(name).catch((err) => {
            // eslint-disable-next-line no-console
            console.warn('[controller] switchLive2DModelByName failed:', err);
        });
    }

    initEventListeners() {
        this._continuousMode = false;

        // Make control bar draggable
        this._initDraggableControlBar();

        const settingsBtn = document.getElementById('settingsBtn');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => this.showModal('settingsModal'));
        }

        const backgroundBtn = document.getElementById('backgroundBtn');
        if (backgroundBtn) {
            backgroundBtn.addEventListener('click', this.switchBackground);
        }

        const modelSelect = document.getElementById('live2dModelSelect');
        if (modelSelect) {
            modelSelect.addEventListener('change', () => this.switchLive2DModel());
        }

        // Dial button
        const dialBtn = document.getElementById('dialBtn');
        if (dialBtn) {
            dialBtn.addEventListener('click', () => {
                dialBtn.disabled = true;
                setTimeout(() => { dialBtn.disabled = false; }, 2000);

                const apiClient = getApiClient();
                if (apiClient.isConnected()) {
                    // Stop continuous mode on disconnect
                    if (this._continuousMode) this._toggleContinuousMode();
                    apiClient.disconnect();
                    this.updateDialButton(false);
                    this.updateConnectionUI(false);
                    this.addChatMessage('已断开连接~', false);
                } else {
                    const serverUrl = getServerUrl();
                    if (!serverUrl) {
                        this.showModal('settingsModal');
                        this.switchTab('device');
                        this.addChatMessage('请先填写服务器地址', false);
                        return;
                    }
                    this.handleConnect();
                }
            });
        }

        // Record button — click: normal record toggle; long-press 3s: continuous mode
        const recordBtn = document.getElementById('recordBtn');
        if (recordBtn) {
            let longPressTimer = null;
            let didLongPress = false;

            recordBtn.addEventListener('pointerdown', () => {
                didLongPress = false;
                longPressTimer = setTimeout(() => {
                    didLongPress = true;
                    longPressTimer = null;
                    this._toggleContinuousMode();
                }, 3000);
            });

            const cancelLongPress = () => {
                if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
            };
            recordBtn.addEventListener('pointerup', cancelLongPress);
            recordBtn.addEventListener('pointercancel', cancelLongPress);
            recordBtn.addEventListener('pointerleave', cancelLongPress);

            recordBtn.addEventListener('click', () => {
                if (didLongPress) return; // long-press already handled
                // In continuous mode, click toggles it off
                if (this._continuousMode) {
                    this._toggleContinuousMode();
                    return;
                }
                const audioRecorder = getAudioRecorder();
                if (audioRecorder.isRecording) {
                    audioRecorder.stop();
                    recordBtn.classList.remove('recording');
                    recordBtn.querySelector('.btn-text').textContent = '录音';
                } else {
                    recordBtn.classList.add('recording');
                    recordBtn.querySelector('.btn-text').textContent = '录音中';
                    setTimeout(() => audioRecorder.start(), 100);
                }
            });
        }

        // Chat input (composing guard for CJK IME)
        const messageInput = document.getElementById('messageInput');
        if (messageInput) {
            let composing = false;
            messageInput.addEventListener('compositionstart', () => { composing = true; });
            messageInput.addEventListener('compositionend', () => { composing = false; });
            messageInput.addEventListener('keydown', (e) => {
                if (e.key !== 'Enter' || composing || e.isComposing) return;
                const text = e.target.value.trim();
                if (!text) return;
                e.preventDefault();
                e.target.value = '';
                const apiClient = getApiClient();
                if (!apiClient.isConnected()) {
                    this.addChatMessage('请先点击拨号连接', false);
                    return;
                }
                this.addChatMessage(text, true);
                apiClient.sendTextMessage(text);
            });
        }

        // Close buttons
        document.querySelectorAll('.close-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const modal = e.target.closest('.modal');
                if (modal) {
                    if (modal.id === 'settingsModal') saveConfig();
                    this.hideModal(modal.id);
                }
            });
        });

        // Tab switch
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', (e) => this.switchTab(e.target.dataset.tab));
        });

        // Modal background click
        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal && modal.id !== 'settingsModal') {
                    this.hideModal(modal.id);
                }
            });
        });
    }

    updateConnectionUI(isConnected) {
        const connectionStatus = document.getElementById('connectionStatus');
        const statusDot = document.querySelector('.status-dot');
        if (connectionStatus) {
            connectionStatus.textContent = isConnected ? '已连接' : '离线';
            if (statusDot) {
                statusDot.className = `status-dot ${isConnected ? 'status-connected' : 'status-disconnected'}`;
            }
        }
    }

    updateDialButton(isConnected) {
        const dialBtn = document.getElementById('dialBtn');
        const recordBtn = document.getElementById('recordBtn');

        if (dialBtn) {
            if (isConnected) {
                dialBtn.classList.add('dial-active');
                dialBtn.querySelector('.btn-text').textContent = '挂断';
                dialBtn.querySelector('svg').innerHTML = `<path d="M12,9C10.4,9 9,10.4 9,12C9,13.6 10.4,15 12,15C13.6,15 15,13.6 15,12C15,10.4 13.6,9 12,9M12,17C9.2,17 7,14.8 7,12C7,9.2 9.2,7 12,7C14.8,7 17,9.2 17,12C17,14.8 14.8,17 12,17M12,4.5C7,4.5 2.7,7.6 1,12C2.7,16.4 7,19.5 12,19.5C17,19.5 21.3,16.4 23,12C21.3,7.6 17,4.5 12,4.5Z"/>`;
            } else {
                dialBtn.classList.remove('dial-active');
                dialBtn.querySelector('.btn-text').textContent = '拨号';
                dialBtn.querySelector('svg').innerHTML = `<path d="M6.62,10.79C8.06,13.62 10.38,15.94 13.21,17.38L15.41,15.18C15.69,14.9 16.08,14.82 16.43,14.93C17.55,15.3 18.75,15.5 20,15.5A1,1 0 0,1 21,16.5V20A1,1 0 0,1 20,21A17,17 0 0,1 3,4A1,1 0 0,1 4,3H7.5A1,1 0 0,1 8.5,4C8.5,5.25 8.7,6.45 9.07,7.57C9.18,7.92 9.1,8.31 8.82,8.59L6.62,10.79Z"/>`;
            }
        }

        if (recordBtn) {
            const micAvailable = window.microphoneAvailable !== false;
            if (isConnected && micAvailable) {
                recordBtn.disabled = false;
                recordBtn.title = '开始录音';
            } else {
                recordBtn.disabled = true;
                recordBtn.title = !micAvailable
                    ? (window.isHttpNonLocalhost ? '当前由于是http访问，无法录音' : '麦克风不可用')
                    : '请先连接服务器';
            }
            recordBtn.querySelector('.btn-text').textContent = '录音';
            recordBtn.classList.remove('recording');
        }
    }

    updateRecordButtonState(isRecording) {
        const recordBtn = document.getElementById('recordBtn');
        if (recordBtn) {
            recordBtn.querySelector('.btn-text').textContent = isRecording ? '录音中' : '录音';
            recordBtn.classList.toggle('recording', isRecording);
        }
    }

    updateMicrophoneAvailability(isAvailable, isHttpNonLocalhost) {
        const recordBtn = document.getElementById('recordBtn');
        if (!recordBtn) return;
        if (!isAvailable) {
            recordBtn.disabled = true;
            recordBtn.querySelector('.btn-text').textContent = '录音';
            recordBtn.title = isHttpNonLocalhost ? '当前由于是http访问，无法录音' : '麦克风不可用';
        } else {
            const apiClient = getApiClient();
            if (apiClient.isConnected()) {
                recordBtn.disabled = false;
                recordBtn.title = '开始录音';
            }
        }
    }

    addChatMessage(content, isUser = false) {
        const chatStream = document.getElementById('chatStream');
        if (!chatStream) return;
        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${isUser ? 'user' : 'ai'}`;
        messageDiv.innerHTML = `<div class="message-bubble">${content}</div>`;
        chatStream.appendChild(messageDiv);
        chatStream.scrollTop = chatStream.scrollHeight;

        // Fan-out for the Pet overlay (and any other listeners). Vanilla
        // CustomEvent; listeners guard by their own open/closed state.
        try {
            document.dispatchEvent(new CustomEvent('jarvis:message-added', {
                detail: { text: content, isUser },
            }));
        } catch { /* IE-era fallback unnecessary on Electron/modern browsers */ }
    }

    switchBackground() {
        this.currentBackgroundIndex = (this.currentBackgroundIndex + 1) % this.backgroundImages.length;
        const backgroundContainer = document.querySelector('.background-container');
        if (backgroundContainer) {
            backgroundContainer.style.backgroundImage = `url('./images/${this.backgroundImages[this.currentBackgroundIndex]}')`;
        }
        localStorage.setItem('backgroundIndex', this.currentBackgroundIndex);
    }

    switchLive2DModel() {
        const modelSelect = document.getElementById('live2dModelSelect');
        if (!modelSelect) return;
        const selectedModel = modelSelect.value;
        const app = window.chatApp;
        if (app && app.live2dManager) {
            app.live2dManager.switchModel(selectedModel)
                .then(success => this.addChatMessage(success ? `已切换到模型: ${selectedModel}` : '模型切换失败', false))
                .catch(() => this.addChatMessage('模型切换出错', false));
        }
    }

    showModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.style.display = 'flex';
    }

    hideModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) modal.style.display = 'none';
    }

    switchTab(tabName) {
        document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
        const activeBtn = document.querySelector(`[data-tab="${tabName}"]`);
        const activeContent = document.getElementById(`${tabName}Tab`);
        if (activeBtn && activeContent) {
            activeBtn.classList.add('active');
            activeContent.classList.add('active');
        }
    }

    async handleConnect() {
        const apiClient = getApiClient();
        apiClient.setServerUrl(getServerUrl());
        this.addChatMessage('正在连接小月...', false);

        const dialBtn = document.getElementById('dialBtn');
        if (dialBtn) {
            dialBtn.classList.add('dial-active');
            dialBtn.querySelector('.btn-text').textContent = '连接中...';
        }

        const chatIpt = document.getElementById('chatIpt');
        if (chatIpt) chatIpt.style.display = 'flex';

        const ok = await apiClient.connect();
        if (ok) {
            this.updateDialButton(true);
            this.updateConnectionUI(true);
            this.addChatMessage('已连接，随时待命~', false);
            this.hideModal('settingsModal');

            if (window.microphoneAvailable) {
                const recordBtn = document.getElementById('recordBtn');
                if (recordBtn) recordBtn.click();
            }
        } else {
            this.addChatMessage('连接失败，请检查服务器地址', false);
            this.updateDialButton(false);
            this.updateConnectionUI(false);
        }
    }

    // Live2D helpers
    startLive2DTalking() {
        const live2d = window.chatApp?.live2dManager;
        if (live2d && live2d.live2dModel) live2d.startTalking();
    }

    stopLive2DTalking() {
        const live2d = window.chatApp?.live2dManager;
        if (live2d) live2d.stopTalking();
    }

    triggerLive2DEmotionAction(emotion) {
        const live2d = window.chatApp?.live2dManager;
        if (live2d && typeof live2d.triggerEmotionAction === 'function') {
            live2d.triggerEmotionAction(emotion);
        }
    }

    _toggleContinuousMode() {
        const apiClient = getApiClient();
        if (!apiClient.isConnected()) {
            this.addChatMessage('请先连接服务器', false);
            return;
        }
        const recorder = getAudioRecorder();
        this._continuousMode = !this._continuousMode;

        const recordBtn = document.getElementById('recordBtn');

        if (this._continuousMode) {
            // Stop manual recording if active
            if (recorder.isRecording && !recorder.continuousMode) {
                recorder.stop();
            }
            recorder.onContinuousStatus = (status) => {
                if (!recordBtn) return;
                const text = recordBtn.querySelector('.btn-text');
                if (status === 'listening') {
                    recordBtn.classList.remove('recording');
                    recordBtn.classList.add('continuous-active');
                    text.textContent = '性奴';
                } else if (status === 'speaking') {
                    recordBtn.classList.add('recording');
                    text.textContent = '性奴';
                } else if (status === 'processing') {
                    recordBtn.classList.remove('recording');
                    text.textContent = '性奴';
                } else {
                    recordBtn.classList.remove('recording', 'continuous-active');
                    text.textContent = '录音';
                }
            };
            recorder.startContinuous();
            apiClient.setHiddenMode(true);
        } else {
            recorder.stopContinuous();
            apiClient.setHiddenMode(false);
            if (recordBtn) {
                recordBtn.classList.remove('recording', 'continuous-active');
                recordBtn.querySelector('.btn-text').textContent = '录音';
            }
        }
    }

    _initDraggableControlBar() {
        const bar = document.querySelector('.control-bar');
        if (!bar) return;
        let dragging = false, startX, startY, origX, origY;

        bar.addEventListener('pointerdown', (e) => {
            // Don't drag when clicking buttons
            if (e.target.closest('button, input, select')) return;
            dragging = true;
            bar.style.cursor = 'grabbing';
            const rect = bar.getBoundingClientRect();
            startX = e.clientX;
            startY = e.clientY;
            origX = rect.left + rect.width / 2;
            origY = rect.top;
            bar.setPointerCapture(e.pointerId);
        });

        bar.addEventListener('pointermove', (e) => {
            if (!dragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            bar.style.left = (origX + dx) + 'px';
            bar.style.top = (origY + dy) + 'px';
            bar.style.bottom = 'auto';
            bar.style.transform = 'translateX(-50%)';
        });

        const stopDrag = () => {
            dragging = false;
            bar.style.cursor = 'grab';
        };
        bar.addEventListener('pointerup', stopDrag);
        bar.addEventListener('pointercancel', stopDrag);
    }
}

export const uiController = new UIController();
export { UIController };
