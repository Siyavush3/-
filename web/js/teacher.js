// Экран преподавателя: создать занятие, назначить роли, старт/стоп, живой монитор, итоги.
// Монитор приходит по WebSocket раз в секунду, пока занятие идёт (ARCHITECTURE.md §18.10).
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  if (me.role !== 'teacher') { location.replace(me.role === 'admin' ? 'admin.html' : 'arm.html'); return; }
  $('who').textContent = me.name;

  const CONN = { 'на связи': 'on', 'восстанавливается': 're', 'нет связи': 'off' };
  const T = { packs: [], lessons: [], sel: null, monitor: null, results: null, conn: 'offline', busy: false };
  let sock = null;

  const painted = new Map();
  function paint(id, html) { // перерисовка только при изменении: кнопки не пересоздаются под курсором
    if (painted.get(id) === html) return;
    painted.set(id, html);
    $(id).innerHTML = html;
  }

  async function logout(reason) {
    if (sock) sock.close();
    try { if (reason !== 'expired') await API.call('POST', '/api/logout'); } catch (e) { /* уже не действует */ }
    API.clear();
    location.replace(reason === 'expired' ? 'index.html#expired' : 'index.html');
  }

  function fail(where, e) {
    if (e.status === 401) { logout('expired'); return; }
    $(where).textContent = e.message;
  }

  // ---------- сценарии и список занятий ----------
  // Вкладки: у каждой — свой обработчик, вызывается при открытии (узел 6).
  window.TeacherView = {
    handlers: {},
    show(view) {
      for (const x of document.querySelectorAll('#tabs button')) x.classList.toggle('on', x.dataset.view === view);
      for (const v of document.querySelectorAll('.tview')) v.hidden = v.id !== 'view-' + view;
      if (this.handlers[view]) this.handlers[view]();
    },
  };
  $('tabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-view]');
    if (b) TeacherView.show(b.dataset.view);
  });

  async function loadGroupsSelect() {
    let groups = [];
    try { groups = await API.call('GET', '/api/groups'); } catch (e) { return; }
    const keep = $('lgroup').value;
    $('lgroup').innerHTML = '<option value="">любые обучающиеся</option>' +
      groups.map((g) => '<option value="' + g.id + '">' + esc(g.name) + ' (' + g.members.length + ')</option>').join('');
    $('lgroup').value = groups.some((g) => String(g.id) === keep) ? keep : '';
  }

  async function loadPacks() {
    loadGroupsSelect();
    try {
      T.packs = await API.call('GET', '/api/packs');
    } catch (e) { fail('create-err', e); return; }
    const keep = $('pack').value;
    $('pack').innerHTML = T.packs.map((p) => '<option value="' + esc(p.id) + '">' + esc(p.title) + '</option>').join('');
    if (T.packs.some((p) => p.id === keep)) $('pack').value = keep;
    packInfo();
  }

  function packInfo() {
    const p = T.packs.find((x) => x.id === $('pack').value);
    const cards = p && p.mode === 'cards';
    $('pack-info').textContent = !p ? 'Нет сценариев: положите пакет в data/scenarios.'
      : cards ? 'Режим «карточки»: обучающиеся — операторы Службы 112, принимают вызовы и заполняют карточки. Вызовов в пакете: ' + p.cards
        : 'Режим «действия с карточками»: роль обучающихся — ' + p.role_title + ' · карточек: ' + p.cards +
          ' · последняя приходит на ' + Math.ceil(p.duration_s / 60) + '-й минуте (в реальном времени)' +
          ' · карточек обучающихся для этой службы: ' + (p.student_cards || 0);
    $('cards-opts').hidden = !cards;
    $('dds-opts').hidden = !p || !!cards;
    $('pnorm-box').hidden = !!cards;
    $('anorm-box').hidden = !cards;
    $('scount-box').hidden = $('source').value === 'pack';
    $('tempo-box').hidden = !!cards;
    if (cards) {
      paint('groups', p.groups.map((g) => '<label><input type="checkbox" value="' + esc(g.group) + '"> ' + esc(g.title) +
        '<small>' + g.count + '</small></label>').join(''));
      if (p.norm_s) $('norm').value = Math.round(p.norm_s);
    }
    $('create').disabled = !p;
  }

  async function loadLessons() {
    try {
      T.lessons = await API.call('GET', '/api/lessons');
    } catch (e) { if (e.status === 401) logout('expired'); return; }
    renderLessons();
    renderLesson();
  }

  function renderLessons() {
    paint('lessons', T.lessons.length
      ? T.lessons.slice().reverse().map((l) =>
        '<button type="button" class="lesson-item' + (l.id === T.sel ? ' sel' : '') + '" data-id="' + esc(l.id) + '"><b>' +
        esc(l.id) + ' · ' + esc(l.pack.title) + '</b><span>' + esc(l.state) + ' · участников: ' + l.participants.length +
        '</span></button>').join('')
      : '<div class="none">Пока нет занятий.</div>');
  }

  function select(id) {
    if (T.sel === id) return;
    T.sel = id;
    T.monitor = null;
    T.results = null;
    $('l-err').textContent = '';
    if (sock) sock.close();
    sock = null;
    const l = T.lessons.find((x) => x.id === id);
    if (l && !l.live) { // история: занятие уже не в памяти сервера — итоги из базы
      T.conn = 'history';
      loadStoredResults(id);
      renderLessons();
      renderLesson();
      return;
    }
    sock = API.socket(id, {
      message: onMessage,
      status: (s, code) => {
        if (s === 'denied' && code === 4401) { logout('expired'); return; }
        T.conn = s;
        renderLesson();
      },
    });
    renderLessons();
    renderLesson();
  }

  async function loadStoredResults(id) {
    try {
      T.results = await API.call('GET', '/api/lessons/' + encodeURIComponent(id) + '/results');
    } catch (e) {
      T.results = null;
      if (e.status === 401) { logout('expired'); return; }
      $('l-err').textContent = e.status === 409 ? 'Итогов нет: занятие не было завершено.' : e.message;
    }
    renderLesson();
  }

  function onMessage(msg) {
    if (msg.t === 'monitor') {
      Clock.sync(msg.server_time);
      T.monitor = msg;
      const l = T.lessons.find((x) => x.id === msg.lesson.id);
      if (l && l.state !== msg.lesson.state) loadLessons();
      renderLesson();
    } else if (msg.t === 'results') {
      T.results = msg.results;
      loadLessons();
      setTimeout(() => { if (T.sel === msg.results.lesson) loadStoredResults(T.sel); }, 600); // с номерами результатов — для экспертной оценки
    }
  }

  // ---------- занятие ----------
  function current() { return T.lessons.find((x) => x.id === T.sel); }

  function elapsedText() {
    const m = T.monitor && T.monitor.lesson;
    if (!m || !m.started_at) return '';
    const end = m.finished_at || Clock.now();
    return (m.state === 'идёт' ? 'идёт ' : 'длилось ') + Clock.span(end - m.started_at);
  }

  function renderLesson() {
    const l = current();
    $('empty').hidden = !!l;
    $('lesson').hidden = !l;
    if (!l) return;
    const m = T.monitor && T.monitor.lesson.id === l.id ? T.monitor : null;
    const state = m ? m.lesson.state : l.state;
    const stored = !l.live && T.results ? T.results.participants.map((p) => ({ participant: p.participant, name: p.name, role: p.role })) : null;
    const rows = m ? m.participants : stored || l.participants.map((p) => ({ participant: p.id, name: p.name, role: p.role }));
    const unassigned = rows.filter((r) => !r.role);

    $('l-title').textContent = l.id + ' · ' + l.pack.title;
    const tempo = l.time_scale < 1 ? 'ускорение ×' + Math.round(1 / l.time_scale)
      : l.time_scale > 1 ? 'замедление ×' + l.time_scale : 'реальное время';
    paint('l-meta', '<span class="badge s-' + esc(state) + '">' + esc(state) + '</span>' +
      'роль: ' + esc(l.pack.role_title) + ' · ' + tempo + ' <span id="elapsed">' + esc(elapsedText() ? '· ' + elapsedText() : '') + '</span>');

    $('assign-all').disabled = T.busy || state !== 'набор' || !unassigned.length;
    $('start').disabled = T.busy || state !== 'набор' || !rows.length || unassigned.length > 0;
    $('finish').disabled = T.busy || state !== 'идёт';

    const sum = (k) => rows.reduce((a, r) => a + (r[k] || 0), 0);
    const online = rows.filter((r) => r.connection === 'на связи').length;
    const sent = m ? m.lesson.cards_sent : 0;
    const total = m ? m.lesson.cards_total : l.pack.cards;
    const cardsMode = l.mode === 'cards';
    paint('thead', cardsMode
      ? '<tr><th>Обучающийся</th><th>Роль</th><th>Связь</th><th>Вызов</th><th class="n">Обработано</th>' +
        '<th class="n">Среднее время, с</th><th class="n">Средний балл</th><th class="n">Просрочено</th></tr>'
      : '<tr><th>Обучающийся</th><th>Роль</th><th>Связь</th><th class="n">Карточек</th><th class="n">Ждут статуса</th>' +
        '<th class="n">Просрочено &gt; 30 с</th><th class="n">Ход работ не отмечен</th></tr>');
    if (cardsMode) {
      const scores = rows.map((r) => r.avg_score).filter((x) => x !== null && x !== undefined);
      $('stats').innerHTML =
        stat(sum('calls_done') + ' / ' + (rows.length * (m ? m.lesson.cards_total : (l.config.count || 8))), 'карточек обработано') +
        stat(online + ' / ' + rows.length, 'обучающихся на связи', rows.length && online < rows.length) +
        stat(scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : '—', 'средний балл по группе') +
        stat(sum('overdue'), 'сейчас с превышением норматива', sum('overdue') > 0);
    } else $('stats').innerHTML =
      stat(sent + ' / ' + total, 'карточек выдано ботом-оператором 112') +
      stat(online + ' / ' + rows.length, 'обучающихся на связи', rows.length && online < rows.length) +
      stat(sum('waiting'), 'карточек ждут первичного статуса') +
      stat(sum('overdue'), 'из них дольше 30 с (нарушение №1)', sum('overdue') > 0);

    $('conn').textContent = !l.live ? '· из архива занятий' : T.conn === 'online' ? '· обновляется в реальном времени' : '· монитор переподключается…';
    paint('rows', rows.length ? rows.map((r) => {
      const dot = CONN[r.connection] || '';
      if (cardsMode) {
        const role = r.role ? 'Оператор 112'
          : (state === 'набор' ? '<button type="button" data-assign="' + esc(r.participant) + '">Назначить роль</button>' : '—');
        return '<tr><td>' + esc(r.name) + '</td><td>' + role + '</td><td>' +
          (r.connection ? '<span class="dot ' + dot + '"></span>' + esc(r.connection) : '—') + '</td><td>' + esc(r.call_state || '—') + '</td>' +
          '<td class="n">' + (r.calls_done ?? '—') + (r.calls_total ? ' / ' + r.calls_total : '') + '</td>' +
          '<td class="n">' + (r.avg_fill_s ?? '—') + '</td><td class="n">' + (r.avg_score ?? '—') + '</td>' +
          '<td class="n' + (r.overdue ? ' bad' : '') + '">' + (r.overdue ?? '—') + '</td></tr>';
      }
      const role = r.role ? esc(l.pack.role_title)
        : (state === 'набор' ? '<button type="button" data-assign="' + esc(r.participant) + '">Назначить роль</button>' : '—');
      return '<tr><td>' + esc(r.name) + '</td><td>' + role + '</td><td>' +
        (r.connection ? '<span class="dot ' + dot + '"></span>' + esc(r.connection) : '—') + '</td>' +
        '<td class="n">' + (r.cards ?? '—') + '</td><td class="n">' + (r.waiting ?? '—') + '</td>' +
        '<td class="n' + (r.overdue ? ' bad' : '') + '">' + (r.overdue ?? '—') + '</td>' +
        '<td class="n' + (r.late_progress ? ' bad' : '') + '">' + (r.late_progress ?? '—') + '</td></tr>';
    }).join('') : '<tr><td colspan="7" class="none">Пока никто не вошёл.</td></tr>');

    paint('join-note', state === 'набор'
      ? 'Обучающимся: откройте <code>' + esc(location.origin) + '</code>, войдите своей учётной записью и выберите занятие <b>' +
        esc(l.id) + '</b>. Затем назначьте роли и начните занятие.'
      : '');
    renderResults(l);
  }

  function stat(value, label, warn) {
    return '<div class="stat' + (warn ? ' warn' : '') + '"><b>' + esc(value) + '</b><span>' + esc(label) + '</span></div>';
  }

  function renderResults(l) {
    const r = T.results && T.results.lesson === l.id ? T.results : null;
    $('results-box').hidden = !r;
    if (!r) return;
    paint('results', r.participants.length ? r.participants.map((p) => {
      const count = {};
      for (const f of p.findings) count[f.category] = (count[f.category] || 0) + 1;
      const cats = r.categories.map((c) =>
        '<div class="cat' + (count[c.category] ? ' hit' : '') + '"><b>' + (count[c.category] || 0) + '</b>№' + c.category +
        '. ' + esc(c.title) + '</div>').join('');
      const finds = p.findings.length
        ? '<details><summary>Замечания (' + p.findings.length + ')</summary>' + p.findings.map((f) =>
          '<div class="fnd">№' + f.category + (f.card ? ' · карточка ' + esc(f.card) : '') + ' · <span>' + esc(f.detail) +
          '</span></div>').join('') + '</details>'
        : '<div class="fnd">Замечаний нет.</div>';
      const final = p.final_score ?? p.score;
      const verdict = p.passed === undefined ? '' : '<span class="pill ' + (p.passed ? 'ok' : 'no') + '" title="' +
        esc((p.reasons || []).join('; ')) + '">' + (p.passed ? 'зачтено' : 'не зачтено') + '</span>';
      const expert = p.result_id ? '<div class="expert"><span>Экспертная оценка:</span>' +
        '<input type="number" min="0" max="100" data-exp-score="' + p.result_id + '" value="' + (p.expert_score ?? '') + '" placeholder="авто ' + p.score + '">' +
        '<input data-exp-comment="' + p.result_id + '" maxlength="2000" value="' + esc(p.expert_comment || '') + '" placeholder="Комментарий для обучающегося (увидит в кабинете)">' +
        '<button type="button" data-exp-save="' + p.result_id + '">Сохранить</button></div>' : '';
      return '<div class="res"><div class="res-head"><b>' + esc(p.name) + '</b><span class="res-score' +
        (p.passed === false ? ' low' : '') + '">' + final + '</span><span>баллов из 100' +
        (p.expert_score !== null && p.expert_score !== undefined ? ' (эксперт; авто — ' + p.score + ')' : '') + ' · карточек: ' + p.cards +
        ' · замечаний: ' + p.findings.length + '</span>' + verdict + (p.timing ? '<span class="timing">среднее время карточки ' +
          (p.timing.avg_fill_s ?? '—') + ' с при нормативе ' + Math.round(p.timing.norm_s) + ' с · дольше норматива: ' +
          p.timing.over_norm + '</span>' : '') + '</div>' +
        (p.passed === false && p.reasons && p.reasons.length ? '<div class="reasons">Не зачтено: ' + esc(p.reasons.join('; ')) + '</div>' : '') +
        '<div class="cats">' + cats + '</div>' + finds + expert + '</div>';
    }).join('') : '<p class="none">В занятии не было обучающихся с ролями.</p>');
  }

  // ---------- команды ----------
  async function act(fn) {
    if (T.busy) return;
    T.busy = true;
    $('l-err').textContent = '';
    renderLesson();
    try {
      await fn();
    } catch (e) {
      fail('l-err', e);
    } finally {
      T.busy = false;
      await loadLessons();
    }
  }

  function assign(pid) {
    const l = current();
    return API.call('POST', '/api/lessons/' + encodeURIComponent(l.id) + '/roles',
      { participant: pid, service: l.pack.role || 'Оператор 112' });
  }

  $('create').addEventListener('click', async () => {
    $('create-err').textContent = '';
    try {
      const p = T.packs.find((x) => x.id === $('pack').value);
      const body = { pack_id: $('pack').value, time_scale: Number($('tempo').value) };
      Object.assign(body, { pass_score: Number($('pass').value), text_check: $('textcheck').checked });
      if ($('maxerr').value !== '') body.max_errors = Number($('maxerr').value);
      if ($('lgroup').value) body.group_id = Number($('lgroup').value);
      if (p && p.mode === 'cards') body.answer_norm_s = Number($('anorm').value) || 15;
      else body.primary_norm_s = Number($('pnorm').value) || 30;
      if (p && p.mode !== 'cards' && $('source').value !== 'pack') {
        Object.assign(body, { source: $('source').value, count: Number($('scount').value) || 8 });
      }
      if (p && p.mode === 'cards') {
        Object.assign(body, {
          time_scale: 1, count: Number($('count').value) || 8, norm_s: Number($('norm').value) || 80,
          feedback: $('feedback').value, groups: [...document.querySelectorAll('#groups input:checked')].map((x) => x.value),
        });
      }
      const l = await API.call('POST', '/api/lessons', body);
      await loadLessons();
      select(l.id);
    } catch (e) { fail('create-err', e); }
  });
  $('pack').addEventListener('change', packInfo);
  $('source').addEventListener('change', packInfo);
  window.TeacherPacks = loadPacks; // вкладка «Сценарии»: после утверждения сценарий сразу в списке
  TeacherView.handlers.lessons = loadPacks;
  $('open-report').addEventListener('click', () => window.open('report.html?lesson=' + encodeURIComponent(T.sel), '_blank'));
  $('results').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-exp-save]');
    if (!b) return;
    const rid = b.dataset.expSave;
    const score = document.querySelector('[data-exp-score="' + rid + '"]').value;
    const comment = document.querySelector('[data-exp-comment="' + rid + '"]').value;
    b.disabled = true;
    try {
      await API.call('PUT', '/api/results/' + rid + '/expert', { score: score === '' ? null : Number(score), comment });
      await loadStoredResults(T.sel);
    } catch (e) { fail('l-err', e); } finally { b.disabled = false; }
  });
  $('lessons').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-id]');
    if (b) select(b.dataset.id);
  });
  $('rows').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-assign]');
    if (b) act(() => assign(b.dataset.assign));
  });
  $('assign-all').addEventListener('click', () => act(async () => {
    const l = current();
    for (const p of l.participants.filter((x) => !x.role)) await assign(p.id);
  }));
  $('start').addEventListener('click', () => act(() => API.call('POST', '/api/lessons/' + encodeURIComponent(T.sel) + '/start')));
  $('finish').addEventListener('click', () => {
    if (!confirm('Завершить занятие? Будет посчитан итог по всем обучающимся.')) return;
    act(() => API.call('POST', '/api/lessons/' + encodeURIComponent(T.sel) + '/finish'));
  });
  $('exit').addEventListener('click', () => logout());

  setInterval(() => {
    const el = $('elapsed');
    if (el) el.textContent = elapsedText() ? '· ' + elapsedText() : '';
  }, 1000);
  setInterval(loadLessons, 3000); // новые участники в наборе и смена состояния

  $('hint').textContent = 'адрес для обучающихся: ' + location.origin;
  (async () => {
    await loadPacks();
    await loadLessons();
    if (T.lessons.length) select(T.lessons[T.lessons.length - 1].id);
  })();
})();
