// Config manager — localStorage with jarvis_ prefix

export function loadConfig() {
    const serverUrlInput = document.getElementById('serverUrl');
    if (serverUrlInput) {
        const saved = localStorage.getItem('jarvis_serverUrl');
        if (saved) serverUrlInput.value = saved;
    }
}

export function saveConfig() {
    const serverUrlInput = document.getElementById('serverUrl');
    if (serverUrlInput) {
        localStorage.setItem('jarvis_serverUrl', serverUrlInput.value.trim());
    }
}

export function getServerUrl() {
    const input = document.getElementById('serverUrl');
    const url = input ? input.value.trim() : '';
    return url || localStorage.getItem('jarvis_serverUrl') || 'http://localhost:8006';
}
