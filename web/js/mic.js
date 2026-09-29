// Микрофон оператора 112 (узел 9): звук → сервер распознавания (/ws/voice) → текст фразы.
// Звук микрофона фильтруется, прореживается до 16 кГц (16 бит, моно) и уходит на сервер кусками по 0,1 с.
// Пока звучит голос заявителя или нет разговора (held()), звук не передаётся: распознавание не должно
// слышать заявителя из колонок. Браузер даёт микрофон только в защищённом контексте — по HTTPS или на
// этом же компьютере (localhost); в классе — сертификат сервера или режим киоска (deploy/kiosk).
'use strict';

const Mic = (() => {
  const RATE = 16000;
  let ws = null;
  let ctx = null;
  let media = null;
  let nodes = [];
  let active = false;
  let lessonId = null;
  let retry = null;
  let wasHeld = true;
  let h = {};
  let held = () => false;

  function supported() {
    return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function why() {
    return window.isSecureContext ? 'браузер не даёт доступ к микрофону'
      : 'микрофон работает только по HTTPS или на этом компьютере (localhost)';
  }

  // Прореживание до 16 кГц для браузеров без AudioWorklet — тот же алгоритм, что в mic-worklet.js
  function decimator(rate) {
    const ratio = rate / RATE;
    let pos = 0;
    let acc = 0;
    let n = 0;
    return (input) => {
      const out = new Int16Array(Math.ceil(input.length / ratio) + 1);
      let k = 0;
      for (let i = 0; i < input.length; i++) {
        acc += input[i]; n += 1; pos += 1;
        if (pos >= ratio) {
          pos -= ratio;
          const v = Math.max(-1, Math.min(1, acc / n));
          acc = 0; n = 0;
          out[k++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        }
      }
      return out.slice(0, k).buffer;
    };
  }

  function loudness(buf) { // громкость куска 0..1 — для индикатора
    const a = new Int16Array(buf);
    let s = 0;
    for (let i = 0; i < a.length; i++) s += a[i] * a[i];
    return Math.min(1, Math.sqrt(s / Math.max(1, a.length)) / 6000);
  }

  function send(buf) {
    const hold = held();
    if (hold && !wasHeld) end();          // заявитель заговорил — фраза оператора закончена
    wasHeld = hold;
    if (h.level) h.level(hold ? 0 : loudness(buf));
    if (!hold && ws && ws.readyState === 1) ws.send(buf);
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(proto + '://' + location.host + '/ws/voice?lesson=' + encodeURIComponent(lessonId));
    ws = sock;
    sock.binaryType = 'arraybuffer';
    sock.onopen = () => sock.send(JSON.stringify({ t: 'hello', token: API.token }));
    sock.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch (x) { return; }
      if (m.t === 'ready') { if (h.state) h.state('ready'); }
      else if (m.t === 'partial') { if (h.partial) h.partial(m.text); }
      else if (m.t === 'final') { if (h.final) h.final(m.text, m.intent || null); }
      else if (m.t === 'unavailable') { const fail = h.error; stop(); if (fail) fail(m.reason); }
    };
    sock.onclose = (e) => {
      if (ws !== sock) return;
      ws = null;
      if (!active) return;
      if (e.code === 4401 || e.code === 4403) { const fail = h.error; stop(); if (fail) fail('нет доступа к распознаванию речи'); return; }
      if (e.code === 4503) return;       // причину уже сообщил сервер (unavailable)
      if (h.state) h.state('reconnect');
      retry = setTimeout(() => { retry = null; if (active) connect(); }, 2000);
    };
  }

  async function start(lesson, handlers, isHeld) {
    if (active) return;
    if (!supported()) throw new Error(why());
    h = handlers || {};
    held = isHeld || (() => false);
    lessonId = lesson;
    active = true;
    wasHeld = true;
    try {
      media = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (!active) { stop(); return; }
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = ctx.createMediaStreamSource(media);
      const lowpass = ctx.createBiquadFilter();   // срез выше 7 кГц перед прореживанием до 16 кГц
      lowpass.type = 'lowpass';
      lowpass.frequency.value = 7000;
      const silent = ctx.createGain();            // граф звука должен доходить до выхода, сам звук — нет
      silent.gain.value = 0;
      let tap;
      if (ctx.audioWorklet && window.AudioWorkletNode) {
        await ctx.audioWorklet.addModule('js/mic-worklet.js');
        tap = new AudioWorkletNode(ctx, 'pcm16k', { processorOptions: { rate: ctx.sampleRate } });
        tap.port.onmessage = (e) => send(e.data);
      } else {
        const dec = decimator(ctx.sampleRate);
        tap = ctx.createScriptProcessor(4096, 1, 1);
        tap.onaudioprocess = (e) => send(dec(e.inputBuffer.getChannelData(0)));
      }
      src.connect(lowpass);
      lowpass.connect(tap);
      tap.connect(silent);
      silent.connect(ctx.destination);
      nodes = [src, lowpass, tap, silent];
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});  // без действия пользователя браузер ждёт клика
      connect();
    } catch (e) {
      stop();
      throw e;
    }
  }

  // Конец фразы по команде (микрофон выключен, вызов окончен, заговорил заявитель): итог сразу, без паузы
  function end() {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'end' }));
  }

  function stop() {
    active = false;
    if (retry) { clearTimeout(retry); retry = null; }
    if (ws) {
      const s = ws;
      ws = null;
      try { s.close(1000); } catch (e) { /* уже закрыт */ }
    }
    for (const n of nodes) { try { n.disconnect(); } catch (e) { /* уже отключён */ } }
    nodes = [];
    if (media) { for (const t of media.getTracks()) t.stop(); media = null; }
    if (ctx) { ctx.close().catch(() => {}); ctx = null; }
    if (h.state) h.state('off');
  }

  return { supported, why, start, stop, end, get active() { return active; } };
})();
