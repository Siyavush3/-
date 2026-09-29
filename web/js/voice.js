// Голос тренажёра (узел 5): реплики заявителя и доклады старшего бригады, звонок телефона.
// Звук озвучен заранее на сервере и приходит ссылкой в сообщении (line.audio, call.audio);
// если реплика ещё не озвучена, остаётся текст на экране — занятие от звука не зависит.
'use strict';

const Voice = (() => {
  const KEY = 'a112.sound';
  let on = true;
  try { on = localStorage.getItem(KEY) !== 'off'; } catch (e) { /* без хранилища звук включён */ }
  const queue = [];
  let current = null;
  let ringer = null;
  let blocked = false;
  const listeners = [];

  function notify() { for (const fn of listeners) fn({ on, blocked }); }

  function attempt(audio, onFail) {
    const p = audio.play();
    if (!p || !p.catch) return;
    p.then(() => { if (blocked) { blocked = false; notify(); } }).catch((e) => {
      if (e && e.name === 'NotAllowedError' && !blocked) { blocked = true; notify(); } // браузер ждёт клика по странице
      if (onFail) onFail(e);
    });
  }

  function next() {
    if (current || !queue.length) return;
    const a = new Audio(queue.shift());
    current = a;
    const done = () => { if (current === a) { current = null; next(); } };
    a.onended = done;
    a.onerror = done;
    attempt(a, done);
  }

  function play(url) {
    if (!on || !url) return;
    queue.push(url);
    next();
  }

  function stop() {
    queue.length = 0;
    if (current) { current.pause(); current = null; }
  }

  // Звонок по кругу, пока вызов не принят. fallback — короткий сигнал, если файла звонка нет.
  function ring(start, fallback) {
    if (!start) { if (ringer) ringer.pause(); return; }
    if (!on) return;
    if (!ringer) {
      ringer = new Audio('/tts/ring.wav');
      ringer.loop = true;
      ringer.onerror = () => { ringer = null; if (fallback) fallback(); };
    }
    ringer.currentTime = 0;
    attempt(ringer);
  }

  function set(value) {
    on = !!value;
    try { localStorage.setItem(KEY, on ? 'on' : 'off'); } catch (e) { /* не запомним — не страшно */ }
    if (!on) { stop(); ring(false); }
    notify();
  }

  // Кнопка «Звук» в полосе тренажёра: включить/выключить; если браузер заблокировал звук — подсказка.
  function bindButton(button) {
    if (!button) return;
    const paint = () => {
      button.textContent = !on ? 'Звук: выкл' : blocked ? 'Звук: нажмите, чтобы включить' : 'Звук: вкл';
      button.classList.toggle('warn', on && blocked);
    };
    button.addEventListener('click', () => {
      if (blocked) { blocked = false; notify(); if (ringer && !ringer.paused) attempt(ringer); return; }
      set(!on);
    });
    listeners.push(paint);
    paint();
  }

  // Любой клик по странице снимает блокировку автозапуска звука в обычном (не киосковом) браузере.
  document.addEventListener('pointerdown', () => { if (blocked) { blocked = false; notify(); } }, { capture: true });

  // Звучит ли сейчас что-то (голос заявителя, звонок): микрофон в это время не передаёт — иначе
  // распознавание услышит заявителя из колонок.
  function busy() { return !!current || queue.length > 0 || !!(ringer && !ringer.paused); }

  return { play, stop, ring, set, bindButton, busy, get on() { return on; } };
})();
