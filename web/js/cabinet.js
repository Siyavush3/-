// Кабинет обучающегося (узел 6, ТЗ «Роль: обучающийся»): назначенные задания, свои результаты с оценкой
// преподавателя, прогресс и прогноз, рекомендации системы и преподавателя, методические материалы.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  if (me.role !== 'student') { location.replace(me.role === 'teacher' ? 'teacher.html' : 'admin.html'); return; }
  $('who').textContent = me.name;
  const LESSON_KEY = 'a112.lesson.' + me.id;
  const LEVEL = { 1: 'базовая', 2: 'средняя', 3: 'высокая' };
  const loaded = {};

  function fail(e, where) {
    if (e.status === 401) { API.clear(); location.replace('index.html#expired'); return; }
    if (where) $(where).textContent = e.message;
  }

  // ---------- вкладки ----------
  function show(view) {
    for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('on', b.dataset.view === view);
    for (const v of document.querySelectorAll('.view')) v.hidden = v.id !== 'view-' + view;
    if (view === 'tasks') loadTasks();
    if (view === 'results' && !loaded.results) loadResults();
    if (view === 'progress' && !loaded.progress) loadProgress();
    if (view === 'materials' && !loaded.materials) loadMaterials();
    try { sessionStorage.setItem('a112.cab.view', view); } catch (e) { /* не запомним */ }
  }
  $('tabs').addEventListener('click', (ev) => { const b = ev.target.closest('[data-view]'); if (b) show(b.dataset.view); });

  // ---------- занятия преподавателя, которые идут сейчас ----------
  async function loadLive() {
    let list = [];
    try { list = await API.call('GET', '/api/lessons'); } catch (e) { return fail(e); }
    const open = list.filter((l) => l.state === 'набор' || l.state === 'идёт');
    $('live').hidden = !open.length;
    $('live-text').textContent = open.length ? 'Преподаватель открыл занятие: ' + open.map((l) => l.pack.title + ' (' + l.id + ')').join(', ') : '';
  }
  $('live-go').addEventListener('click', () => location.assign('arm.html'));

  // ---------- задания ----------
  async function loadTasks() {
    let tasks;
    try { tasks = await API.call('GET', '/api/tasks'); } catch (e) { return fail(e, 'tasks-err'); }
    $('tasks').innerHTML = tasks.length ? tasks.map((t) => {
      const last = t.attempts[t.attempts.length - 1];
      const due = t.due_at ? 'срок — ' + Clock.dmy(t.due_at) : '';
      return '<div class="task' + (t.passed ? ' done' : '') + '"><h3>' + esc(t.title) + '</h3>' +
        '<div class="m">' + (t.mode === 'cards' ? 'Оператор Службы 112 — приём вызовов и заполнение карточки' : esc(t.role_title) + ' — действия с карточками') +
        '<br>Группа «' + esc(t.group_name) + '» · ' + esc(t.teacher || '') + (t.difficulty ? ' · сложность: ' + esc(LEVEL[t.difficulty]) : '') + (due ? ' · ' + esc(due) : '') + '</div>' +
        (t.note ? '<div class="m">' + esc(t.note) + '</div>' : '') +
        '<div class="foot">' + (t.attempts.length ? '<span class="pill ' + (t.passed ? 'ok' : 'no') + '">' + (t.passed ? 'зачтено' : 'не зачтено') + '</span>' +
          '<span class="m">попыток: ' + t.attempts.length + ' · лучший: ' + t.best + (last ? ' · последний: ' + last.score : '') + '</span>' : '<span class="m">ещё не выполнялось</span>') +
        '<span class="spacer"></span>' + (t.available ? '<button type="button" class="primary" data-start="' + t.id + '">' + (t.active_lesson ? 'Продолжить' : t.attempts.length ? 'Ещё раз' : 'Начать') + '</button>'
          : '<span class="m">сценарий недоступен</span>') + '</div></div>';
    }).join('') : '<div class="empty-s">Преподаватель пока не назначил заданий вашей группе.</div>';
  }

  $('tasks').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-start]');
    if (!b) return;
    b.disabled = true;
    $('tasks-err').textContent = '';
    try {
      const l = await API.call('POST', '/api/tasks/' + b.dataset.start + '/start');
      try { sessionStorage.setItem(LESSON_KEY, l.id); } catch (e) { /* без хранилища тренажёр предложит выбрать занятие */ }
      location.assign(l.page);
    } catch (e) { b.disabled = false; fail(e, 'tasks-err'); }
  });

  // ---------- результаты ----------
  async function loadResults() {
    let rows;
    try { rows = await API.call('GET', '/api/results/me'); } catch (e) { return fail(e); }
    loaded.results = true;
    $('results').innerHTML = rows.length ? rows.map((r) => {
      const final = r.expert_score !== null && r.expert_score !== undefined ? r.expert_score : r.score;
      const s = r.summary || {};
      const pass = s.criteria ? final >= s.criteria.pass_score && (s.criteria.max_errors === null || s.criteria.max_errors === undefined || r.findings.length <= s.criteria.max_errors) : null;
      const byCat = {};
      for (const f of r.findings) byCat[f.category] = byCat[f.category] || { title: f.title, items: [] }, byCat[f.category].items.push(f);
      return '<div class="res-row"><div class="h"><b>' + final + '</b><div class="t">' + esc(r.title) + '<span>' + esc(r.lesson) + ' · ' +
        (r.mode === 'карточки' ? 'оператор 112' : 'ДДС') + (r.finished_at ? ' · ' + Clock.dmy(r.finished_at) : '') + ' · замечаний: ' + r.findings.length + '</span></div>' +
        (pass === null ? '' : '<span class="pill ' + (pass ? 'ok' : 'no') + '">' + (pass ? 'зачтено' : 'не зачтено') + '</span>') +
        (r.expert_score !== null && r.expert_score !== undefined ? '<span class="pill ex" title="Автоматическая оценка: ' + r.score + '">оценка преподавателя</span>' : '') +
        '<a href="report.html?lesson=' + encodeURIComponent(r.lesson) + '">подробный отчёт</a></div>' +
        (r.expert_comment ? '<div class="ex"><b>Преподаватель:</b> ' + esc(r.expert_comment) + '</div>' : '') +
        (r.findings.length ? '<details><summary>Разбор ошибок</summary>' + Object.keys(byCat).sort((a, b) => a - b).map((c) =>
          '<div class="fnd"><b>№' + c + '. ' + esc(byCat[c].title) + '</b> — ' + byCat[c].items.length + '<br><span>' +
          byCat[c].items.map((f) => esc((f.card ? 'карточка ' + f.card + ': ' : '') + f.detail)).join('<br>') + '</span></div>').join('') + '</details>' : '') + '</div>';
    }).join('') : '<div class="empty-s">Результатов пока нет — выполните задание или занятие.</div>';
  }

  // ---------- прогресс ----------
  async function loadProgress() {
    let p, fb;
    try { [p, fb] = await Promise.all([API.call('GET', '/api/me/progress'), API.call('GET', '/api/me/feedback')]); } catch (e) { return fail(e); }
    loaded.progress = true;
    const s = p.student;
    $('chart').innerHTML = s.history.length ? Charts.progress(s.history, s.forecast, p.threshold) : '<div class="empty-s">Пока нет результатов.</div>';
    const f = s.forecast;
    const bt = s.backtest;
    $('forecast').innerHTML = f ? 'Прогноз на следующее занятие: <b class="big">' + f.next + '</b> (80% интервал ' + f.low + '–' + f.high + '), вероятность зачёта — ' +
      Math.round(f.p_pass * 100) + '%. Темп: ' + (f.trend > 0 ? '+' : '') + String(f.trend).replace('.', ',') + ' балла за занятие' +
      (f.lessons_to_pass && f.lessons_to_pass > 1 ? '; порог при таком темпе — примерно через ' + f.lessons_to_pass + ' занятия' : '') + '.' +
      (bt && bt.n ? '<br><span class="note">Точность прогноза на ваших прошлых занятиях: ошибка ' + String(bt.mae).replace('.', ',') + ' балла (' + bt.n + ' пров.).</span>' : '') : '';
    $('recs').innerHTML = s.recommendations.length ? s.recommendations.map((r) => '<li><b>' + esc(r.mode + ' · №' + r.category + '. ' + r.title) + '</b>' +
      '<span class="who">за последние занятия: ' + r.count + ' (' + String(r.per10).replace('.', ',') + ' на 10 карточек)</span><br>' + esc(r.text) + '</li>').join('')
      : '<li>Ошибок в последних занятиях нет — так держать.</li>';
    $('feedback').innerHTML = fb.length ? fb.map((x) => '<li>' + esc(x.text) + '<br><span class="who">' + esc(x.teacher) + ' · ' + Clock.dmy(x.created_at) + '</span></li>').join('')
      : '<li class="note">Пока нет.</li>';
    $('heat').innerHTML = Object.keys(s.heat).length ? Object.entries(s.heat).map(([mode, h]) => '<h3>' + (mode === 'ДДС' ? 'Диспетчер ДДС' : 'Оператор 112') + '</h3>' +
      Charts.heatmap([{ name: me.name, values: h.per10, counts: h.counts, cards: h.cards }], p.categories[mode])).join('')
      : '<div class="empty-s">Пока нет данных.</div>';
  }

  // ---------- материалы ----------
  async function loadMaterials() {
    let rows;
    try { rows = await API.call('GET', '/api/materials'); } catch (e) { return fail(e); }
    loaded.materials = true;
    $('materials').innerHTML = rows.length ? rows.map((m) => '<div class="mat"><div class="t"><b>' + esc(m.title) + '</b><span>' + esc(m.filename) + ' · ' +
      Math.max(1, Math.round(m.size / 1024)) + ' КБ · ' + esc(m.author) + (m.group_name ? ' · группа «' + esc(m.group_name) + '»' : '') + '</span>' +
      (m.description ? '<span>' + esc(m.description) + '</span>' : '') + '</div><button type="button" data-open="' + m.id + '" data-name="' + esc(m.filename) + '">Открыть</button></div>').join('')
      : '<div class="empty-s">Материалов пока нет.</div>';
  }

  $('materials').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-open]');
    if (b) await openMaterial(b.dataset.open, b.dataset.name);
  });

  window.openMaterial = async function openMaterial(id, name) {
    const resp = await fetch('/api/materials/' + id + '/file', { headers: { Authorization: 'Bearer ' + API.token } });
    if (!resp.ok) { alert('Файл недоступен (' + resp.status + ')'); return; }
    const url = URL.createObjectURL(await resp.blob());
    const inline = /^(application\/pdf|image\/|audio\/|video\/|text\/plain)/.test(resp.headers.get('content-type') || '');
    if (inline) { window.open(url, '_blank'); } else {
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  };

  $('exit').addEventListener('click', async () => {
    try { await API.call('POST', '/api/logout'); } catch (e) { /* уже не действует */ }
    API.clear();
    location.replace('index.html');
  });

  let start = 'tasks';
  try { start = sessionStorage.getItem('a112.cab.view') || 'tasks'; } catch (e) { /* с начала */ }
  show(start);
  loadLive();
  setInterval(loadLive, 5000);
})();
