// Simplified audio stream context — delegates to AudioPlayer's AnalyserNode.
import { getAudioPlayer } from './player.js';

export function getStreamAnalyser() {
    return getAudioPlayer().getAnalyser();
}
