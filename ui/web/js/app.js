// 主应用入口
import { getAudioPlayer } from './core/audio/player.js';
import { checkMicrophoneAvailability, isHttpNonLocalhost } from './core/audio/recorder.js';
import { uiController } from './ui/controller.js';
import { log } from './utils/logger.js';

class App {
    constructor() {
        this.uiController = null;
        this.audioPlayer = null;
        this.live2dManager = null;
    }

    async init() {
        log('正在初始化应用...', 'info');
        this.uiController = uiController;
        this.uiController.init();
        this.audioPlayer = getAudioPlayer();
        await this.audioPlayer.start();
        await this.checkMicrophoneAvailability();
        await this.initLive2D();
        this.setModelLoadingStatus(false);
        log('应用初始化完成', 'success');
    }

    async initLive2D() {
        try {
            if (typeof window.Live2DManager === 'undefined') {
                throw new Error('Live2DManager未加载，请检查脚本引入顺序');
            }
            this.live2dManager = new window.Live2DManager();
            await this.live2dManager.initializeLive2D();
            log('Live2D初始化完成', 'success');
        } catch (error) {
            log(`Live2D初始化失败: ${error.message}`, 'error');
        }
    }

    setModelLoadingStatus(isLoading) {
        const modelLoading = document.getElementById('modelLoading');
        if (modelLoading) {
            modelLoading.style.display = isLoading ? 'flex' : 'none';
        }
    }

    async checkMicrophoneAvailability() {
        try {
            const isAvailable = await checkMicrophoneAvailability();
            const isHttp = isHttpNonLocalhost();
            window.microphoneAvailable = isAvailable;
            window.isHttpNonLocalhost = isHttp;
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(isAvailable, isHttp);
            }
            log(`麦克风可用性: ${isAvailable ? '可用' : '不可用'}`, isAvailable ? 'success' : 'warning');
        } catch (error) {
            log(`检查麦克风可用性失败: ${error.message}`, 'error');
            window.microphoneAvailable = false;
            window.isHttpNonLocalhost = isHttpNonLocalhost();
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(false, window.isHttpNonLocalhost);
            }
        }
    }
}

const app = new App();
window.chatApp = app;
document.addEventListener('DOMContentLoaded', () => {
    app.init();
});
export default app;
