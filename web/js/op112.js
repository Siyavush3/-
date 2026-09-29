// Карточка оператора Службы 112 (режим «карточки»): вызов → разговор → карточка → сохранение → разбор.
// Опросная карта строится из классификатора (сервер: /api/classifier/*), службы — по ЕКП (/api/route).
// Черновик карточки сохраняется во вкладке и переживает перезагрузку страницы (ТЗ: промежуточные результаты).
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  if (me.role !== 'student') { location.replace(me.role === 'admin' ? 'admin.html' : 'teacher.html'); return; }

  const LESSON_KEY = 'a112.lesson.' + me.id;
  const lessonId = (() => { try { return sessionStorage.getItem(LESSON_KEY); } catch (e) { return null; } })();
  if (!lessonId) { location.replace('arm.html'); return; }

  const LEVELS = ['attr1', 'attr2', 'attr3'];
  const LEVEL_TITLES = ['Что и где', 'Уточнение', 'Подробнее'];
  const FREQUENT = ['101', '102', '103', '104', 'ДТП', 'Человек в опасности', 'Аварии и происшествия в городском хозяйстве',
    'Ошибочно набран номер', 'Отмена вызова', 'Консультация'];
  const ADDR = ['subject', 'locality', 'street', 'house', 'korpus', 'stroenie', 'flat', 'entrance', 'floor', 'code', 'descriptive'];

  const S = {
    lesson: null, desk: null, result: null, resultShown: false, conn: 'offline',
    seq: 0, pending: null,
    call: null,          // текущий вызов: {number, phone, rang_at, answered_at, transcript}
    callType: null, q: null, path: [], answers: {}, code: null,
    flags: new Set(), victims: null,
    auto: [], added: new Set(), removed: new Set(),
    callTypes: [], services: null, groups: {}, saving: false,
  };
  let sock = null;

  function getS(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function setS(k, v) { try { if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) { /* без хранения */ } }

  // ---------- звук ----------
  let audio = null;
  document.addEventListener('pointerdown', () => {
    try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); if (audio.state === 'suspended') audio.resume(); } catch (e) { audio = null; }
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
      o.connect(g); g.connect(audio.destination);
      o.start(t0); o.stop(t0 + dur);
    } catch (e) { /* без звука */ }
  }
  function ringTone() { beep(740, 0.35); beep(740, 0.35, 0.5); }

  // ---------- связь ----------
  function connect() {
    sock = API.socket(lessonId, {
      message: onMessage,
      status: (st, code) => {
        S.conn = st;
        if (st === 'denied') {
          setS(LESSON_KEY, null);
          if (code === 4401) { API.clear(); location.replace('index.html#expired'); return; }
          location.replace('arm.html');
          return;
        }
        renderTrainer();
      },
    });
  }

  function send(msg) { return sock && sock.send(msg); }

  function onMessage(msg) {
    switch (msg.t) {
      case 'state': return onState(msg);
      case 'incoming': return onIncoming(msg.call);
      case 'caller_say': return onSay(msg);
      case 'card_result': return onCardResult(msg.result);
      case 'save_ack':
        if (S.pending && S.pending.seq === msg.seq) S.pending = null;
        return undefined;
      case 'save_rejected':
        S.pending = null; S.saving = false; $('save').disabled = false;
        toast('Карточка не сохранена: ' + msg.reason);
        return undefined;
      case 'queue_done':
        if (S.desk) S.desk.remaining = 0;
        if (S.lesson && S.lesson.solo) {  // самостоятельное задание: вызовы кончились — итог сразу
          showWait('Вызовов больше нет. Подводим итог задания…');
          finishSolo(false);
          return undefined;
        }
        showWait('Вызовов больше нет. Дождитесь, пока преподаватель завершит занятие, — итог появится здесь.');
        return undefined;
      case 'result':
        if (Mic.active) { Mic.stop(); micPaint(); }  // занятие окончено — микрофон больше не нужен
        S.result = msg.result;
        if (S.lesson) S.lesson.state = 'отчёт';
        renderTrainer();
        showFinal();
        return undefined;
      case 'error':
        toast(msg.reason || 'ошибка');
        return undefined;
      default:
        return undefined;
    }
  }

  function onState(msg) {
    Clock.sync(msg.server_time);
    S.lesson = msg.lesson;
    if (S.lesson.mode !== 'cards') { location.replace('arm.html'); return; }
    S.desk = msg.desk;
    S.result = msg.result;
    S.seq = Math.max(S.seq, msg.last_seq || 0);
    if (S.pending && S.pending.seq > (msg.last_seq || 0)) send(S.pending); else S.pending = null;
    renderTrainer();
    if (S.result) { showWait('Занятие завершено.'); if (!S.resultShown) showFinal(); return; }
    if (!S.lesson.role) { showWait('Вы в занятии. Преподаватель ещё не назначил роль.'); return; }
    if (S.lesson.state === 'набор') { showWait('Роль: оператор Службы 112. Ожидание начала занятия — вызовы начнут поступать после старта.'); return; }
    if (!S.desk) { showWait('Подключение к занятию…'); return; }
    if (S.desk.current) {
      const same = S.call && S.call.number === S.desk.current.number;
      S.call = S.desk.current;
      if (!same) startCard(S.call, true);
      renderCall();
    } else if (!S.desk.remaining) {
      showWait('Вызовов больше нет. Дождитесь, пока преподаватель завершит занятие.');
    } else {
      showCardIdle();
    }
  }

  // ---------- вызов ----------
  function onIncoming(call) {
    S.call = call;
    if (S.desk) S.desk.current = call;
    startCard(call, false);
    renderCall();
    Voice.ring(true, ringTone); // звонок по кругу, пока вызов не принят
    renderTrainer();
  }

  function onSay(msg) {
    if (!S.call || S.call.number !== msg.call) return;
    S.call.transcript = S.call.transcript || [];
    S.call.transcript.push(msg.line);
    if (!S.call.answered_at) S.call.answered_at = msg.line.at;
    Voice.ring(false);
    if (msg.line.who === 'заявитель') Voice.play(msg.line.audio); // голос заявителя, если реплика озвучена
    renderCall();
  }

  function answer() {
    if (!S.call || S.call.answered_at) return;
    Voice.ring(false);
    send({ t: 'answer' });
  }

  function ask(intent, text, via) {
    if (!S.call || !S.call.answered_at) return;
    const line = { who: 'оператор', text: text || intent, at: Clock.now() };
    const msg = intent ? { t: 'ask', intent, text } : { t: 'ask', text };
    if (via) { line.via = via; msg.via = via; }
    S.call.transcript.push(line);
    send(msg);
    renderCall();
  }

  // ---------- голос оператора (узел 9) ----------
  // Микрофон включается кнопкой «Микрофон» или Alt+M; фраза распознаётся на сервере после паузы и уходит
  // заявителю тем же вопросом, что и кнопка. Пока говорит заявитель или нет разговора, звук не передаётся.
  const MIC_KEY = 'a112.mic';
  let micWanted = (() => { try { return localStorage.getItem(MIC_KEY) === 'on'; } catch (e) { return false; } })();

  function micHeld() { return !S.call || !S.call.answered_at || Voice.busy(); }

  function micNote(text, warn) {
    $('mic-note').textContent = text || '';
    $('mic-note').classList.toggle('warn', !!warn);
  }

  function micPaint() {
    $('mic').textContent = micWanted ? 'Микрофон: вкл' : 'Микрофон: выкл';
    $('mic').classList.toggle('on', Mic.active);
    $('mic-level').hidden = !Mic.active;
  }

  function intentTitle(intent) {
    const b = document.querySelector('#asks [data-intent="' + intent + '"]');
    return b ? b.textContent : intent;
  }

  function onHeard(text, intent) {
    if (!S.call || !S.call.answered_at) return;
    if (intent) {
      ask(intent, text, 'voice');
      micNote('«' + text + '» → ' + intentTitle(intent));
      return;
    }
    S.call.transcript.push({ who: 'оператор', text, at: Clock.now(), via: 'voice' });
    send({ t: 'say', text });  // реплика без вопроса: заявитель её «слышит», но не отвечает
    renderCall();
    micNote('«' + text + '» — вопрос не распознан: спросите иначе или кнопкой', true);
  }

  async function micStart() {
    micNote('включаем микрофон…');
    try {
      await Mic.start(lessonId, {
        state: (st) => {
          if (st === 'ready') micNote('говорите — вопрос распознаётся после короткой паузы');
          else if (st === 'reconnect') micNote('нет связи с распознаванием, переподключаемся…', true);
          micPaint();
        },
        level: (v) => { $('mic-level').firstElementChild.style.width = Math.round(v * 100) + '%'; },
        partial: (text) => micNote('слышу: ' + text),
        final: onHeard,
        error: (reason) => { micSet(false); micNote(reason, true); },
      }, micHeld);
    } catch (e) {
      micSet(false);
      micNote(e && e.name === 'NotAllowedError' ? 'браузер не разрешил микрофон — разрешите доступ в адресной строке'
        : (e && e.message) || 'микрофон недоступен', true);
    }
    micPaint();
  }

  function micSet(on) {
    micWanted = on;
    try { localStorage.setItem(MIC_KEY, on ? 'on' : 'off'); } catch (e) { /* не запомним — не страшно */ }
    if (on && !Mic.active) micStart();
    if (!on && Mic.active) { Mic.stop(); micNote(''); }
    micPaint();
  }

  // ---------- карточка ----------
  function showWait(text) {
    $('wait-text').textContent = text;
    $('wait').hidden = false;
    $('card').hidden = true;
  }

  function showCardIdle() {
    Voice.ring(false);
    Voice.stop();
    $('wait').hidden = true;
    $('card').hidden = false;
    S.call = null;
    resetForm();
    $('line-state').textContent = 'Ожидание вызова';
    $('card-title').textContent = 'Происшествие —';
    $('card-saved').textContent = 'следующий вызов поступит через несколько секунд';
    renderCall();
  }

  function resetForm() {
    for (const k of ADDR) { const el = $('a-' + k); if (el) el.value = k === 'subject' ? 'Москва' : ''; }
    $('caller-name').value = '';
    $('caller-status').value = '';
    $('description').value = '';
    $('phone-given').value = '';
    $('phone-site').value = '';
    $('what-input').value = '';
    S.callType = null; S.q = null; S.path = []; S.answers = {}; S.code = null;
    S.flags = new Set(); S.victims = null; S.auto = []; S.added = new Set(); S.removed = new Set();
    S.saving = false;
    $('save').disabled = false;
    renderAll();
  }

  function startCard(call, restore) {
    $('wait').hidden = true;
    $('card').hidden = false;
    resetForm();
    $('card-title').textContent = 'Происшествие ' + call.number;
    $('card-saved').textContent = 'новая карточка';
    $('card-op').textContent = 'Опер. ' + me.name + ', АРМ тренажёра';
    if (restore) restoreDraft(call.number);
  }

  function draftKey() { return S.call ? 'a112.draft.' + me.id + '.' + S.call.number : null; }

  function saveDraft() {
    const key = draftKey();
    if (!key || S.saving) return;
    setS(key, JSON.stringify({ form: readForm(), callType: S.callType, group: S.q && S.q.group, path: S.path, answers: S.answers }));
  }

  async function restoreDraft(number) {
    let d = null;
    try { d = JSON.parse(getS('a112.draft.' + me.id + '.' + number) || 'null'); } catch (e) { d = null; }
    if (!d) return;
    const f = d.form;
    $('caller-name').value = f.caller.name || '';
    $('caller-status').value = f.caller.status || '';
    $('description').value = f.description || '';
    for (const k of ADDR) { const el = $('a-' + k); if (el && f.address[k] !== undefined) el.value = f.address[k]; }
    S.victims = f.victims;
    S.flags = new Set((f.flags || []).filter((x) => x === 'НД' || x === 'ПП_НЕ_НА_МЕСТЕ'));
    S.added = new Set(f.services_added || []);
    S.removed = new Set(f.services_removed || []);
    S.callType = d.callType;
    if (d.group) {
      await loadGroup(d.group, d.callType);
      S.path = d.path || [];
      S.answers = d.answers || {};
    }
    renderAll();
    await refreshServices();
  }

  function readForm() {
    const address = {};
    for (const k of ADDR) address[k] = ($('a-' + k).value || '').trim();
    const flags = new Set(S.flags);
    for (const [flag, v] of Object.entries(S.answers)) if (v === true) flags.add(flag);
    return {
      number: S.call ? S.call.number : null,
      call_type: S.callType, code: S.code, flags: [...flags], victims: S.victims,
      caller: { name: $('caller-name').value.trim(), status: $('caller-status').value },
      address, description: $('description').value,
      phones: { given: $('phone-given').value.trim(), site: $('phone-site').value.trim() },
      services_added: [...S.added].filter((x) => !S.auto.some((a) => a.name === x)),
      services_removed: [...S.removed].filter((x) => S.auto.some((a) => a.name === x)),
    };
  }

  function save(extra) {
    if (!S.call || !S.call.answered_at || S.saving) {
      if (S.call && !S.call.answered_at) toast('Сначала примите вызов');
      return;
    }
    const card = Object.assign(readForm(), extra || {});
    S.saving = true;
    $('save').disabled = true;
    S.pending = { t: 'save_card', card, seq: ++S.seq };
    if (!send(S.pending)) toast('Нет связи с сервером: карточка уйдёт, когда связь восстановится');
  }

  function onCardResult(result) {
    const key = draftKey();
    if (key) setS(key, null);
    if (S.desk) { S.desk.done = S.desk.done || []; S.desk.done.push(result); S.desk.current = null; }
    const feedback = S.desk ? S.desk.feedback : 'immediate';
    S.call = null;
    showCardIdle();
    renderTrainer();
    if (feedback === 'immediate') showCardResult(result);
    else toast('Карточка сохранена', true);
  }

  // ---------- опросная карта ----------
  async function loadGroup(group, callType) {
    if (!S.groups[group]) S.groups[group] = await API.call('GET', '/api/classifier/groups/' + encodeURIComponent(group));
    S.q = S.groups[group];
    S.callType = callType || S.q.title;
    S.path = [];
    S.answers = {};
    S.code = null;
  }

  function resolve() {
    if (!S.q) return null;
    let cands = S.q.incidents;
    S.path.forEach((v, i) => { cands = cands.filter((c) => c[LEVELS[i]] === v); });
    if (cands.length === 1) return cands[0];
    return cands.find((c) => LEVELS.every((k, i) => (i < S.path.length ? c[k] === S.path[i] : !c[k]))) || null;
  }

  function renderQ() {
    const box = $('qcard');
    if (!S.q) {
      box.hidden = true;
      $('what').hidden = false;
      renderChipsState();
      return;
    }
    box.hidden = false;
    $('what').hidden = true;
    $('qtab').textContent = S.callType || S.q.title;
    $('qtitle').textContent = S.q.title;
    let cands = S.q.incidents;
    const rows = [];
    for (let i = 0; i < LEVELS.length; i++) {
      const key = LEVELS[i];
      const opts = [...new Set(cands.map((c) => c[key]).filter(Boolean))];
      if (!opts.length) break;
      rows.push('<div class="qrow"><span>' + LEVEL_TITLES[i] + '</span><div class="qopts">' + opts.map((o) =>
        '<button type="button" class="qbtn' + (S.path[i] === o ? ' on' : '') + '" data-level="' + i + '" data-val="' + esc(o) + '">' +
        esc(o) + '</button>').join('') + '</div></div>');
      if (!S.path[i]) break;
      cands = cands.filter((c) => c[key] === S.path[i]);
    }
    for (const q of S.q.questions) {
      const v = S.answers[q.flag];
      const btns = [['да', true], ['нет', false]].concat(q.flag === 'ГАЗ' ? [['нет данных', null]] : []);
      rows.push('<div class="qrow"><span>' + esc(q.text) + '</span><div class="qopts">' + btns.map(([t, val]) =>
        '<button type="button" class="qbtn' + (v === val && q.flag in S.answers ? ' on' : '') + '" data-flag="' + q.flag +
        '" data-ans="' + (val === null ? 'null' : val) + '">' + t + '</button>').join('') + '</div></div>');
    }
    $('qbody').innerHTML = rows.join('');
    const r = resolve();
    S.code = r ? r.code : null;
    $('qresult').innerHTML = r ? 'Итоговый тип: <b>' + esc(r.type) + '</b> <span style="color:#777">(' + r.code + ')</span>'
      : 'Выберите признаки — список служб появится, когда тип будет определён.';
    renderChipsState();
  }

  $('qbody').addEventListener('click', async (ev) => {
    const lvl = ev.target.closest('[data-level]');
    const fl = ev.target.closest('[data-flag]');
    if (lvl) {
      const i = Number(lvl.dataset.level);
      S.path = S.path.slice(0, i);
      if (!(S.path[i] === lvl.dataset.val)) S.path[i] = lvl.dataset.val;
    } else if (fl) {
      const val = fl.dataset.ans === 'null' ? null : fl.dataset.ans === 'true';
      S.answers[fl.dataset.flag] = val;
    } else return;
    renderQ();
    saveDraft();
    await refreshServices();
  });

  $('qreset').addEventListener('click', async () => {
    S.q = null; S.callType = null; S.path = []; S.answers = {}; S.code = null;
    renderQ();
    await refreshServices();
    saveDraft();
    $('what-input').focus();
  });

  // ---------- «что случилось?» ----------
  async function loadCallTypes() {
    try { S.callTypes = await API.call('GET', '/api/classifier/call-types'); } catch (e) { S.callTypes = []; }
    renderChips();
  }

  function renderChips() {
    $('chips').innerHTML = FREQUENT.map((name) => {
      const t = S.callTypes.find((x) => x.name === name);
      const kind = t && t.kind !== 'происшествие' ? ' kind-service' : '';
      return '<button type="button" class="' + kind + '" data-type="' + esc(name) + '">' + esc(name) + '</button>';
    }).join('');
  }

  function renderChipsState() {
    const chosen = !!S.callType;
    for (const b of document.querySelectorAll('.alarm')) b.disabled = chosen;
  }

  async function chooseType(name) {
    const t = S.callTypes.find((x) => x.name === name);
    $('suggest').hidden = true;
    $('what-input').value = '';
    if (t && t.group) {
      await loadGroup(t.group, name);
    } else {
      S.q = null; S.callType = name; S.code = null; // служебный или нецелевой вызов — без опросной карты
      toast('Выбран служебный тип «' + name + '»: карточка без опросной карты', true);
    }
    renderAll();
    await refreshServices();
    saveDraft();
  }

  async function chooseIncident(inc) {
    $('suggest').hidden = true;
    $('what-input').value = '';
    await loadGroup(inc.group, inc.title);
    S.path = LEVELS.map((k) => inc[k]).filter(Boolean);
    renderAll();
    await refreshServices();
    saveDraft();
  }

  let suggestTimer = null;
  let suggestItems = [];
  $('what-input').addEventListener('input', () => {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(async () => {
      const q = $('what-input').value.trim().toLowerCase();
      if (q.length < 2) { $('suggest').hidden = true; return; }
      const types = S.callTypes.filter((t) => t.name.toLowerCase().includes(q)).slice(0, 8).map((t) => ({ kind: 'type', t }));
      let incs = [];
      try { incs = (await API.call('GET', '/api/classifier/search?q=' + encodeURIComponent(q))).map((i) => ({ kind: 'inc', i })); } catch (e) { incs = []; }
      suggestItems = types.concat(incs).slice(0, 30);
      $('suggest').innerHTML = suggestItems.map((x, n) => x.kind === 'type'
        ? '<div data-n="' + n + '"><b>' + esc(x.t.name) + '</b><small>' + esc(x.t.kind) + '</small></div>'
        : '<div data-n="' + n + '">' + esc(x.i.type) + '<small>' + esc(x.i.title) + '</small></div>').join('') ||
        '<div>ничего не найдено</div>';
      $('suggest').hidden = false;
    }, 200);
  });
  $('suggest').addEventListener('click', (ev) => {
    const el = ev.target.closest('[data-n]');
    if (!el) return;
    const x = suggestItems[Number(el.dataset.n)];
    if (x.kind === 'type') chooseType(x.t.name); else chooseIncident(x.i);
  });
  $('chips').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-type]');
    if (b) chooseType(b.dataset.type);
  });
  document.addEventListener('click', (ev) => { if (!ev.target.closest('.what-line')) $('suggest').hidden = true; });

  // ---------- службы ----------
  async function refreshServices() {
    if (!S.code) { S.auto = []; renderServices(); return; }
    const flags = readForm().flags;
    try {
      S.auto = await API.call('GET', '/api/route?code=' + encodeURIComponent(S.code) + '&flags=' + encodeURIComponent(flags.join(',')));
    } catch (e) { S.auto = []; }
    renderServices();
  }

  function renderServices() {
    const list = S.auto.filter((p) => !S.removed.has(p.name)).map((p) => ({ name: p.name, main: p.main, manual: false }))
      .concat([...S.added].filter((n) => !S.auto.some((p) => p.name === n)).map((n) => ({ name: n, main: false, manual: true })));
    $('svc-list').innerHTML = list.length ? list.map((p) =>
      '<div class="chip112' + (p.main ? ' main' : '') + (p.manual ? ' manual' : '') + '" title="' + esc(p.name) + '">' +
      '<svg class="ph"><use href="#i-phone"/></svg><svg class="rm" data-rm="' + esc(p.name) + '"><use href="#i-close"/></svg>' +
      '<div class="nm">' + esc(p.name) + '</div></div>').join('')
      : '<span class="svc-empty">службы подтянутся автоматически, когда тип происшествия будет определён</span>';
    const d = $('description').value;
    const to03 = list.some((p) => p.name === 'Служба 103');
    $('first100').textContent = to03 && d ? 'в службу 03 уйдёт: «' + d.slice(0, 100) + (d.length > 100 ? '…' : '') + '»' : '';
  }

  $('svc-list').addEventListener('click', (ev) => {
    const rm = ev.target.closest('[data-rm]');
    if (!rm) return;
    const name = rm.dataset.rm;
    if (S.added.has(name)) S.added.delete(name); else S.removed.add(name);
    renderServices();
    saveDraft();
  });

  $('svc-add').addEventListener('click', async () => {
    if (!S.services) { try { S.services = await API.call('GET', '/api/services'); } catch (e) { S.services = []; } }
    showModal('<h2>Добавьте службы</h2><input id="svc-q" class="svc-search" placeholder="Поиск …" style="width:100%;height:30px;margin-top:6px;padding:0 8px">' +
      '<div class="svc-pick" id="svc-pick"></div><div class="modal-foot"><button type="button" class="btn" data-close>Сохранить и закрыть</button></div>');
    const paint = () => {
      const q = ($('svc-q').value || '').toLowerCase();
      $('svc-pick').innerHTML = S.services.filter((s) => (s.short + ' ' + s.full).toLowerCase().includes(q)).slice(0, 80).map((s) =>
        '<div data-pick="' + esc(s.short) + '"><b>' + esc(s.short) + '</b> <small>' + esc(s.full === s.short ? '' : s.full) + '</small></div>').join('');
    };
    $('svc-q').addEventListener('input', paint);
    $('svc-pick').addEventListener('click', (ev) => {
      const p = ev.target.closest('[data-pick]');
      if (!p) return;
      const name = p.dataset.pick;
      if (S.removed.has(name)) S.removed.delete(name); else S.added.add(name);
      renderServices();
      saveDraft();
      toast('Добавлена служба: ' + name, true);
    });
    paint();
    $('svc-q').focus();
  });

  // ---------- поля ----------
  function renderAddressLine() {
    const v = (k) => ($('a-' + k).value || '').trim();
    const parts = [v('subject'), v('locality'), v('street'), v('house'),
      v('korpus') && 'к. ' + v('korpus'), v('stroenie') && 'стр. ' + v('stroenie'), v('flat') && 'кв. ' + v('flat'),
      v('entrance') && 'под. ' + v('entrance'), v('floor') && 'эт. ' + v('floor'), v('descriptive')].filter(Boolean);
    $('addr-line').textContent = parts.join(', ');
  }

  function renderFlags() {
    for (const b of document.querySelectorAll('#victims [data-victims]')) b.classList.toggle('on', S.victims === (b.dataset.victims === '1'));
    for (const b of document.querySelectorAll('.tgl')) b.classList.toggle('on', S.flags.has(b.dataset.flag));
  }

  function renderAll() {
    renderAddressLine();
    renderFlags();
    renderQ();
    renderServices();
    $('desc-count').textContent = $('description').value.length + ' / 1999';
  }

  $('victims').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-victims]');
    if (!b) return;
    S.victims = b.dataset.victims === '1';
    renderFlags(); saveDraft(); await refreshServices();
  });
  for (const b of document.querySelectorAll('.tgl')) {
    b.addEventListener('click', async () => {
      if (S.flags.has(b.dataset.flag)) S.flags.delete(b.dataset.flag); else S.flags.add(b.dataset.flag);
      renderFlags(); saveDraft(); await refreshServices();
    });
  }
  for (const b of document.querySelectorAll('.alarm')) {
    b.addEventListener('click', () => {
      if (!S.call || !S.call.answered_at) return;
      if (confirm('«' + b.dataset.noncontact + '»: карточка закроется без типа происшествия. Продолжить?')) {
        save({ call_type: b.dataset.noncontact, code: null });
      }
    });
  }
  document.querySelector('.cv112').addEventListener('input', (ev) => {
    if (ev.target.id === 'description') $('desc-count').textContent = ev.target.value.length + ' / 1999';
    if (ev.target.id && ev.target.id.startsWith('a-')) renderAddressLine();
    if (ev.target.id === 'description') renderServices();
    saveDraft();
  });
  $('addr-clear').addEventListener('click', () => {
    for (const k of ADDR) if (k !== 'subject') $('a-' + k).value = '';
    renderAddressLine(); saveDraft();
  });

  // ---------- разговор ----------
  function renderCall() {
    const c = S.call;
    const ringing = c && !c.answered_at;
    $('hang').classList.toggle('ring', !!ringing);
    $('answer').hidden = !ringing;
    $('line-state').textContent = !c ? 'Ожидание вызова' : ringing ? 'Входящий вызов' : 'Разговор';
    $('aon').textContent = c ? formatPhone(c.phone) : '+7 (   )    -  -';
    for (const b of document.querySelectorAll('#asks button, #ask-form button, #ask-text')) b.disabled = !c || ringing;
    $('save').disabled = !c || ringing || S.saving;
    const lines = (c && c.transcript) || [];
    $('transcript').innerHTML = lines.length ? lines.map((l) =>
      '<div class="line ' + (l.who === 'оператор' ? 'op' : 'cl') + (l.via === 'voice' ? ' voice' : '') + '"><span class="who">' + (l.who === 'оператор' ? 'Вы:' : 'Заявитель:') +
      '</span>' + esc(l.text) + '</div>').join('')
      : '<div class="line" style="color:#8a7f63">' + (ringing ? 'Звонит телефон — примите вызов (кнопка слева вверху или Enter).'
        : 'Здесь появится разговор с заявителем.') + '</div>';
    $('transcript').scrollTop = $('transcript').scrollHeight;
  }

  $('hang').addEventListener('click', answer);
  $('answer').addEventListener('click', answer);
  $('asks').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-intent]');
    if (b) ask(b.dataset.intent, b.textContent);
  });
  $('ask-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const text = $('ask-text').value.trim();
    if (!text) return;
    ask(null, text);
    $('ask-text').value = '';
  });
  $('save').addEventListener('click', () => save());
  document.addEventListener('keydown', (ev) => {
    if (ev.altKey && (ev.key === 's' || ev.key === 'ы' || ev.code === 'KeyS')) { ev.preventDefault(); save(); }
    if (ev.altKey && (ev.key === 'm' || ev.key === 'ь' || ev.code === 'KeyM') && !$('mic').disabled) { ev.preventDefault(); micSet(!micWanted); }
    if (ev.key === 'Enter' && S.call && !S.call.answered_at && !ev.target.closest('input,textarea')) { ev.preventDefault(); answer(); }
    if (ev.key === 'Escape' && !$('modal').hidden) closeModal();
  });

  // ---------- таймер и полоса тренажёра ----------
  function tick() {
    const c = S.call;
    const t = $('timer');
    if (c && c.answered_at) {
      const sec = Math.max(0, Clock.now() - c.answered_at);
      $('timer-v').textContent = Clock.span(sec);
      t.classList.toggle('red', sec > ((S.desk && S.desk.norm_s) || 80));
    } else {
      $('timer-v').textContent = '00:00';
      t.classList.remove('red');
    }
    const L = S.lesson;
    let txt = '';
    if (L && L.started_at && L.state === 'идёт') txt = 'идёт ' + Clock.span(Clock.now() - L.started_at);
    $('tr-time').textContent = txt;
  }
  setInterval(tick, 250);

  const CONN = { online: 'на связи', reconnecting: 'связь восстанавливается…', offline: 'нет связи', denied: 'нет доступа' };
  function renderTrainer() {
    const L = S.lesson;
    $('tr-user').textContent = me.name;
    $('tr-lesson').innerHTML = L ? 'Занятие <b>' + esc(L.id) + '</b> · ' + esc(L.title) : 'Подключение…';
    $('tr-conn').dataset.state = S.conn;
    $('tr-conn').textContent = CONN[S.conn] || S.conn;
    const d = S.desk;
    $('tr-progress').textContent = d ? 'обработано ' + (d.done ? d.done.length : 0) + ' из ' + d.total : '';
    $('tr-cards').hidden = !(d && d.done && d.done.length);
    $('tr-result').hidden = !S.result;
    $('tr-finish').hidden = !(L && L.solo && L.state === 'идёт');
  }

  async function finishSolo(ask) {
    if (ask && !confirm('Завершить задание? Необработанные вызовы будут засчитаны как необработанные.')) return;
    try {
      await API.call('POST', '/api/lessons/' + encodeURIComponent(lessonId) + '/finish');
    } catch (e) { if (e.status !== 409) toast(e.message, false); }
  }

  // ---------- окна ----------
  function showModal(html) { $('modal-box').innerHTML = html; $('modal').hidden = false; }
  function closeModal() { $('modal').hidden = true; }
  $('modal').addEventListener('click', (ev) => { if (ev.target === $('modal') || ev.target.closest('[data-close]')) closeModal(); });

  function findingsHtml(list) {
    return list.length ? list.map((f) => '<div class="fnd"><b>№' + f.category + '. ' + esc(f.title) + '</b><div class="d">' + esc(f.detail) + '</div></div>').join('')
      : '<p>Замечаний нет — карточка заполнена по эталону.</p>';
  }

  function goldHtml(g) {
    if (!g) return '';
    const a = g.address || {};
    const addr = [a.subject, a.locality, a.street, a.house && 'д. ' + a.house, a.korpus && 'к. ' + a.korpus,
      a.stroenie && 'стр. ' + a.stroenie, a.flat && 'кв. ' + a.flat, a.entrance && 'под. ' + a.entrance,
      a.floor && 'эт. ' + a.floor, a.code && 'код ' + a.code].filter(Boolean).join(', ');
    return '<div class="res-gold"><b>Как надо:</b> тип «' + esc(g.type) + '» (' + esc(g.code) + ')' +
      (addr ? '<br>адрес: ' + esc(addr) : '') + (g.flags && g.flags.length ? '<br>признаки: ' + esc(g.flags.join(', ')) : '') +
      '<br>пострадавшие: ' + (g.victims === null ? 'не проверяется' : g.victims ? 'есть' : 'нет') +
      '<br><span style="color:#555">ситуация из билета: ' + esc(g.situation) + '</span></div>';
  }

  function showCardResult(r) {
    showModal('<h2>Карточка ' + esc(r.number) + ': ' + r.score + ' из 100</h2><p class="sub-t">заполнена за ' +
      (r.fill_s === null ? '—' : Math.round(r.fill_s) + ' с') + ' (норматив ' + Math.round((S.desk && S.desk.norm_s) || 80) + ' с), принят вызов через ' +
      (r.answer_s === null ? '—' : Math.round(r.answer_s) + ' с') + '</p>' + findingsHtml(r.findings) + goldHtml(r.gold) +
      '<div class="modal-foot"><button type="button" class="btn" data-close>К следующему вызову</button></div>');
  }

  function showFinal() {
    const r = S.result;
    if (!r) return;
    S.resultShown = true;
    const count = {};
    for (const f of r.findings) count[f.category] = (count[f.category] || 0) + 1;
    const rows = (r.categories || []).map((c) => '<tr class="' + (count[c.category] ? 'hit' : '') + '"><td>' + c.category + '</td><td>' +
      esc(c.title) + '</td><td class="n">' + (count[c.category] || '—') + '</td></tr>').join('');
    const t = r.timing || {};
    showModal('<h2>Итог занятия</h2><p class="sub-t">' + esc(S.lesson ? S.lesson.title : '') + ' · карточек: ' + r.cards +
      ' · среднее время ' + (t.avg_fill_s === null || t.avg_fill_s === undefined ? '—' : Math.round(t.avg_fill_s) + ' с') +
      ' при нормативе ' + Math.round(t.norm_s || 80) + ' с</p><div class="score' + (r.score < 70 ? ' low' : '') + '"><b>' + r.score +
      '</b><span>баллов из 100 (среднее по карточкам)</span></div><h3>Ошибки по категориям</h3><table class="cat-table">' +
      '<tr><th>№</th><th>Категория</th><th class="n">Ошибок</th></tr>' + rows + '</table>' +
      '<h3>Разбор карточек</h3>' + (r.cards_detail || []).map((c) => '<h3>Карточка ' + esc(c.number) + ' — ' + c.score + '</h3>' +
        findingsHtml(c.findings) + goldHtml(c.gold)).join('') +
      '<div class="modal-foot"><button type="button" class="btn" data-close>Закрыть</button></div>');
  }

  $('tr-cards').addEventListener('click', () => {
    const done = (S.desk && S.desk.done) || [];
    showModal('<h2>Мои карточки</h2>' + done.map((c) => '<h3>Карточка ' + esc(c.number) + ' — ' + c.score + ' из 100</h3>' +
      findingsHtml(c.findings) + goldHtml(c.gold)).join('') + '<div class="modal-foot"><button type="button" class="btn" data-close>Закрыть</button></div>');
  });
  $('tr-result').addEventListener('click', showFinal);

  let toastTimer = null;
  function toast(text, ok) {
    const t = $('toast');
    t.textContent = text;
    t.className = 'toast' + (ok ? ' ok' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
  }

  Voice.bindButton($('tr-sound'));
  $('tr-finish').addEventListener('click', () => finishSolo(true));
  $('tr-cab').addEventListener('click', () => {
    const running = S.lesson && S.lesson.state === 'идёт';
    if (running && !confirm('Перейти в кабинет? Занятие продолжится — вернуться можно из кабинета.')) return;
    location.assign('cabinet.html');
  });
  $('tr-exit').addEventListener('click', async () => {
    if (S.lesson && S.lesson.state === 'идёт' && !confirm('Выйти из тренажёра? Текущий вызов останется необработанным.')) return;
    if (sock) sock.close();
    Mic.stop();
    setS(LESSON_KEY, null);
    try { await API.call('POST', '/api/logout'); } catch (e) { /* уже не действует */ }
    API.clear();
    location.replace('index.html');
  });

  $('mic').addEventListener('click', () => micSet(!micWanted));
  if (!Mic.supported()) {
    $('mic').disabled = true;
    $('mic').title = Mic.why();
    micNote(Mic.why());
  } else if (micWanted) {
    // браузер запускает звук только после действия пользователя: микрофон — при первом клике или клавише
    const kick = (ev) => {
      document.removeEventListener('pointerdown', kick, true);
      document.removeEventListener('keydown', kick, true);
      if (micWanted && !Mic.active && !(ev.target && ev.target.id === 'mic')) micStart();
    };
    document.addEventListener('pointerdown', kick, true);
    document.addEventListener('keydown', kick, true);
    micNote('микрофон включится при первом действии на странице');
  }
  micPaint();

  renderTrainer();
  loadCallTypes();
  connect();
})();
