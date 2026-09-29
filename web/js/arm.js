// АРМ-112, вид ДДС: лента карточек, карточка, полоса служб, проставление статуса.
// Всё состояние занятия — на сервере; клиент показывает снимок и шлёт команды.
// Команды нумеруются (seq): при обрыве связи они копятся и уходят заново после
// переподключения, сервер отбрасывает повторы (ТЗ: сбой сети до 30 с — без потери данных).
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  if (me.role !== 'student') { location.replace(me.role === 'admin' ? 'admin.html' : 'teacher.html'); return; }

  const LESSON_KEY = 'a112.lesson.' + me.id;
  const TITLE = 'Система 112 · Тренажёр ДДС';

  const S = {
    lessonId: null,
    lesson: null,
    cards: new Map(),
    result: null,
    resultShown: false,
    conn: 'offline',
    seq: 0,
    pending: new Map(),
    view: 'lobby',
    open: null,
    expanded: new Set(),
    histFor: null,
    form: null,
    calls: [],
    toasts: [],
    notif: 0,
    page: 0,
    perPage: 10,
    query: '',
  };
  let sock = null;
  let lobbyTimer = null;
  let lobbyHtml = '';
  let rowsCache = '';
  let callSeq = 0;

  function getS(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }
  function setS(key, value) {
    try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch (e) { /* без хранения */ }
  }

  // ---------- неотправленные команды ----------
  function pendingKey() { return 'a112.pending.' + me.id + '.' + S.lessonId; }
  function savePending() {
    setS(pendingKey(), JSON.stringify({ seq: S.seq, pending: [...S.pending.values()] }));
  }
  function loadPending() {
    S.seq = 0;
    S.pending = new Map();
    try {
      const raw = JSON.parse(getS(pendingKey()) || 'null');
      if (raw) {
        S.seq = raw.seq || 0;
        for (const m of raw.pending || []) S.pending.set(m.seq, m);
      }
    } catch (e) { /* испорченная запись — начинаем заново */ }
  }

  // ---------- звук: новая карточка, входящий вызов ----------
  let audio = null;
  document.addEventListener('pointerdown', () => {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === 'suspended') audio.resume();
    } catch (e) { audio = null; }
  });
  function beep(freq, dur, delay) {
    if (!audio) return;
    try {
      const t0 = audio.currentTime + (delay || 0);
      const o = audio.createOscillator();
      const g = audio.createGain();
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.07, t0);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
      o.connect(g);
      g.connect(audio.destination);
      o.start(t0);
      o.stop(t0 + dur);
    } catch (e) { /* без звука */ }
  }

  // ---------- лобби ----------
  function show(id) {
    for (const v of ['lobby', 'feed', 'card']) $(v).hidden = v !== id;
  }

  async function showLobby() {
    S.view = 'lobby';
    S.lesson = null;
    closeForm();
    show('lobby');
    document.title = TITLE;
    renderTrainer();
    lobbyHtml = '';
    await refreshLobby();
    clearInterval(lobbyTimer);
    lobbyTimer = setInterval(refreshLobby, 2000);
  }

  async function refreshLobby() {
    let list;
    try {
      list = await API.call('GET', '/api/lessons');
      $('lobby-err').textContent = '';
    } catch (e) {
      if (e.status === 401) return logout('expired');
      $('lobby-err').textContent = e.message;
      return;
    }
    const box = $('lobby-list');
    const html = !list.length
      ? '<div class="lobby-empty">Открытых занятий пока нет — подождите, пока преподаватель создаст занятие.</div>'
      : list.map((l) => {
      const mine = l.participants.some((p) => p.id === me.id);
      return '<div class="lobby-item"><div class="t"><b>' + esc(l.pack.title) + '</b><span>' +
        (l.mode === 'cards' ? 'Оператор 112 · ' : 'ДДС · ') + 'Занятие ' + esc(l.id) +
        ' · роль: ' + esc(l.pack.role_title) + ' · ' + esc(l.state) + ' · участников: ' + l.participants.length +
        '</span></div><button type="button" data-join="' + esc(l.id) + '" data-mine="' + (mine ? 1 : 0) + '" data-mode="' + esc(l.mode || '') + '">' +
        (mine ? 'Вернуться' : 'Войти') + '</button></div>';
      }).join('');
    if (html !== lobbyHtml) { lobbyHtml = html; box.innerHTML = html; } // кнопка не пересоздаётся под курсором
  }

  $('lobby-list').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-join]');
    if (!b) return;
    b.disabled = true;
    try {
      if (b.dataset.mine !== '1') await API.call('POST', '/api/lessons/' + encodeURIComponent(b.dataset.join) + '/join');
      if (b.dataset.mode === 'cards') {       // занятие оператора 112 — свой экран
        setS(LESSON_KEY, b.dataset.join);
        location.replace('op112.html');
        return;
      }
      enterLesson(b.dataset.join);
    } catch (e) {
      $('lobby-err').textContent = e.message;
      b.disabled = false;
    }
  });

  function enterLesson(id) {
    clearInterval(lobbyTimer);
    S.lessonId = id;
    setS(LESSON_KEY, id);
    S.cards = new Map();
    S.calls = [];
    S.toasts = [];
    S.result = null;
    S.resultShown = false;
    S.lesson = null;
    loadPending();
    renderCalls();
    if (sock) sock.close();
    sock = API.socket(id, { message: onMessage, status: onConn });
    S.view = 'feed';
    show('feed');
    renderFeed();
    renderTrainer();
  }

  function onConn(state, code) {
    S.conn = state;
    if (state === 'denied') {
      sock = null;
      S.lessonId = null;
      setS(LESSON_KEY, null);
      if (code === 4401) return logout('expired');
      toast('Занятие недоступно', false);
      return showLobby();
    }
    renderTrainer();
  }

  // ---------- сообщения сервера ----------
  function onMessage(msg) {
    switch (msg.t) {
      case 'state': return onState(msg);
      case 'card': return onCard(msg.card);
      case 'card_status': return onCardStatus(msg);
      case 'call': return onCall(msg);
      case 'status_ack':
        S.pending.delete(msg.seq);
        savePending();
        return undefined;
      case 'status_rejected':
        S.pending.delete(msg.seq);
        savePending();
        toast('Статус не сохранён: ' + msg.reason, false);
        return undefined;
      case 'result':
        S.result = msg.result;
        if (S.lesson) S.lesson.state = 'отчёт';
        S.toasts = [];
        renderCalls();
        closeForm();
        render();
        showResult();
        return undefined;
      case 'error':
        toast(msg.reason || 'ошибка', false);
        return undefined;
      default:
        return undefined;
    }
  }

  function onState(msg) {
    Clock.sync(msg.server_time);
    if (msg.lesson.mode === 'cards') { location.replace('op112.html'); return; }
    S.lesson = msg.lesson;
    S.cards = new Map(msg.cards.map((c) => [c.number, c]));
    S.calls = [];
    for (const c of msg.cards) {
      for (const k of c.calls || []) S.calls.push({ card: c.number, at: k.at, text: k.text, from: k.from, id: ++callSeq });
    }
    S.calls.sort((a, b) => a.at - b.at);
    S.result = msg.result;
    // Команды, которые сервер уже учёл, не нужны; остальные уходят заново по порядку.
    const last = msg.last_seq || 0;
    for (const seq of [...S.pending.keys()]) if (seq <= last) S.pending.delete(seq);
    S.seq = Math.max(S.seq, last);
    for (const m of [...S.pending.values()].sort((a, b) => a.seq - b.seq)) sock.send(m);
    savePending();
    if (S.view === 'card' && S.open) sock.send({ t: 'open_card', card: S.open });
    route();
    if (S.result && !S.resultShown) showResult();
  }

  function onCard(card) {
    S.cards.set(card.number, card);
    S.notif += 1;
    beep(880, 0.18);
    beep(1175, 0.2, 0.2);
    render();
  }

  function onCardStatus(msg) {
    const card = S.cards.get(msg.card);
    if (!card) return;
    const list = card.statuses[msg.service] || (card.statuses[msg.service] = []);
    if (!list.some((e) => e.status === msg.entry.status && Math.abs(e.at - msg.entry.at) < 0.01)) list.push(msg.entry);
    list.sort((a, b) => a.at - b.at);
    if (msg.allowed) card.allowed = msg.allowed;
    if (msg.order_no !== undefined) card.order_no = msg.order_no;
    render();
  }

  function onCall(msg) {
    const card = S.cards.get(msg.card);
    if (card) (card.calls = card.calls || []).push({ at: msg.at, text: msg.text, from: msg.from });
    const call = { card: msg.card, at: msg.at, text: msg.text, from: msg.from, id: ++callSeq };
    S.calls.push(call);
    S.toasts.push(call);
    if (S.toasts.length > 3) S.toasts.shift();
    beep(660, 0.14);
    beep(660, 0.14, 0.22);
    if (msg.audio) setTimeout(() => Voice.play(msg.audio), 450); // доклад голосом после сигнала вызова
    renderCalls();
    render();
  }

  function sendStatus(number, status, orderNo, comment) {
    const msg = { t: 'set_status', card: number, status, order_no: orderNo, comment, seq: ++S.seq };
    S.pending.set(msg.seq, msg);
    savePending();
    if (!sock || !sock.send(msg)) toast('Нет связи с сервером: статус сохранён и уйдёт, когда связь восстановится', false);
  }

  // ---------- маршрутизация экранов ----------
  function route() {
    if (!S.lessonId) return showLobby();
    const m = location.hash.match(/^#\/incident\/(\d+)/);
    if (m && S.cards.has(m[1])) return openCardView(m[1]);
    return showFeed();
  }
  window.addEventListener('hashchange', () => { if (S.lessonId) route(); });

  function render() {
    if (S.view === 'feed') renderFeed();
    else if (S.view === 'card') {
      const c = S.cards.get(S.open);
      if (c) { renderPlaques(c); renderReports(c); }
    }
    renderTrainer();
  }

  // ---------- лента ----------
  function lastEntry(c, service) {
    const list = c.statuses[service] || [];
    return list.length ? list[list.length - 1] : null;
  }

  function showFeed() {
    S.view = 'feed';
    S.open = null;
    S.histFor = null;
    closeForm();
    document.title = TITLE;
    show('feed');
    renderFeed();
    renderTrainer();
  }

  function servicesLine(c) {
    return c.plaques.map((p) => {
      const e = lastEntry(c, p.name);
      return p.name + (e ? ' – ' + Clock.hms(e.at) + ' ' + e.status : '');
    }).join(',  ');
  }

  function rowHtml(c) {
    const t = c.saved_at;
    const own = lastEntry(c, c.own_service);
    const fresh = own && own.status === 'Добавлена';
    const open = S.expanded.has(c.number);
    return '<div class="rowg" data-card="' + esc(c.number) + '">' +
      '<div class="grid row">' +
      '<span class="c-chev" data-chev><svg><use href="#i-chev-' + (open ? 'up' : 'down') + '"/></svg></span>' +
      '<span></span>' +
      '<span class="c-mark"><svg><use href="#i-bookmark"/></svg></span>' +
      '<span><svg><use href="#i-pin"/></svg></span>' +
      '<span><svg><use href="#i-timer"/></svg></span>' +
      '<span class="c-opr">' + esc(c.op_no) + '</span>' +
      '<span>' + esc(c.arm_no) + '</span>' +
      '<span>' + esc(c.number) + '</span>' +
      '<span>' + Clock.dmyShort(t) + '</span>' +
      '<span class="c-time">' + Clock.hm(t) + ':<sup>' + Clock.ss(t) + '</sup></span>' +
      '<span class="c-type">' + esc(c.list_type) + '<span class="tip">' + esc(c.list_type) + '</span></span>' +
      '<span>' + (c.victims === 'есть' ? 'Да' : 'Нет') + '</span>' +
      '<span class="c-addr"><span>' + esc(c.address_short) + '</span><svg><use href="#i-bell-off"/></svg></span>' +
      '<span class="c-status' + (fresh ? ' fresh' : '') + '">' + esc(own ? own.status : '') + '</span>' +
      '<span><svg><use href="#i-clip"/></svg></span>' +
      '</div>' +
      '<div class="sub"><span class="k">Описание:</span><span class="meta">' + Clock.dmy(t) + ' ' + Clock.hms(t) + ' ' +
      esc(c.author) + ' -</span><span class="txt">' + esc(c.description) + '</span></div>' +
      (open ? '<div class="sub"><span class="k">Службы:</span><span class="txt">' + esc(servicesLine(c)) + '</span></div>' : '') +
      '</div>';
  }

  function renderFeed() {
    if (S.view !== 'feed') return;
    const q = S.query.trim().toLowerCase();
    let cards = [...S.cards.values()].sort((a, b) => b.saved_at - a.saved_at);
    if (q) {
      cards = cards.filter((c) => [c.number, c.address_short, c.list_type, c.card_title, c.description, c.class]
        .join(' ').toLowerCase().includes(q));
    }
    const total = cards.length;
    const pages = Math.max(1, Math.ceil(total / S.perPage));
    S.page = Math.min(S.page, pages - 1);
    const from = S.page * S.perPage;
    const shown = cards.slice(from, from + S.perPage);
    const rows = shown.map(rowHtml).join('');
    if (rows !== rowsCache) { rowsCache = rows; $('rows').innerHTML = rows; } // строки не пересоздаются под курсором

    const pageOpts = Array.from({ length: pages }, (_, i) =>
      '<option value="' + i + '"' + (i === S.page ? ' selected' : '') + '>' + (i + 1) + '</option>').join('');
    const perOpts = [10, 20, 50].map((n) =>
      '<option value="' + n + '"' + (n === S.perPage ? ' selected' : '') + '>' + n + '</option>').join('');
    $('pager').innerHTML = 'Страница: <select data-page>' + pageOpts + '</select>' +
      '<span>Записей на странице: <select data-per>' + perOpts + '</select></span>' +
      '<b>' + (total ? (from + 1) + '-' + (from + shown.length) : '0') + ' из ' + total + '</b>' +
      '<button type="button" data-prev' + (S.page === 0 ? ' disabled' : '') + '>&#10094;</button>' +
      '<button type="button" data-next' + (S.page >= pages - 1 ? ' disabled' : '') + '>&#10095;</button>';

    const n = $('notif-n');
    n.hidden = !S.notif;
    n.textContent = S.notif;

    const L = S.lesson;
    let wait = '';
    if (!L) wait = 'Подключение к занятию…';
    else if (!L.role) wait = 'Вы в занятии. Преподаватель ещё не назначил вам роль.';
    else if (L.state === 'набор') wait = 'Роль: ' + L.role_title + '. Ожидание начала занятия — карточки начнут приходить после старта.';
    else if (L.state === 'идёт' && !S.cards.size) wait = 'Занятие идёт. Карточки происшествий появятся в этом списке.';
    else if ((L.state === 'отчёт' || L.state === 'завершено')) wait = 'Занятие завершено. Итог — кнопка «Итог занятия» в верхней полосе.';
    $('feed-wait').hidden = !wait;
    $('feed-wait').textContent = wait;
  }

  $('rows').addEventListener('click', (ev) => {
    const g = ev.target.closest('[data-card]');
    if (!g) return;
    const number = g.dataset.card;
    if (ev.target.closest('[data-chev]')) {
      if (S.expanded.has(number)) S.expanded.delete(number); else S.expanded.add(number);
      renderFeed();
      return;
    }
    location.hash = '#/incident/' + number;
  });

  $('pager').addEventListener('change', (ev) => {
    if (ev.target.matches('[data-page]')) S.page = Number(ev.target.value);
    if (ev.target.matches('[data-per]')) { S.perPage = Number(ev.target.value); S.page = 0; }
    renderFeed();
  });
  $('pager').addEventListener('click', (ev) => {
    if (ev.target.closest('[data-prev]')) S.page = Math.max(0, S.page - 1);
    else if (ev.target.closest('[data-next]')) S.page += 1;
    else return;
    renderFeed();
  });
  $('search').addEventListener('input', (ev) => { S.query = ev.target.value; S.page = 0; renderFeed(); });
  $('search-reset').addEventListener('click', () => { $('search').value = ''; S.query = ''; S.page = 0; renderFeed(); });
  $('notif').addEventListener('click', () => { S.notif = 0; renderFeed(); });

  // ---------- карточка ----------
  function openCardView(number) {
    const c = S.cards.get(number);
    const first = S.open !== number;
    S.view = 'card';
    S.open = number;
    if (first) {
      S.histFor = null;
      closeForm();
      if (sock) sock.send({ t: 'open_card', card: number }); // «Получена службой» — при открытии (памятка)
    }
    document.title = 'Происшествие ' + number;
    show('card');
    renderCard(c);
    renderTrainer();
  }

  function telIco() {
    return '<div class="tel-ico"><svg><use href="#i-phone"/></svg><svg><use href="#i-sms"/></svg></div>';
  }

  function renderCard(c) {
    const t = c.saved_at;
    const caller = c.caller || {};
    $('card').innerHTML =
      '<div class="tel">' +
        '<div class="tel-hang"><svg><use href="#i-hangup"/></svg></div>' +
        '<div class="tel-state"><div class="st">Отключение</div><div class="tel-btns">' +
          '<button type="button" disabled>записи звонков</button><button type="button">список SMS</button></div></div>' +
        telIco() +
        '<div class="tel-num"><label>АОН</label><div class="val">' + esc(formatPhone(caller.phone)) + '</div></div>' +
        telIco() +
        '<div class="tel-num"><label>предоставленный</label><div class="val"></div></div>' +
        telIco() +
        '<div class="tel-num"><label>телефон на место</label><div class="val"></div></div>' +
        '<div class="tel-id"><b>Происшествие ' + esc(c.number) + '</b><div>Сохр. ' + Clock.dmy(t) + ' в ' + Clock.hms(t) +
          '</div><div>' + esc(c.operator) + '</div></div>' +
        '<div class="tel-act"><button type="button" class="b-view">просмотр</button><button type="button" class="b-add">дополнение</button></div>' +
      '</div>' +
      '<div class="cv-main">' +
        '<div class="cv-left">' +
          '<div class="blk f-caller">' + (caller.name
            ? '<b>' + esc(caller.name) + '</b><span class="muted">' + esc(caller.status || '') + '</span>'
            : '<span class="lbl">ФИО заявителя</span>') + '</div>' +
          '<div class="blk f-addr"><b>' + esc(c.address) + '</b><div>' + esc(c.okrug_title) + '</div><svg><use href="#i-map-off"/></svg></div>' +
          '<div class="blk f-desc"><div class="head">' + Clock.dmy(t) + ' ' + Clock.hms(t) + '<span>' + esc(c.author) + '</span></div>' +
            '<p>' + esc(c.description) + '</p></div>' +
        '</div>' +
        '<div class="cv-right">' +
          '<div class="f-flags">' +
            '<div class="blk flags-text"><span>Пострадавшие: ' + esc(c.victims) + '</span><span>Отказ от скорой: нет</span>' +
              '<span>Заблокированные: ' + esc(c.blocked) + '</span></div>' +
            '<div class="blk flags-btns"><span class="b-chs">ЧС <svg><use href="#i-bolt"/></svg></span>' +
              '<span class="b-chp' + (c.chp ? '' : ' off') + '">ЧП <svg><use href="#i-warn"/></svg></span>' +
              '<span class="b-edit"><svg><use href="#i-pencil"/></svg></span></div>' +
          '</div>' +
          '<div class="f-group"><div class="grp-bar"><span>' + esc(c.card_title) + '</span></div>' +
            '<div class="blk grp-feat">' + esc(c.features) + '</div></div>' +
          '<div class="blk f-class">Класс.: <b>' + esc(c.class) + '</b> ;</div>' +
          '<div class="blk f-vis">[ВИС] Класс.:</div>' +
          '<div class="trn" id="trn"></div>' +
        '</div>' +
      '</div>' +
      '<div class="svc">' +
        '<span class="svc-label">Службы:</span>' +
        '<div class="svc-plaques" id="plaques"></div>' +
        '<button type="button" class="svc-expand" id="svc-expand" title="Развернуть / свернуть"><svg><use href="#i-expand"/></svg></button>' +
        '<div class="svc-right"><button type="button" class="svc-sq" title="Сообщение"><svg><use href="#i-msg-alert"/></svg></button>' +
          '<button type="button" class="svc-sq" id="card-close" title="Закрыть карточку (Esc)"><svg><use href="#i-close"/></svg></button></div>' +
      '</div>';
    renderPlaques(c);
    renderReports(c);
  }

  function histRows(c) {
    return (c.statuses[S.histFor] || []).map((e) =>
      '<div class="hist-row"><span>оп. 0</span><span class="ar">&#8250;</span><span>' +
      Clock.dmy(e.at) + ' ' + Clock.hms(e.at) + ' ' + esc(e.status) + '</span><span class="ar">' +
      (e.comment ? '&#8250;' : '') + '</span><span class="cm">' + esc(e.comment || '') + '</span></div>').join('');
  }

  // Полоса служб строится один раз на карточку, дальше меняются только статусы и раскрытие:
  // боты обновляют статусы каждые несколько секунд, и пересоздание плашек съедало бы клики.
  let histCache = '';
  function renderPlaques(c) {
    const box = $('plaques');
    if (!box || !c) return;
    const key = c.number + '|' + c.plaques.map((p) => p.name).join('|');
    if (box.dataset.key !== key) {
      box.dataset.key = key;
      histCache = '';
      box.innerHTML = c.plaques.map((p) =>
        '<div class="plq' + (p.main ? ' main' : '') + (p.manual ? ' manual' : '') + '" data-plq="' + esc(p.name) +
        '" title="' + esc(p.name) + '"><svg class="chev"><use href="#i-chev-up"/></svg>' +
        (p.name === c.own_service
          ? '<button type="button" class="pen" data-pen title="Проставить статус"><svg><use href="#i-pencil"/></svg></button>'
          : '') +
        '<div class="nm">' + esc(p.name) + '</div><div class="st"></div></div>').join('') +
        '<div class="hist" id="hist" hidden></div>';
    }
    for (const el of box.querySelectorAll('[data-plq]')) {
      const open = S.histFor === el.dataset.plq;
      el.classList.toggle('open', open);
      el.querySelector('.chev use').setAttribute('href', '#i-chev-' + (open ? 'down' : 'up'));
      const e = lastEntry(c, el.dataset.plq);
      const text = e ? Clock.hm(e.at) + ' ' + e.status : '';
      const st = el.querySelector('.st');
      if (st.textContent !== text) st.textContent = text;
    }
    const hist = $('hist');
    if (!S.histFor) {
      hist.hidden = true;
      histCache = '';
      return;
    }
    const html = histRows(c);
    if (html !== histCache) {
      histCache = html;
      hist.innerHTML = html;
      hist.hidden = false;
      hist.scrollTop = hist.scrollHeight; // на стенде видны последние статусы (sdds_20)
    }
    hist.hidden = false;
    const plq = [...box.querySelectorAll('[data-plq]')].find((el) => el.dataset.plq === S.histFor);
    hist.style.left = (plq ? plq.offsetLeft - 1 : 0) + 'px';
  }

  function renderReports(c) {
    const box = $('trn');
    if (!box || !c) return;
    const calls = c.calls || [];
    box.innerHTML = '<h4>Тренажёр · доклады старшего бригады по карточке</h4>' + (calls.length
      ? calls.map((k) => '<div class="trn-row"><time>' + Clock.hms(k.at) + '</time><span>' + esc(k.text) + '</span></div>').join('')
      : '<div class="trn-empty">Докладов по этой карточке не было.</div>');
  }

  $('card').addEventListener('click', (ev) => {
    const c = S.cards.get(S.open);
    if (!c) return;
    if (ev.target.closest('#card-close')) { location.hash = '#/'; return; }
    if (ev.target.closest('[data-pen]')) { openForm(); return; }
    if (ev.target.closest('#svc-expand')) {
      S.histFor = S.histFor ? null : c.own_service;
      renderPlaques(c);
      return;
    }
    const plq = ev.target.closest('[data-plq]');
    if (plq) {
      S.histFor = S.histFor === plq.dataset.plq ? null : plq.dataset.plq;
      renderPlaques(c);
    }
  });

  // ---------- форма статуса ----------
  function openForm() {
    const c = S.cards.get(S.open);
    if (!c) return;
    if (S.lesson && S.lesson.state !== 'идёт') { toast('Занятие не идёт — статусы не принимаются', false); return; }
    S.form = { number: c.number, status: null, hot: -1, options: [] };
    $('sel-val').textContent = 'Статус';
    $('sel').classList.remove('bad');
    $('sf-order').value = c.order_no || ''; // номер наряда сохраняется между шагами (sdds_14)
    $('sf-comment').value = '';
    $('dim').hidden = false;
    $('sform').hidden = false;
    closeList();
    $('sel').focus();
  }

  function closeForm() {
    S.form = null;
    $('dim').hidden = true;
    $('sform').hidden = true;
    closeList();
  }

  function paintList() {
    const opts = S.form.options;
    $('sel-list').innerHTML = opts.length
      ? opts.map((s, i) => '<div class="sel-opt' + (i === S.form.hot ? ' hot' : '') + '" data-opt="' + i + '">' + esc(s) + '</div>').join('')
      : '<div class="sel-none">нет доступных статусов</div>';
  }

  function openList() {
    const c = S.cards.get(S.form.number);
    S.form.options = (c && c.allowed) || [];
    S.form.hot = Math.max(0, S.form.options.indexOf(S.form.status));
    paintList();
    $('sel-list').hidden = false;
  }

  function closeList() { $('sel-list').hidden = true; }

  function choose(i) {
    S.form.status = S.form.options[i];
    $('sel-val').textContent = S.form.status;
    $('sel').classList.remove('bad');
    closeList();
    $('sf-comment').focus();
  }

  function submitForm() {
    if (!S.form) return;
    if (!S.form.status) {
      $('sel').classList.add('bad');
      $('sel').focus();
      openList();
      return;
    }
    // Комментарий не проверяем: в боевом АРМ его можно не вписать — это нарушение №4 памятки,
    // и тренажёр должен дать его совершить, чтобы оценка его поймала.
    sendStatus(S.form.number, S.form.status, $('sf-order').value.trim(), $('sf-comment').value.trim());
    closeForm();
  }

  $('sel').addEventListener('click', (ev) => {
    const opt = ev.target.closest('[data-opt]');
    if (opt) { choose(Number(opt.dataset.opt)); return; }
    if ($('sel-list').hidden) openList(); else closeList();
  });
  $('sel').addEventListener('keydown', (ev) => {
    const listOpen = !$('sel-list').hidden;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!listOpen) { openList(); return; }
      const n = S.form.options.length;
      if (!n) return;
      S.form.hot = (S.form.hot + (ev.key === 'ArrowDown' ? 1 : -1) + n) % n;
      paintList();
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      ev.stopPropagation();
      if (!listOpen) { openList(); return; }
      if (S.form.options.length) choose(S.form.hot);
    }
  });
  document.addEventListener('click', (ev) => {
    if (S.form && !ev.target.closest('#sel')) closeList();
  });
  $('sf-ok').addEventListener('click', submitForm);
  $('sf-cancel').addEventListener('click', closeForm);

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      if (!$('modal').hidden) { closeModal(); return; }
      if (S.form) {
        if (!$('sel-list').hidden) closeList(); else closeForm();
        return;
      }
      if (S.view === 'card' && S.histFor) { S.histFor = null; renderPlaques(S.cards.get(S.open)); return; }
      if (S.view === 'card') location.hash = '#/';
      return;
    }
    if (ev.key === 'Enter' && S.form && !ev.target.closest('#sf-cancel')) {
      ev.preventDefault();
      submitForm();
    }
  });

  // ---------- полоса тренажёра, часы ----------
  const CONN_TEXT = { online: 'на связи', reconnecting: 'связь восстанавливается…', offline: 'нет связи', denied: 'нет доступа' };

  function renderTrainer() {
    const L = S.lesson;
    $('tr-user').textContent = me.name;
    if (!S.lessonId) {
      $('tr-lesson').textContent = 'Выбор занятия';
      $('tr-role').textContent = '';
    } else if (!L) {
      $('tr-lesson').textContent = 'Подключение к занятию ' + S.lessonId + '…';
      $('tr-role').textContent = '';
    } else {
      $('tr-lesson').innerHTML = 'Занятие <b>' + esc(L.id) + '</b> · ' + esc(L.title);
      $('tr-role').innerHTML = L.role ? 'роль: <b>' + esc(L.role_title) + '</b>' : 'роль не назначена';
    }
    const conn = $('tr-conn');
    conn.hidden = !S.lessonId;
    conn.dataset.state = S.conn;
    conn.textContent = CONN_TEXT[S.conn] || S.conn;
    $('tr-ref').hidden = !(L && L.reference && L.reference.sections);
    $('tr-calls').hidden = !L;
    $('tr-calls-n').textContent = S.calls.length;
    $('tr-result').hidden = !S.result;
    $('tr-finish').hidden = !(L && L.solo && L.state === 'идёт');
    tick();
  }

  function tick() {
    const now = Clock.now();
    $('clock-date').textContent = Clock.longDate(now);
    $('clock-hm').textContent = Clock.hm(now);
    $('clock-ss').textContent = ':' + Clock.ss(now);
    const L = S.lesson;
    let txt = '';
    if (L && L.state === 'набор') txt = 'ожидание начала';
    else if (L && L.started_at) {
      const end = L.finished_at || now;
      txt = (L.state === 'идёт' ? 'идёт ' : 'длилось ') + Clock.span(end - L.started_at);
      if (L.time_scale && L.time_scale < 1) txt += ' · ускорение ×' + Math.round(1 / L.time_scale);
      else if (L.time_scale && L.time_scale > 1) txt += ' · замедление ×' + L.time_scale;
    }
    $('tr-time').textContent = txt;
  }
  setInterval(tick, 250);

  // ---------- вызовы бригад ----------
  function renderCalls() {
    $('calls').innerHTML = S.toasts.map((k) => {
      const c = S.cards.get(k.card);
      const where = c ? c.address_short.split(' , ').slice(2).join(', ') : '';
      return '<div class="call"><div class="call-head"><svg><use href="#i-phone"/></svg>Входящий вызов · ' + esc(k.from) +
        '<time>' + Clock.hms(k.at) + '</time></div>' +
        '<div class="call-card">Карточка ' + esc(k.card) + (where ? ' · ' + esc(where) : '') + '</div>' +
        '<div class="call-text">«' + esc(k.text) + '»</div>' +
        '<div class="call-act"><button type="button" data-goto="' + esc(k.card) + '" data-id="' + k.id + '">Открыть карточку</button>' +
        '<button type="button" data-dismiss="' + k.id + '">Понятно</button></div></div>';
    }).join('');
  }

  function dropToast(id) {
    S.toasts = S.toasts.filter((k) => k.id !== id);
    renderCalls();
  }

  $('calls').addEventListener('click', (ev) => {
    const go = ev.target.closest('[data-goto]');
    if (go) {
      dropToast(Number(go.dataset.id));
      location.hash = '#/incident/' + go.dataset.goto;
      return;
    }
    const d = ev.target.closest('[data-dismiss]');
    if (d) dropToast(Number(d.dataset.dismiss));
  });

  // ---------- окна ----------
  const FOOT = '<div class="modal-foot"><button type="button" class="btn" data-close>Закрыть</button></div>';

  function showModal(html) {
    $('modal-box').innerHTML = html;
    $('modal').hidden = false;
  }

  function closeModal() { $('modal').hidden = true; }

  $('modal').addEventListener('click', (ev) => {
    if (ev.target === $('modal') || ev.target.closest('[data-close]')) { closeModal(); return; }
    const go = ev.target.closest('[data-goto]');
    if (go) {
      closeModal();
      location.hash = '#/incident/' + go.dataset.goto;
    }
  });

  function showReference() {
    const ref = S.lesson && S.lesson.reference;
    if (!ref) return;
    showModal('<h2>' + esc(ref.title || 'Справка службы') + '</h2>' + (ref.sections || []).map((s) =>
      '<div class="ref-sec"><h3>' + esc(s.title) + '</h3><ul>' + s.items.map((i) => '<li>' + esc(i) + '</li>').join('') +
      '</ul></div>').join('') + FOOT);
  }

  function showCallLog() {
    const rows = S.calls.slice().reverse().map((k) =>
      '<tr><td>' + Clock.hms(k.at) + '</td><td><a data-goto="' + esc(k.card) + '">' + esc(k.card) + '</a></td><td>' +
      esc(k.text) + '</td></tr>').join('');
    showModal('<h2>Вызовы старших бригад</h2><p class="sub-t">Доклады о ходе работ по вашим карточкам, последние сверху.</p>' +
      (rows ? '<table class="call-log">' + rows + '</table>' : '<p>Вызовов пока не было.</p>') + FOOT);
  }

  function showResult() {
    const r = S.result;
    if (!r) return;
    S.resultShown = true;
    const count = {};
    for (const f of r.findings) count[f.category] = (count[f.category] || 0) + 1;
    const rows = (r.categories || []).map((c) =>
      '<tr class="' + (count[c.category] ? 'hit' : '') + '"><td>' + c.category + '</td><td>' + esc(c.title) +
      '</td><td class="n">' + (count[c.category] || '—') + '</td><td class="n">' +
      (count[c.category] ? '−' + count[c.category] * c.weight : '') + '</td></tr>').join('');
    const finds = r.findings.map((f) =>
      '<div class="fnd"><b>' + (f.card ? 'Карточка ' + esc(f.card) + ' · ' : '') + '№' + f.category + '. ' + esc(f.title) +
      '</b><div class="d">' + esc(f.detail) + '</div></div>').join('') ||
      '<p>Замечаний нет: все карточки обработаны по памятке.</p>';
    showModal('<h2>Итог занятия</h2><p class="sub-t">' + esc(S.lesson ? S.lesson.title : '') + ' · ' + esc(me.name) +
      ' · карточек: ' + r.cards + '</p>' +
      '<div class="score' + (r.score < 70 ? ' low' : '') + '"><b>' + r.score + '</b><span>баллов из 100</span></div>' +
      '<h3>Нарушения по категориям памятки</h3><table class="cat-table"><tr><th>№</th><th>Категория</th>' +
      '<th class="n">Нарушений</th><th class="n">Баллы</th></tr>' + rows + '</table>' +
      '<h3>Замечания</h3>' + finds + FOOT);
  }

  // ---------- уведомления об ошибках ----------
  let toastTimer = null;
  function toast(text, ok) {
    const t = $('toast');
    t.textContent = text;
    t.className = 'toast' + (ok ? ' ok' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
  }

  async function logout(reason) {
    if (sock) sock.close();
    setS(LESSON_KEY, null);
    try { if (reason !== 'expired') await API.call('POST', '/api/logout'); } catch (e) { /* уже не действует */ }
    API.clear();
    location.replace(reason === 'expired' ? 'index.html#expired' : 'index.html');
  }

  function confirmExit() {
    const running = S.lesson && S.lesson.state === 'идёт';
    if (!running || confirm('Выйти из тренажёра? Во время занятия это засчитывается как потеря связи с рабочим местом.')) logout();
  }

  Voice.bindButton($('tr-sound'));
  $('tr-exit').addEventListener('click', confirmExit);
  $('tr-cab').addEventListener('click', () => {
    const running = S.lesson && S.lesson.state === 'идёт';
    if (running && !confirm('Перейти в кабинет? Занятие продолжится — вернуться можно из кабинета.')) return;
    location.assign('cabinet.html');
  });
  $('tr-finish').addEventListener('click', async () => {  // самостоятельное задание ДДС завершает сам обучающийся
    if (!confirm('Завершить задание? Итог посчитается по всем пришедшим карточкам.')) return;
    try {
      await API.call('POST', '/api/lessons/' + encodeURIComponent(S.lessonId) + '/finish');
    } catch (e) { toast(e.message, false); }
  });
  $('clock-exit').addEventListener('click', confirmExit);
  $('tr-ref').addEventListener('click', showReference);
  $('clock-help').addEventListener('click', showReference);
  $('tr-calls').addEventListener('click', showCallLog);
  $('tr-result').addEventListener('click', showResult);

  // ---------- старт ----------
  $('clock-id').textContent = ', ' + me.name;
  const saved = getS(LESSON_KEY);
  if (saved) enterLesson(saved); else showLobby();
  tick();
})();
