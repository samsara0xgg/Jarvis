/**
 * Live2D 管理器
 * 负责 Live2D 模型的初始化、嘴部动画控制等功能
 */
class Live2DManager {
    constructor() {
        this.live2dApp = null;
        this.live2dModel = null;
        this.isTalking = false;
        this.mouthAnimationId = null;
        this.mouthParam = 'ParamMouthOpenY';
        this.audioContext = null;
        this.analyser = null;
        this.dataArray = null;
        this.lastEmotionActionTime = null;
        this.currentModelName = null;

        // 模型特定配置
        this.modelConfig = {
            'hiyori_pro_zh': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 },
                motionMap: {
                    'FlickUp': 'FlickUp',
                    'FlickDown': 'FlickDown',
                    'Tap': 'Tap',
                    'Tap@Body': 'Tap@Body',
                    'Flick': 'Flick',
                    'Flick@Body': 'Flick@Body'
                }
            },
            'natori_pro_zh': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.1, high: 0.4 },
                mouthFormParam: 'ParamMouthForm',
                mouthFormAmplitude: 1.0,
                mouthForm2Param: 'ParamMouthForm2',
                mouthForm2Amplitude: 0.8,
                motionMap: {
                    'FlickUp': 'FlickUp',
                    'FlickDown': 'Flick@Body',
                    'Tap': 'Tap',
                    'Tap@Body': 'Tap@Head',
                    'Flick': 'Tap',
                    'Flick@Body': 'Flick@Body'
                }
            },
            'Mao': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 }
            },
            'Haru': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 }
            },
            'Rice': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 }
            },
            'Murasame_Yukata': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 }
            },
            'Senko_Normals': {
                mouthParam: 'ParamMouthOpenY',
                mouthAmplitude: 1.0,
                mouthThresholds: { low: 0.3, high: 0.7 }
            }
        };

        // 情绪到动作的映射
        this.emotionToActionMap = {
            'happy': 'FlickUp',      // 开心-向上轻扫动作
            'laughing': 'FlickUp',   // 大笑-向上轻扫动作
            'funny': 'FlickUp',      // 搞笑-向上轻扫动作
            'sad': 'FlickDown',      // 伤心-向下轻扫动作
            'crying': 'FlickDown',   // 哭泣-向下轻扫动作
            'angry': 'Tap@Body',     // 生气-身体点击动作
            'surprised': 'Tap',      // 惊讶-点击动作
            'neutral': 'Flick',      // 平常-轻扫动作
            'default': 'Flick@Body'  // 默认-身体轻扫动作
        };

        // 单/双击判定配置与状态
        this._lastClickTime = 0;
        this._lastClickPos = { x: 0, y: 0 };
        this._singleClickTimer = null;
        this._doubleClickMs = 280; // 双击时间阈值(ms)
        this._doubleClickDist = 16; // 双击允许的最大位移(px)
        // 滑动判定
        this._pointerDown = false;
        this._downPos = { x: 0, y: 0 };
        this._downTime = 0;
        this._downArea = 'Body';
        this._movedBeyondClick = false;
        this._swipeMinDist = 24; // 触发滑动的最小距离

        // Skirt easter egg: 连续 5 次点裙子触发彩蛋（超时自动重置）
        this._skirtTapCount = 0;
        this._skirtResetTimer = null;
        this._skirtWindowMs = 3000;
        this._skirtCombo = 5;

        // Pet mode 拖动 + 缩放（中键拖动零延迟；⌘+/⌘-/⌘0 像浏览器一样缩放）
        this._isDragging = false;
        this._dragStartPointer = { x: 0, y: 0 };
        this._dragStartModelPos = { x: 0, y: 0 };
        this._minScale = 0.1;
        this._maxScale = 5.0;
        this._keyScaleStep = 0.04;  // ⌘+/⌘- 每按一次 4%（乘法，视觉变化恒定）
        this._defaultScale = 0.1626; // ⌘0 重置目标（再 +20%）
        this._saveTransformTimer = null;
    }

    /**
     * 读取上次保存的 scale + 视觉中心相对比例（fx, fy ∈ [0, 1]）。
     * 存 fraction 而非绝对像素的原因：下次启动可能换屏 / 换分辨率，
     * 同样的比例位置在不同窗口上视觉一致，绝对坐标会错位。
     * @returns {{scale: number|null, fx: number|null, fy: number|null}}
     */
    _loadTransform() {
        const scale = parseFloat(localStorage.getItem('live2dScale'));
        const fx = parseFloat(localStorage.getItem('live2dFX'));
        const fy = parseFloat(localStorage.getItem('live2dFY'));
        return {
            scale: Number.isFinite(scale) ? scale : null,
            fx: Number.isFinite(fx) ? fx : null,
            fy: Number.isFinite(fy) ? fy : null,
        };
    }

    /**
     * Debounced 250ms 持久化 scale + 视觉中心比例到 localStorage。
     * 用 getBounds() 取可见人物的中心，避免 Live2D 透明 padding 干扰。
     */
    _saveTransform() {
        if (!this.live2dModel) return;
        if (this._saveTransformTimer) clearTimeout(this._saveTransformTimer);
        this._saveTransformTimer = setTimeout(() => {
            if (!this.live2dModel) return;
            const bnd = this.live2dModel.getBounds();
            let cx, cy;
            if (bnd && bnd.width > 0 && bnd.height > 0) {
                cx = bnd.x + bnd.width * 0.5;
                cy = bnd.y + bnd.height * 0.5;
            } else {
                cx = this.live2dModel.x + this.live2dModel.width * 0.5;
                cy = this.live2dModel.y + this.live2dModel.height * 0.5;
            }
            localStorage.setItem('live2dScale', String(this.live2dModel.scale.x));
            localStorage.setItem('live2dFX', String(cx / window.innerWidth));
            localStorage.setItem('live2dFY', String(cy / window.innerHeight));
            // 清掉老格式（绝对像素），防止下次误用
            localStorage.removeItem('live2dX');
            localStorage.removeItem('live2dY');
        }, 250);
    }

    /**
     * 把保存的 transform 应用到 this.live2dModel。无保存值则 fallback 到
     * 传入的默认 scale + 横向居中 + y=20（老行为）。加载后会 clamp 到
     * 当前窗口可视区，防止上次在跨屏 pet mode 存的坐标在窗口模式下不可见。
     * @param {number} defaultScale
     */
    _applyTransform(defaultScale) {
        if (!this.live2dModel) return;
        const saved = this._loadTransform();
        const scale = saved.scale !== null ? saved.scale : defaultScale;
        this.live2dModel.scale.set(scale);
        if (saved.fx !== null && saved.fy !== null) {
            // 按当前窗口尺寸还原视觉中心：fx*W, fy*H
            const bnd = this.live2dModel.getBounds();
            let curCX, curCY;
            if (bnd && bnd.width > 0 && bnd.height > 0) {
                curCX = bnd.x + bnd.width * 0.5;
                curCY = bnd.y + bnd.height * 0.5;
            } else {
                curCX = this.live2dModel.x + this.live2dModel.width * 0.5;
                curCY = this.live2dModel.y + this.live2dModel.height * 0.5;
            }
            this.live2dModel.x += saved.fx * window.innerWidth - curCX;
            this.live2dModel.y += saved.fy * window.innerHeight - curCY;
            this._clampModelInView();
        } else {
            this._positionTopRight(-115, -45);
        }
    }

    /**
     * 把模型摆到当前窗口右上角，让**可见人物**距顶/右各 margin 像素。
     * 关键：用 getBounds() 排除 Live2D 纹理里大量的透明 padding ——
     * model.width 包含透明区，直接用会让人物偏下偏左（看起来不像右上角）。
     */
    _positionTopRight(rightMargin = 40, topMargin = rightMargin) {
        if (!this.live2dModel) return;
        const winW = window.innerWidth;
        const bounds = this.live2dModel.getBounds();
        if (bounds && bounds.width > 0 && bounds.height > 0) {
            const dx = (winW - rightMargin) - (bounds.x + bounds.width);
            const dy = topMargin - bounds.y;
            this.live2dModel.x += dx;
            this.live2dModel.y += dy;
        } else {
            // Bounds 还没算好 —— fallback 到纹理 box
            this.live2dModel.x = winW - this.live2dModel.width - rightMargin;
            this.live2dModel.y = topMargin;
        }
    }

    /**
     * 跨屏时保持模型**可见中心**在窗口里的相对比例位置不变（原来在右上角
     * 85%/15%，跳屏后仍在新窗口的 85%/15%）。用 getBounds() 而非纹理 box
     * 算中心，避免透明 padding 影响。
     * @param {{x: number, y: number, width: number, height: number}} oldBounds
     * @param {{x: number, y: number, width: number, height: number}} newBounds
     */
    applyProportionalPosition(oldBounds, newBounds) {
        if (!this.live2dModel || !oldBounds || !newBounds) return;
        const bnd = this.live2dModel.getBounds();
        let curCX, curCY;
        if (bnd && bnd.width > 0 && bnd.height > 0) {
            curCX = bnd.x + bnd.width * 0.5;
            curCY = bnd.y + bnd.height * 0.5;
        } else {
            curCX = this.live2dModel.x + this.live2dModel.width * 0.5;
            curCY = this.live2dModel.y + this.live2dModel.height * 0.5;
        }
        const fx = curCX / oldBounds.width;
        const fy = curCY / oldBounds.height;
        const newCX = fx * newBounds.width;
        const newCY = fy * newBounds.height;
        this.live2dModel.x += newCX - curCX;
        this.live2dModel.y += newCY - curCY;
        this._clampModelInView();
        this._saveTransform();
    }

    /**
     * Clamp this.live2dModel.x/y 到当前窗口可视区，保留 60px 最小露头。
     * 任何时刻调用都安全。
     */
    _clampModelInView() {
        if (!this.live2dModel) return;
        const margin = 60;
        const mw = this.live2dModel.width;
        const mh = this.live2dModel.height;
        const winW = window.innerWidth;
        const winH = window.innerHeight;
        this.live2dModel.x = Math.max(-mw + margin, Math.min(winW - margin, this.live2dModel.x));
        this.live2dModel.y = Math.max(-mh + margin, Math.min(winH - margin, this.live2dModel.y));
    }

    /**
     * 初始化 Live2D
     */
    async initializeLive2D() {
        try {
            const canvas = document.getElementById('live2d-stage');

            // 供内部使用
            window.PIXI = PIXI;

            this.live2dApp = new PIXI.Application({
                view: canvas,
                height: window.innerHeight,
                width: window.innerWidth,
                resolution: window.devicePixelRatio,
                autoDensity: true,
                antialias: true,
                backgroundAlpha: 0,
            });

            // 加载 Live2D 模型 - 动态检测当前目录，适配不同环境
            // 获取当前HTML文件所在的目录路径
            const currentPath = window.location.pathname;
            const lastSlashIndex = currentPath.lastIndexOf('/');
            const basePath = currentPath.substring(0, lastSlashIndex + 1);

            // 从 localStorage 读取上次选择的模型，如果没有则使用默认
            const savedModelName = localStorage.getItem('live2dModel') || 'hiyori_pro_zh';
            const modelFileMap = {
                'hiyori_pro_zh': { file: 'hiyori_pro_t11.model3.json', subdir: 'runtime/' },
                'natori_pro_zh': { file: 'natori_pro_t06.model3.json', subdir: 'runtime/' },
                'Mao': { file: 'Mao.model3.json', subdir: '' },
                'Haru': { file: 'Haru.model3.json', subdir: '' },
                'Rice': { file: 'Rice.model3.json', subdir: '' },
                'Murasame_Yukata': { file: 'Murasame_Yukata.model3.json', subdir: '' },
                'Senko_Normals': { file: 'senko.model3.json', subdir: '' }
            };
            const modelInfo = modelFileMap[savedModelName] || modelFileMap['hiyori_pro_zh'];
            const modelPath = basePath + 'resources/' + savedModelName + '/' + modelInfo.subdir + modelInfo.file;

            this.live2dModel = await PIXI.live2d.Live2DModel.from(modelPath);
            this.live2dApp.stage.addChild(this.live2dModel);

            // 保存当前模型名称
            this.currentModelName = savedModelName;

            // 更新下拉框显示
            const modelSelect = document.getElementById('live2dModelSelect');
            if (modelSelect) {
                modelSelect.value = savedModelName;
            }

            // 设置模型特定的嘴部参数名
            if (this.modelConfig[savedModelName]) {
                this.mouthParam = this.modelConfig[savedModelName].mouthParam || 'ParamMouthOpenY';
            }

            // 设置模型属性（保存过则恢复，否则默认 scale=0.28 + 居中）
            this._applyTransform(0.28);

            // 交互监听（单/双击/滑动/Face/Skirt/debug log）统一走 setupModelInteractions。
            // 切换模型时也会调用它，保证首次加载和切换模型用同一套逻辑。
            this.setupModelInteractions();

            // 添加窗口大小变化监听器
            window.addEventListener('resize', () => {
                if (this.live2dApp && this.live2dApp.renderer) {
                    this.live2dApp.renderer.resize(window.innerWidth, window.innerHeight);
                }
                if (!this.live2dModel) return;
                if (localStorage.getItem('live2dFX') === null) {
                    this.live2dModel.x = (window.innerWidth - this.live2dModel.width) * 0.5;
                    this.live2dModel.y = -50;
                } else {
                    // 尊重用户拖动结果，但 clamp 到新窗口大小
                    this._clampModelInView();
                }
            });

            // Drag：⌘（macOS）/Ctrl（Win/Linux）+ 左键按住拖动，零延迟零阈值。
            // document 级监听，换模型后无需重绑。PIXI 侧的 pointerdown
            // handlers 会检 metaKey/ctrlKey 并早退，避免触发 click/swipe。
            document.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                if (!(e.metaKey || e.ctrlKey)) return;
                if (!this.live2dModel) return;
                if (!this.isHitOnModel(e.clientX, e.clientY)) return;
                e.preventDefault();
                this._isDragging = true;
                this._dragStartPointer = { x: e.clientX, y: e.clientY };
                this._dragStartModelPos = { x: this.live2dModel.x, y: this.live2dModel.y };
            });

            document.addEventListener('mousemove', (e) => {
                if (!this._isDragging || !this.live2dModel) return;
                const newX = this._dragStartModelPos.x + (e.clientX - this._dragStartPointer.x);
                const newY = this._dragStartModelPos.y + (e.clientY - this._dragStartPointer.y);
                // Clamp：至少保留 60px 模型在 canvas 内，否则跨屏拖过头会
                // 直接拖出窗口边界（macOS 透明窗口跨屏渲染不总是可靠）
                const margin = 60;
                const mw = this.live2dModel.width;
                const mh = this.live2dModel.height;
                const winW = window.innerWidth;
                const winH = window.innerHeight;
                this.live2dModel.x = Math.max(-mw + margin, Math.min(winW - margin, newX));
                this.live2dModel.y = Math.max(-mh + margin, Math.min(winH - margin, newY));
            });

            document.addEventListener('mouseup', (e) => {
                if (e.button !== 0 || !this._isDragging) return;
                this._isDragging = false;
                this._saveTransform();
            });

            // 键盘缩放：⌘=/⌘+ 放大，⌘- 缩小，⌘0 重置（和浏览器一致）
            document.addEventListener('keydown', (e) => {
                if (!(e.metaKey || e.ctrlKey)) return;
                if (e.key === '=' || e.key === '+') {
                    e.preventDefault();
                    this.zoomIn();
                } else if (e.key === '-' || e.key === '_') {
                    e.preventDefault();
                    this.zoomOut();
                } else if (e.key === '0') {
                    e.preventDefault();
                    this.resetTransform();
                }
            });

        } catch (err) {
            console.error('加载 Live2D 模型失败:', err);
        }
    }

    /**
     * 初始化音频分析器 - 使用音频播放器的分析器节点
     */
    initializeAudioAnalyzer() {
        try {
            const audioPlayer = window.chatApp?.audioPlayer;
            if (!audioPlayer) {
                console.warn('音频播放器未初始化');
                return false;
            }
            this.analyser = audioPlayer.getAnalyser();
            if (!this.analyser) {
                console.warn('无法获取分析器节点');
                return false;
            }
            this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
            return true;
        } catch (error) {
            console.error('初始化音频分析器失败:', error);
            return false;
        }
    }

    /**
     * 连接到音频播放器的输出节点
     */
    connectToAudioPlayer() {
        try {
            const audioPlayer = window.chatApp?.audioPlayer;
            if (!audioPlayer) return false;
            this.analyser = audioPlayer.getAnalyser();
            if (!this.analyser) return false;
            this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
            return true;
        } catch (error) {
            console.error('连接到音频播放器失败:', error);
            return false;
        }
    }

    /**
     * 嘴部动画循环
     */
    animateMouth() {
        if (!this.isTalking) return;
        if (!this.live2dModel) return;
        const internal = this.live2dModel && this.live2dModel.internalModel;
        if (internal && internal.coreModel) {
            const coreModel = internal.coreModel;

            let mouthOpenY = 0;
            let mouthForm = 0;
            let mouthForm2 = 0;
            let average = 0;

            if (this.analyser && this.dataArray) {
                this.analyser.getByteFrequencyData(this.dataArray);
                average = this.dataArray.reduce((a, b) => a + b) / this.dataArray.length;

                const normalizedVolume = average / 255;

                // 获取模型特定的阈值
                let lowThreshold = 0.3;
                let highThreshold = 0.7;
                if (this.currentModelName && this.modelConfig[this.currentModelName]) {
                    lowThreshold = this.modelConfig[this.currentModelName].mouthThresholds?.low || 0.3;
                    highThreshold = this.modelConfig[this.currentModelName].mouthThresholds?.high || 0.7;
                }

                // 使用模型特定的阈值进行映射
                let minOpenY = 0.1;
                if (this.currentModelName && this.modelConfig[this.currentModelName]) {
                    minOpenY = this.modelConfig[this.currentModelName].mouthMinOpenY || 0.1;
                }

                if (normalizedVolume < lowThreshold) {
                    mouthOpenY = minOpenY + Math.pow(normalizedVolume / lowThreshold, 1.5) * (0.4 - minOpenY);
                } else if (normalizedVolume < highThreshold) {
                    mouthOpenY = 0.4 + (normalizedVolume - lowThreshold) / (highThreshold - lowThreshold) * 0.4;
                } else {
                    mouthOpenY = 0.8 + Math.pow((normalizedVolume - highThreshold) / (1 - highThreshold), 1.2) * 0.2;
                }

                // 应用模型特定的嘴部开合幅度
                let amplitudeMultiplier = 1.0;
                let maxOpenY = 2.5;
                if (this.currentModelName && this.modelConfig[this.currentModelName]) {
                    amplitudeMultiplier = this.modelConfig[this.currentModelName].mouthAmplitude;
                    maxOpenY = this.modelConfig[this.currentModelName].maxOpenY || 2.5;
                }
                mouthOpenY = mouthOpenY * amplitudeMultiplier;
                mouthOpenY = Math.min(Math.max(mouthOpenY, 0), maxOpenY);

                // 计算嘴型参数（仅对支持嘴型变化的模型）
                if (this.currentModelName && this.modelConfig[this.currentModelName]?.mouthFormParam) {
                    const config = this.modelConfig[this.currentModelName];
                    const formAmplitude = config.mouthFormAmplitude || 0.5;
                    const form2Amplitude = config.mouthForm2Amplitude || 0;

                    // 嘴型随音量变化：
                    // 低音量：嘴型偏"一"字（负值）
                    // 高音量：嘴型偏"o"字（正值）
                    // 音量=0时：嘴型=0（自然状态）
                    mouthForm = (normalizedVolume - 0.5) * 2 * formAmplitude;
                    mouthForm = Math.max(-formAmplitude, Math.min(formAmplitude, mouthForm));

                    // 第二嘴型参数（natori特有）
                    if (config.mouthForm2Param) {
                        mouthForm2 = (normalizedVolume - 0.3) * 2 * form2Amplitude;
                        mouthForm2 = Math.max(-form2Amplitude, Math.min(form2Amplitude, mouthForm2));
                    }
                }

                // 调试日志：输出嘴部参数
                console.log(`[Live2D] 模型: ${this.currentModelName || 'unknown'}, 音量: ${average?.toFixed(0)}, OpenY: ${mouthOpenY.toFixed(3)}, Form: ${mouthForm.toFixed(3)}, Form2: ${mouthForm2.toFixed(3)}`);
            }

            // 设置嘴部开合参数
            coreModel.setParameterValueById(this.mouthParam, mouthOpenY);

            // 设置嘴型参数（仅对支持嘴型变化的模型）
            if (this.currentModelName && this.modelConfig[this.currentModelName]?.mouthFormParam) {
                const config = this.modelConfig[this.currentModelName];
                const formParam = config.mouthFormParam;
                coreModel.setParameterValueById(formParam, mouthForm);

                // 设置第二嘴型参数（natori特有）
                if (config.mouthForm2Param) {
                    coreModel.setParameterValueById(config.mouthForm2Param, mouthForm2);
                }
            }

            coreModel.update();
        }
        this.mouthAnimationId = requestAnimationFrame(() => this.animateMouth());
    }

    /**
     * 开始说话动画
     */
    startTalking() {
        if (this.isTalking || !this.live2dModel) return;

        // 确保音频分析器已初始化
        if (!this.analyser) {
            if (!this.initializeAudioAnalyzer()) {
                console.warn('音频分析器初始化失败，将使用模拟动画');
                // 即使分析器初始化失败，也启动动画（使用模拟数据）
                this.isTalking = true;
                this.animateMouth();
                return;
            }
        }

        // 连接到音频播放器输出
        if (!this.connectToAudioPlayer()) {
            console.warn('无法连接到音频播放器输出，将使用模拟动画');
        }

        this.isTalking = true;
        this.animateMouth();
    }

    /**
     * 停止说话动画
     */
    stopTalking() {
        this.isTalking = false;
        if (this.mouthAnimationId) {
            cancelAnimationFrame(this.mouthAnimationId);
            this.mouthAnimationId = null;
        }

        // 重置嘴部参数
        if (this.live2dModel) {
            const internal = this.live2dModel.internalModel;
            if (internal && internal.coreModel) {
                const coreModel = internal.coreModel;
                coreModel.setParameterValueById(this.mouthParam, 0);
                coreModel.update();
            }
        }
    }

    /**
     * 基于情绪触发动作
     * @param {string} emotion - 情绪名称
     */
    triggerEmotionAction(emotion) {
        if (!this.live2dModel) return;

        // 添加冷却时间控制，避免过于频繁触发
        const now = Date.now();
        if (this.lastEmotionActionTime && now - this.lastEmotionActionTime < 5000) { // 5秒冷却时间
            return;
        }

        // 根据情绪获取对应的动作
        const action = this.emotionToActionMap[emotion] || this.emotionToActionMap['default'];

        // 触发动作并记录时间
        this.motion(action);
        this.lastEmotionActionTime = now;
    }

    /**
     * Check if a window-relative coordinate hits the Live2D model's bounding rect.
     * Used by Electron Pet Mode for click-through hit-testing. Loose rectangular
     * test — acceptable for MVP.
     * @param {number} x — window-relative X
     * @param {number} y — window-relative Y
     * @returns {boolean}
     */
    isHitOnModel(x, y) {
        if (!this.live2dModel) return false;
        const b = this.live2dModel.getBounds();
        if (!b) return false;
        return x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
    }

    /**
     * 绝对缩放到指定值，clamp 到 `[_minScale, _maxScale]`，并保持模型
     * 视觉中心不动（用 getBounds 做 center correction；直接 scale.set 会从
     * pivot 左上角放缩，产生"放大右下漂 / 缩小左上漂"的观感）。
     * @param {number} scale
     */
    setScale(scale) {
        if (!this.live2dModel) return;
        const clamped = Math.max(this._minScale, Math.min(this._maxScale, scale));
        const prev = this.live2dModel.getBounds();
        const prevCenterX = prev.x + prev.width * 0.5;
        const prevCenterY = prev.y + prev.height * 0.5;
        this.live2dModel.scale.set(clamped);
        const next = this.live2dModel.getBounds();
        const nextCenterX = next.x + next.width * 0.5;
        const nextCenterY = next.y + next.height * 0.5;
        this.live2dModel.x += prevCenterX - nextCenterX;
        this.live2dModel.y += prevCenterY - nextCenterY;
        this._saveTransform();
    }

    zoomIn() {
        if (!this.live2dModel) return;
        this.setScale(this.live2dModel.scale.x * (1 + this._keyScaleStep));
    }

    zoomOut() {
        if (!this.live2dModel) return;
        this.setScale(this.live2dModel.scale.x / (1 + this._keyScaleStep));
    }

    zoomReset() {
        this.setScale(this._defaultScale);
    }

    /**
     * 把模型**可见中心**放到窗口中心（scale 保持不变）。
     * 用 getBounds() 排除透明 padding，居中的是人物而非纹理 box。
     */
    centerModel() {
        if (!this.live2dModel) return;
        const bounds = this.live2dModel.getBounds();
        if (bounds && bounds.width > 0 && bounds.height > 0) {
            const targetCX = window.innerWidth * 0.5;
            const targetCY = window.innerHeight * 0.5;
            const curCX = bounds.x + bounds.width * 0.5;
            const curCY = bounds.y + bounds.height * 0.5;
            this.live2dModel.x += targetCX - curCX;
            this.live2dModel.y += targetCY - curCY;
        } else {
            this.live2dModel.x = (window.innerWidth - this.live2dModel.width) * 0.5;
            this.live2dModel.y = (window.innerHeight - this.live2dModel.height) * 0.5;
        }
        this._saveTransform();
    }

    /**
     * 完整重置：默认 scale + 右上角。⌘0 绑定到这个。
     */
    resetTransform() {
        this.setScale(this._defaultScale);
        this._positionTopRight(-115, -45);
        this._saveTransform();
    }

    /**
     * Resize the PIXI renderer + canvas drawing buffer and re-center the model.
     * Called by the Electron Pet Mode mode-change handshake after window bounds
     * change. OLV's use-live2d-resize.ts does the same: update canvas w/h +
     * renderer.resize + re-center. We keep it simple (no DPR fiddling —
     * PIXI.Application was created with autoDensity:true, which handles that).
     * @param {number} width — new window width in CSS pixels
     * @param {number} height — new window height in CSS pixels
     */
    resizeCanvas(width, height) {
        if (!this.live2dApp || !this.live2dApp.renderer) return;
        this.live2dApp.renderer.resize(width, height);
        if (!this.live2dModel) return;
        if (localStorage.getItem('live2dFX') === null) {
            this._positionTopRight(20);
        } else {
            this._clampModelInView();
        }
    }

    /**
     * 触发模型动作（Motion）
     * @param {string} name - 动作分组名称，如 'TapBody'、'FlickUp'、'Idle' 等
     */
    motion(name) {
        try {
            if (!this.live2dModel) return;

            // 根据当前模型获取对应的动作名称
            let actualMotionName = name;
            if (this.currentModelName && this.modelConfig[this.currentModelName]) {
                const motionMap = this.modelConfig[this.currentModelName].motionMap;
                actualMotionName = motionMap[name] || name;
            }

            this.live2dModel.motion(actualMotionName);
        } catch (error) {
            console.error('触发动作失败:', error);
        }
    }

    /**
     * 设置模型交互事件
     */
    setupModelInteractions() {
        if (!this.live2dModel) return;

        this.live2dModel.interactive = true;

        // Debug helper: log which motion was triggered, to chat panel
        const logReaction = (event, area, motionName, extra = '') => {
            if (!window.petOverlay?.appendMessage) return;
            const motion = motionName || '(none)';
            const tail = extra ? ` ${extra}` : '';
            window.petOverlay.appendMessage({
                text: `${event} ${area} → ${motion}${tail}`,
                role: 'system',
            });
        };

        this.live2dModel.on('doublehit', (args) => {
            const area = Array.isArray(args) ? args[0] : args;

            let motionName = null;
            if (area === 'Body' || area === 'Skirt') {
                motionName = 'Flick@Body';
            } else if (area === 'Head' || area === 'Face') {
                motionName = 'Flick';
            }
            if (motionName) this.motion(motionName);
            logReaction('doublehit', area, motionName);

            const app = window.chatApp;
            const payload = JSON.stringify({ type: 'live2d', event: 'doublehit', area });
            if (app && app.dataChannel && app.dataChannel.readyState === 'open') {
                app.dataChannel.send(payload);
            }
        });

        this.live2dModel.on('singlehit', (args) => {
            const area = Array.isArray(args) ? args[0] : args;

            let motionName = null;
            if (area === 'Body' || area === 'Skirt') {
                motionName = 'Tap@Body';
            } else if (area === 'Head' || area === 'Face') {
                motionName = 'Tap';
            }
            if (motionName) this.motion(motionName);
            logReaction('singlehit', area, motionName);

            const app = window.chatApp;
            const payload = JSON.stringify({ type: 'live2d', event: 'singlehit', area });
            if (app && app.dataChannel && app.dataChannel.readyState === 'open') {
                app.dataChannel.send(payload);
            }
        });

        this.live2dModel.on('swipe', (args) => {
            const area = Array.isArray(args) ? args[0] : args;
            const dir = Array.isArray(args) ? args[1] : undefined;

            let motionName = null;
            if (dir === 'up') {
                motionName = 'FlickUp';
            } else if (dir === 'down') {
                motionName = 'FlickDown';
            }
            if (motionName) this.motion(motionName);
            logReaction('swipe', area, motionName, `dir=${dir}`);

            const app = window.chatApp;
            const payload = JSON.stringify({ type: 'live2d', event: 'swipe', area, dir });
            if (app && app.dataChannel && app.dataChannel.readyState === 'open') {
                app.dataChannel.send(payload);
            }
        });

        // 自定义"头部/身体/裙子"命中区域 + 单/双击/滑动区分
        this.live2dModel.on('pointerdown', (event) => {
            try {
                // ⌘/Ctrl + 左键走 drag（document mousedown 处理），此处早退
                const oe = event.data?.originalEvent;
                if (oe && (oe.metaKey || oe.ctrlKey)) return;
                const global = event.data.global;
                const bounds = this.live2dModel.getBounds();
                if (!bounds || !bounds.contains(global.x, global.y)) return;

                const relX = (global.x - bounds.x) / (bounds.width || 1);
                const relY = (global.y - bounds.y) / (bounds.height || 1);
                let area = '';

                // 裙子区域（优先级高于 Body）
                if (relX >= 0.39 && relX <= 0.63 && relY >= 0.54 && relY <= 0.57) {
                    area = 'Skirt';
                } else if (relX >= 0.4 && relX <= 0.6) {
                    // 经验阈值：模型可见矩形的上部 20% 视为"头部"区域
                    if (relY <= 0.15) {
                        area = 'Head';
                    } else if (relY <= 0.23) {
                        area = 'Face';
                    } else {
                        area = 'Body';
                    }
                }
                // Debug: log every in-bounds click (area + rel coords) to chat panel
                if (window.petOverlay?.appendMessage) {
                    window.petOverlay.appendMessage({
                        text: `hit ${area || 'miss'} (${relX.toFixed(2)}, ${relY.toFixed(2)})`,
                        role: 'system',
                    });
                }
                if (area === '') {
                    return;
                }

                // Skirt combo counter
                if (area === 'Skirt') {
                    this._skirtTapCount += 1;
                    if (this._skirtResetTimer) clearTimeout(this._skirtResetTimer);
                    this._skirtResetTimer = setTimeout(() => {
                        this._skirtTapCount = 0;
                    }, this._skirtWindowMs);

                    if (this._skirtTapCount >= this._skirtCombo) {
                        this._skirtTapCount = 0;
                        if (this._skirtResetTimer) {
                            clearTimeout(this._skirtResetTimer);
                            this._skirtResetTimer = null;
                        }
                        this._triggerSkirtEasterEgg();
                        this._pointerDown = false;
                        return;
                    }

                    if (window.petOverlay?.appendMessage) {
                        window.petOverlay.appendMessage({
                            text: `skirt combo ${this._skirtTapCount}/${this._skirtCombo}`,
                            role: 'system',
                        });
                    }
                }

                // 记录按下状态用于滑动判定
                this._pointerDown = true;
                this._downPos = { x: global.x, y: global.y };
                this._downTime = performance.now();
                this._downArea = area;
                this._movedBeyondClick = false;

                const now = performance.now();
                const dt = now - (this._lastClickTime || 0);
                const dx = global.x - (this._lastClickPos?.x || 0);
                const dy = global.y - (this._lastClickPos?.y || 0);
                const dist = Math.hypot(dx, dy);

                if (this._lastClickTime && dt <= this._doubleClickMs && dist <= this._doubleClickDist) {
                    if (this._singleClickTimer) {
                        clearTimeout(this._singleClickTimer);
                        this._singleClickTimer = null;
                    }
                    if (typeof this.live2dModel.emit === 'function') {
                        this.live2dModel.emit('doublehit', [area]);
                    }
                    this._lastClickTime = 0;
                    this._pointerDown = false;
                    return;
                }

                this._lastClickTime = now;
                this._lastClickPos = { x: global.x, y: global.y };
                if (this._singleClickTimer) {
                    clearTimeout(this._singleClickTimer);
                    this._singleClickTimer = null;
                }
                this._singleClickTimer = setTimeout(() => {
                    if (!this._movedBeyondClick && typeof this.live2dModel.emit === 'function') {
                        this.live2dModel.emit('singlehit', [area]);
                    }
                    this._singleClickTimer = null;
                    this._lastClickTime = 0;
                }, this._doubleClickMs);
            } catch (e) {
                // 忽略命中判断异常
            }
        });

        // 指针移动：判定点击升级为滑动
        this.live2dModel.on('pointermove', (event) => {
            try {
                if (!this._pointerDown) return;
                const global = event.data.global;
                const dx = global.x - this._downPos.x;
                const dy = global.y - this._downPos.y;
                const dist = Math.hypot(dx, dy);

                if (dist > this._doubleClickDist) {
                    this._movedBeyondClick = true;
                    if (this._singleClickTimer) {
                        clearTimeout(this._singleClickTimer);
                        this._singleClickTimer = null;
                    }
                    this._lastClickTime = 0;
                }
            } catch (e) { /* ignore */ }
        });

        // 指针抬起：确认滑动
        const handlePointerUp = (event) => {
            try {
                if (!this._pointerDown) return;
                const global = (event && event.data && event.data.global) ? event.data.global : { x: this._downPos.x, y: this._downPos.y };
                const dx = global.x - this._downPos.x;
                const dy = global.y - this._downPos.y;
                const dist = Math.hypot(dx, dy);

                if (this._movedBeyondClick && dist >= this._swipeMinDist) {
                    if (typeof this.live2dModel.emit === 'function') {
                        const dir = Math.abs(dx) >= Math.abs(dy)
                            ? (dx > 0 ? 'right' : 'left')
                            : (dy > 0 ? 'down' : 'up');
                        this.live2dModel.emit('swipe', [this._downArea, dir]);
                    }
                    if (this._singleClickTimer) {
                        clearTimeout(this._singleClickTimer);
                        this._singleClickTimer = null;
                    }
                    this._lastClickTime = 0;
                }
            } catch (e) { /* ignore */ }
            finally {
                this._pointerDown = false;
                this._movedBeyondClick = false;
            }
        };

        this.live2dModel.on('pointerup', handlePointerUp);
        this.live2dModel.on('pointerupoutside', handlePointerUp);
    }

    /**
     * 清理资源
     */
    destroy() {
        this.stopTalking();

        // 清理音频分析器
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        this.analyser = null;
        this.dataArray = null;

        // 清理 Live2D 应用
        if (this.live2dApp) {
            this.live2dApp.destroy(true);
            this.live2dApp = null;
        }
        this.live2dModel = null;
    }

    /**
     * 切换 Live2D 模型
     * @param {string} modelName - 模型目录名称，如 'hiyori_pro_zh'、'natori_pro_zh'
     * @returns {Promise<boolean>} - 切换是否成功
     */
    async switchModel(modelName) {
        try {
            // 获取模型文件名映射
            const modelFileMap = {
                'hiyori_pro_zh': { file: 'hiyori_pro_t11.model3.json', subdir: 'runtime/' },
                'natori_pro_zh': { file: 'natori_pro_t06.model3.json', subdir: 'runtime/' },
                'Mao': { file: 'Mao.model3.json', subdir: '' },
                'Haru': { file: 'Haru.model3.json', subdir: '' },
                'Rice': { file: 'Rice.model3.json', subdir: '' },
                'Murasame_Yukata': { file: 'Murasame_Yukata.model3.json', subdir: '' },
                'Senko_Normals': { file: 'senko.model3.json', subdir: '' }
            };

            const modelInfo = modelFileMap[modelName];
            if (!modelInfo) {
                console.error('未知的模型名称:', modelName);
                return false;
            }

            // 获取基础路径
            const currentPath = window.location.pathname;
            const lastSlashIndex = currentPath.lastIndexOf('/');
            const basePath = currentPath.substring(0, lastSlashIndex + 1);
            const modelPath = basePath + 'resources/' + modelName + '/' + modelInfo.subdir + modelInfo.file;

            // 切换前先抓"当前可见"的大小+中心（以可见 bounds 为准，
            // 避免不同模型的纹理 padding 差异把切换后 scale 算歪）
            let prevVisible = null;
            const prevModelName = this.currentModelName;
            if (this.live2dModel) {
                const b = this.live2dModel.getBounds();
                if (b && b.width > 0 && b.height > 0) {
                    prevVisible = {
                        width: b.width,
                        height: b.height,
                        centerX: b.x + b.width * 0.5,
                        centerY: b.y + b.height * 0.5,
                    };
                }

                this.live2dApp.stage.removeChild(this.live2dModel);
                this.live2dModel.destroy();
                this.live2dModel = null;
            }

            // 每个模型的"切换后微调偏移"（相对逻辑中心，正值向右/下）。
            // 切到有偏移的模型会加上；切走时扣回去，等于回到无偏移基准。
            const MODEL_OFFSETS = {
                Murasame_Yukata: { dx: -35, dy: 20 },
            };
            const prevOffset = MODEL_OFFSETS[prevModelName] || { dx: 0, dy: 0 };
            const newOffset = MODEL_OFFSETS[modelName] || { dx: 0, dy: 0 };

            // 显示加载状态
            const app = window.chatApp;
            if (app) {
                app.setModelLoadingStatus(true);
            }

            // 加载新模型
            this.live2dModel = await PIXI.live2d.Live2DModel.from(modelPath);
            this.live2dApp.stage.addChild(this.live2dModel);

            // 保持"可见大小 + 可见中心"在切换前后不变：
            // 不同模型纹理尺寸不同，直接复用旧 scale 会一大一小，
            // 所以用 getBounds() 量新模型在 scale=1 时的可见宽度，
            // 求出让新模型可见宽度等于旧可见宽度的 scale 因子。
            if (prevVisible) {
                this.live2dModel.scale.set(1);
                const measured = this.live2dModel.getBounds();
                if (measured && measured.width > 0) {
                    const factor = prevVisible.width / measured.width;
                    this.live2dModel.scale.set(factor);
                    const nb = this.live2dModel.getBounds();
                    const curCX = nb.x + nb.width * 0.5;
                    const curCY = nb.y + nb.height * 0.5;
                    // 目标可见中心 = 旧中心 - 旧模型偏移 + 新模型偏移
                    const targetCX = prevVisible.centerX - prevOffset.dx + newOffset.dx;
                    const targetCY = prevVisible.centerY - prevOffset.dy + newOffset.dy;
                    this.live2dModel.x += targetCX - curCX;
                    this.live2dModel.y += targetCY - curCY;
                    this._saveTransform();
                } else {
                    this._applyTransform(0.28);
                }
            } else {
                // 没有前一个模型（首次加载路径几乎不会命中这里，保留兜底）
                this._applyTransform(0.28);
            }

            // 重新绑定交互事件
            this.setupModelInteractions();

            // 隐藏加载状态
            if (app) {
                app.setModelLoadingStatus(false);
            }

            // 保存当前模型名称
            this.currentModelName = modelName;

            // 设置模型特定的嘴部参数名
            if (this.modelConfig[modelName]) {
                this.mouthParam = this.modelConfig[modelName].mouthParam || 'ParamMouthOpenY';
            }

            // 保存到 localStorage
            localStorage.setItem('live2dModel', modelName);

            // 更新下拉框显示
            const modelSelect = document.getElementById('live2dModelSelect');
            if (modelSelect) {
                modelSelect.value = modelName;
            }

            console.log('模型切换成功:', modelName);
            return true;
        } catch (error) {
            console.error('切换模型失败:', error);
            const app = window.chatApp;
            if (app) {
                app.setModelLoadingStatus(false);
            }
            return false;
        }
    }

    /**
     * Skirt 彩蛋：连续 5 次点击裙子区域后触发。
     * 效果：切换隐藏（continuous）模式 —— 等同于长按录音键 3 秒。
     */
    _triggerSkirtEasterEgg() {
        const uic = window.uiController;
        const available = typeof uic?._toggleContinuousMode === 'function';
        if (available) uic._toggleContinuousMode();

        // 表情/动作反应：Flick@Body → FlickDown 连播（通过 motionMap 自动适配别的模型）
        if (this.live2dModel) {
            this.motion('Flick@Body');
            setTimeout(() => this.motion('FlickDown'), 500);
        }

        if (window.petOverlay?.appendMessage) {
            const text = available
                ? `skirt combo: hidden mode ${uic._continuousMode ? 'on' : 'off'}`
                : 'skirt combo: uiController unavailable';
            window.petOverlay.appendMessage({ text, role: 'system' });
        }
    }

    /**
     * 返回当前模型的所有动作组名（从 Live2D 内部 motion 定义读取）。
     * 用于 pet overlay 的 /emote 二级选单。
     * @returns {string[]}
     */
    getMotionGroups() {
        if (!this.live2dModel) return [];
        try {
            const defs = this.live2dModel.internalModel?.motionManager?.definitions;
            if (defs && typeof defs === 'object') {
                return Object.keys(defs).filter((k) => Array.isArray(defs[k]) && defs[k].length > 0);
            }
        } catch (e) {
            // 忽略
        }
        return [];
    }

}

// 导出全局实例
window.Live2DManager = Live2DManager;
