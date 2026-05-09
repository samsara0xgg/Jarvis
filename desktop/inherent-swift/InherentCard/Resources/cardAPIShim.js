(function() {
  const post = (op, args) => {
    try { window.webkit.messageHandlers.cardAPI.postMessage(Object.assign({op}, args)); }
    catch (e) { console.warn('[shim] cardAPI postMessage failed:', e); }
  };
  const reply = (op, args) => {
    console.log('[shim] reply called op=' + op + ' hasHandler=' + !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cardAPISubmit));
    try { return window.webkit.messageHandlers.cardAPISubmit.postMessage(Object.assign({op}, args)); }
    catch (e) { console.warn('[shim] cardAPISubmit postMessage failed:', e); }
  };
  const arrayBufferToBase64 = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      const chunk = bytes.subarray(i, i + chunkSize);
      binary += String.fromCharCode.apply(null, chunk);
    }
    return btoa(binary);
  };

  window.cardAPI = {
    resize: (h)    => post('resize', {h}),
    setWidth: (w)  => post('setWidth', {w}),
    show: ()       => post('show'),
    close: ()      => post('close'),
    fadeOut: (ms)  => post('fadeOut', {ms}),
    cancelFade: () => post('cancelFade'),
    movePanel: (dx, dy) => post('movePanel', {dx, dy}),
    setDragging: (v)    => post('setDragging', {v}),
    resetPosition: ()   => post('resetPosition'),
    submit: (text) => reply('submit', {text}),
    submitImage: (payload) => reply('submitImage', {
      text: payload && payload.text ? payload.text : '',
      name: payload && payload.name ? payload.name : 'image.png',
      mime: payload && payload.mime ? payload.mime : 'image/png',
      imageBase64: arrayBufferToBase64(payload && payload.buffer ? payload.buffer : new ArrayBuffer(0))
    }),
    submitVoice: (wavArrayBuffer) => reply('submitVoice', {wavBase64: arrayBufferToBase64(wavArrayBuffer)}),
    pasteClipboard: () => reply('pasteClipboard', {}),
    duckAudio: () => reply('duckAudio', {}),
    restoreAudio: () => reply('restoreAudio', {}),

    onSiriOpen:    (cb) => window.addEventListener('siri:open',     e => cb(e.detail)),
    onSiriAppend:  (cb) => window.addEventListener('siri:append',   e => cb(e.detail)),
    onSiriDone:    (cb) => window.addEventListener('siri:done',     e => cb(e.detail)),
    onSiriReset:   (cb) => window.addEventListener('siri:reset',    () => cb()),
    onVoiceState:  (cb) => window.addEventListener('card:voice',    e => cb(e.detail)),
    onOpenInput:   (cb) => window.addEventListener('card:openInput', () => cb()),
  };
})();
