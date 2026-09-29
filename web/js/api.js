// Общий клиент: REST, WebSocket с переподключением, форматирование времени.
// Токен хранится в sessionStorage — у каждой вкладки свой вход (преподаватель и обучающийся
// могут работать в соседних вкладках одного браузера).
'use strict';

const API = (() => {
  const KEY_TOKEN = 'a112.token';
  const KEY_USER = 'a112.user';

  function store(key, value) {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (e) { /* приватный режим — живём без сохранения */ }
  }

  function load(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }

  let memToken = load(KEY_TOKEN);
  let memUser = null;
  try { memUser = JSON.parse(load(KEY_USER) || 'null'); } catch (e) { memUser = null; }

  async function call(method, path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (memToken) headers.Authorization = 'Bearer ' + memToken;
    const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      const detail = data && typeof data.detail === 'string' ? data.detail : 'ошибка запроса (' + resp.status + ')';
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  // WebSocket занятия. Переподключается сам с нарастающей паузой; токен — первым сообщением.
  function socket(lessonId, handlers) {
    let ws = null;
    let stopped = false;
    let attempt = 0;
    let timer = null;

    function open() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(proto + '://' + location.host + '/ws?lesson=' + encodeURIComponent(lessonId));
      ws.onopen = () => {
        ws.send(JSON.stringify({ t: 'hello', token: memToken }));
        attempt = 0;
        handlers.status && handlers.status('online');
      };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        handlers.message(msg);
      };
      ws.onclose = (ev) => {
        ws = null;
        if (stopped) return;
        if (ev.code === 4401 || ev.code === 4403 || ev.code === 4404) {
          handlers.status && handlers.status('denied', ev.code);
          return;
        }
        handlers.status && handlers.status('reconnecting');
        timer = setTimeout(open, Math.min(5000, 400 * Math.pow(2, attempt++)));
      };
    }

    open();
    return {
      send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(msg));
          return true;
        }
        return false;
      },
      close() {
        stopped = true;
        clearTimeout(timer);
        if (ws) ws.close();
      },
    };
  }

  return {
    get token() { return memToken; },
    get user() { return memUser; },
    save(token, user) {
      memToken = token;
      memUser = user;
      store(KEY_TOKEN, token);
      store(KEY_USER, JSON.stringify(user));
    },
    clear() {
      memToken = null;
      memUser = null;
      store(KEY_TOKEN, null);
      store(KEY_USER, null);
    },
    call,
    socket,
  };
})();

// Время: сервер присылает секунды эпохи; часы клиента выравниваются по серверу.
const Clock = (() => {
  let offset = 0; // серверное время минус локальное, с
  const pad = (n) => String(n).padStart(2, '0');
  const DAYS = ['Воскресенье', 'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];
  // На стенде месяц в именительном падеже: «Четверг, 17 Сентябрь 2026» (sdds_02)
  const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август',
    'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];

  return {
    sync(serverTime) { if (typeof serverTime === 'number') offset = serverTime - Date.now() / 1000; },
    now() { return Date.now() / 1000 + offset; },
    date(t) { return new Date(t * 1000); },
    hms(t) { const d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); },
    hm(t) { const d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); },
    ss(t) { return pad(new Date(t * 1000).getSeconds()); },
    dmy(t) { const d = new Date(t * 1000); return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear(); },
    dmyShort(t) { const d = new Date(t * 1000); return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + String(d.getFullYear()).slice(2); },
    longDate(t) { const d = new Date(t * 1000); return DAYS[d.getDay()] + ', ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear(); },
    span(seconds) {
      const s = Math.max(0, Math.floor(seconds));
      return pad(Math.floor(s / 60)) + ':' + pad(s % 60);
    },
  };
})();

// Мини-шаблонизатор: безопасная вставка текста.
function esc(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatPhone(raw) {
  const digits = String(raw || '').replace(/\D/g, '');
  if (digits.length !== 11) return raw || '';
  return '+7 (' + digits.slice(1, 4) + ') ' + digits.slice(4, 7) + '-' + digits.slice(7, 9) + '-' + digits.slice(9);
}
