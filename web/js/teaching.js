// Вкладки преподавателя «Группы и задания», «Аналитика», «Материалы» (узел 6).
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  if (!API.token || !API.user || API.user.role !== 'teacher') return;
  const me = API.user;
  const LEVEL = { 1: 'базовая', 2: 'средняя', 3: 'высокая' };
  const num = (v) => String(v).replace('.', ',');
  const G = { groups: [], students: [], sel: null, packs: [] };

  function fail(where, e) {
    if (e.status === 401) { API.clear(); location.replace('index.html#expired'); return; }
    $(where).textContent = e.message;
  }

  async function download(path, name) {
    const resp = await fetch(path, { headers: { Authorization: 'Bearer ' + API.token } });
    if (!resp.ok) { alert('Не удалось выгрузить (' + resp.status + ')'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await resp.blob());
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // ================= группы и задания =================
  async function loadGroups() {
    $('g-err').textContent = '';
    try {
      [G.groups, G.students, G.packs] = await Promise.all([API.call('GET', '/api/groups'), API.call('GET', '/api/students'),
        API.call('GET', '/api/packs')]);
    } catch (e) { return fail('g-err', e); }
    if (G.sel && !G.groups.some((g) => g.id === G.sel)) G.sel = null;
    $('g-list').innerHTML = G.groups.length ? G.groups.map((g) => '<button type="button" class="lesson-item' + (g.id === G.sel ? ' sel' : '') +
      '" data-group="' + g.id + '"><b>' + esc(g.name) + '</b><span>обучающихся: ' + g.members.length + '</span></button>').join('')
      : '<div class="none">Групп пока нет.</div>';
    $('t-pack').innerHTML = G.packs.map((p) => '<option value="' + esc(p.id) + '">' + esc(p.title) + ' — ' +
      (p.mode === 'cards' ? 'оператор 112' : esc(p.role_title)) + '</option>').join('');
    packChanged();
    renderGroup();
  }

  function packChanged() {
    const p = G.packs.find((x) => x.id === $('t-pack').value);
    const cards = p && p.mode === 'cards';
    $('t-cards-opts').hidden = !cards;
    $('t-pnorm-box').hidden = !!cards;
  }

  function renderGroup() {
    const g = G.groups.find((x) => x.id === G.sel);
    $('g-empty').hidden = !!g;
    $('g-view').hidden = !g;
    if (!g) return;
    $('g-title').textContent = g.name;
    $('g-meta').textContent = 'обучающихся: ' + g.members.length + ' · задания группы — ниже';
    const inGroup = new Set(g.members.map((m) => m.id));
    const q = $('g-filter').value.trim().toLowerCase();
    $('g-members').innerHTML = G.students.filter((s) => !q || (s.name + ' ' + s.login).toLowerCase().includes(q)).map((s) =>
      '<label><input type="checkbox" value="' + s.id + '"' + (inGroup.has(s.id) ? ' checked' : '') + '> ' + esc(s.name) +
      '<small>' + esc(s.login) + '</small></label>').join('') || '<div class="none">Нет обучающихся — их заводит администратор.</div>';
    loadTasks(g.id);
  }

  async function loadTasks(gid) {
    let tasks;
    try { tasks = (await API.call('GET', '/api/tasks')).filter((t) => t.group_id === gid); } catch (e) { return fail('t-err', e); }
    const g = G.groups.find((x) => x.id === gid);
    $('t-list').innerHTML = tasks.length ? tasks.map((t) => {
      const rows = (g ? g.members : []).map((m) => {
        const mine = t.attempts.filter((a) => a.student_id === m.id);
        const best = mine.length ? Math.max(...mine.map((a) => a.score)) : null;
        const passed = mine.some((a) => a.passed);
        return '<tr><td>' + esc(m.name) + '</td><td>' + (mine.length || '—') + '</td><td>' + (best ?? '—') + '</td><td>' +
          (mine.length ? '<span class="pill ' + (passed ? 'ok' : 'no') + '">' + (passed ? 'зачтено' : 'не зачтено') + '</span>' : 'не выполнялось') +
          '</td><td>' + mine.map((a) => '<a href="report.html?lesson=' + encodeURIComponent(a.lesson) + '" target="_blank">' + esc(a.lesson) + '</a>').join(', ') + '</td></tr>';
      }).join('');
      return '<div class="task-row"><div class="h"><b>' + esc(t.title) + '</b><span>' + (t.mode === 'cards' ? 'оператор 112' : esc(t.role_title)) +
        (t.difficulty ? ' · ' + LEVEL[t.difficulty] : '') + (t.due_at ? ' · срок ' + Clock.dmy(t.due_at) : '') + ' · порог ' + (t.config.pass_score ?? 70) +
        '</span><span class="spacer"></span><button type="button" class="danger" data-task-archive="' + t.id + '">Снять задание</button></div>' +
        (t.note ? '<div class="note">' + esc(t.note) + '</div>' : '') +
        '<table><thead><tr><th>Обучающийся</th><th>Попыток</th><th>Лучший балл</th><th>Итог</th><th>Отчёты</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    }).join('') : '<div class="none">Заданий нет — назначьте сценарий группе.</div>';
  }

  $('g-add').addEventListener('click', async () => {
    const name = $('g-new').value.trim();
    if (!name) return;
    try {
      const g = await API.call('POST', '/api/groups', { name });
      $('g-new').value = '';
      G.sel = g.id;
      await loadGroups();
    } catch (e) { fail('g-err', e); }
  });
  $('g-list').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-group]');
    if (!b) return;
    G.sel = Number(b.dataset.group);
    $('g-filter').value = '';
    loadGroups();
  });
  $('g-filter').addEventListener('input', renderGroup);
  $('g-save').addEventListener('click', async () => {
    const g = G.groups.find((x) => x.id === G.sel);
    const shown = new Set([...document.querySelectorAll('#g-members input')].map((x) => Number(x.value)));
    const kept = g.members.map((m) => m.id).filter((id) => !shown.has(id)); // скрытые фильтром — не трогаем
    const members = kept.concat([...document.querySelectorAll('#g-members input:checked')].map((x) => Number(x.value)));
    try { await API.call('PATCH', '/api/groups/' + G.sel, { members }); await loadGroups(); } catch (e) { fail('g-err', e); }
  });
  $('g-archive').addEventListener('click', async () => {
    if (!confirm('Убрать группу в архив? Её задания перестанут показываться обучающимся.')) return;
    try { await API.call('DELETE', '/api/groups/' + G.sel); G.sel = null; await loadGroups(); } catch (e) { fail('g-err', e); }
  });
  $('t-pack').addEventListener('change', packChanged);
  $('t-add').addEventListener('click', async () => {
    $('t-err').textContent = '';
    const p = G.packs.find((x) => x.id === $('t-pack').value);
    if (!p) return;
    const lesson = { pass_score: Number($('t-pass').value) || 70 };
    if (p.mode === 'cards') Object.assign(lesson, { count: Number($('t-count').value) || 8, norm_s: Number($('t-norm').value) || 80 });
    else lesson.primary_norm_s = Number($('t-pnorm').value) || 30;
    const due = $('t-due').value ? new Date($('t-due').value + 'T23:59:00').getTime() / 1000 : null;
    try {
      await API.call('POST', '/api/tasks', { group_id: G.sel, pack_id: p.id, title: $('t-title').value.trim() || null,
        difficulty: $('t-level').value ? Number($('t-level').value) : null, due_at: due, note: $('t-note').value, lesson });
      $('t-title').value = '';
      $('t-note').value = '';
      loadTasks(G.sel);
    } catch (e) { fail('t-err', e); }
  });
  $('t-list').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-task-archive]');
    if (!b || !confirm('Снять задание? Результаты выполненных попыток сохранятся.')) return;
    try { await API.call('DELETE', '/api/tasks/' + b.dataset.taskArchive); loadTasks(G.sel); } catch (e) { fail('t-err', e); }
  });
  TeacherView.handlers.groups = loadGroups;

  // ================= аналитика =================
  async function loadAnalyticsGroups() {
    try {
      const groups = await API.call('GET', '/api/groups');
      const keep = $('a-group').value;
      $('a-group').innerHTML = '<option value="">все мои обучающиеся</option>' + groups.map((g) => '<option value="' + g.id + '">' + esc(g.name) + '</option>').join('');
      $('a-group').value = groups.some((g) => String(g.id) === keep) ? keep : '';
    } catch (e) { fail('a-err', e); }
  }

  function query() {
    const q = new URLSearchParams({ threshold: $('a-threshold').value || 70 });
    if ($('a-group').value) q.set('group_id', $('a-group').value);
    return q.toString();
  }

  async function loadAnalytics() {
    $('a-err').textContent = '';
    $('a-body').innerHTML = '<p class="note">Считаем…</p>';
    let a;
    try { a = await API.call('GET', '/api/analytics?' + query()); } catch (e) { $('a-body').innerHTML = ''; return fail('a-err', e); }
    const withData = a.students.filter((s) => s.history.length);
    const last = withData.map((s) => s.history[s.history.length - 1].score);
    const bt = a.backtest || {};
    let h = '<div class="kpis">' +
      kpi(withData.length + ' из ' + a.students.length, 'обучающихся с результатами') +
      kpi(last.length ? Math.round(last.reduce((x, y) => x + y, 0) / last.length) : '—', 'средний последний балл') +
      kpi(withData.filter((s) => s.forecast && s.forecast.p_pass >= 0.5).length + ' из ' + withData.length, 'по прогнозу сдадут следующее занятие') +
      kpi(bt.n ? num(bt.mae) + ' балла' : '—', bt.n ? 'ошибка прогноза на истории (' + bt.n + ' пров.); «как в прошлый раз» — ' + num(bt.mae_naive) : 'ошибка прогноза: нужно 2+ занятия') +
      kpi(num(a.elapsed_ms) + ' мс', 'время расчёта (ТЗ: не более 30 с)') + '</div>';
    h += '<div class="a-grid"><section class="panel"><h2>Инсайты по группе</h2>' + (a.insights.length ? '<ul class="insights">' +
      a.insights.map((i) => '<li class="' + esc(i.severity) + '">' + esc(i.text) + '</li>').join('') + '</ul>' : '<p class="note">Данных пока мало.</p>') +
      '</section><section class="panel"><h2>Рекомендации группе</h2>' + (a.recommendations.length ? '<ul class="recs">' + a.recommendations.map((r, i) =>
        '<li><b>' + esc(r.mode + ' · №' + r.category + '. ' + r.title) + '</b><span class="who">у ' + r.students + ' из ' + r.of + ' обучающихся</span><br>' +
        esc(r.text) + (r.preset ? '<br><button type="button" data-preset="' + i + '">Сгенерировать тренировку (' +
          (r.preset.mode === 'cards' ? 'оператор 112' : 'ДДС') + ', ' + LEVEL[r.preset.difficulty] + ')</button>' : '') + '</li>').join('') + '</ul>'
      : '<p class="note">Типичных ошибок нет.</p>') + '</section></div>';
    for (const mode of Object.keys(a.categories)) {
      const rows = a.students.filter((s) => s.heat[mode]).map((s) => ({ name: s.name, values: s.heat[mode].per10, counts: s.heat[mode].counts, cards: s.heat[mode].cards }));
      if (rows.length) h += '<section class="panel"><h2>Тепловая карта ошибок — ' + (mode === 'ДДС' ? 'диспетчер ДДС' : 'оператор 112') + '</h2>' +
        Charts.heatmap(rows, a.categories[mode]) + '</section>';
    }
    h += '<section class="panel"><h2>Обучающиеся: результаты, прогноз и его проверка</h2>' + (a.students.map((s) => {
      const f = s.forecast;
      return '<div class="stud"><div><h3>' + esc(s.name) + '</h3>' + (s.history.length ? Charts.progress(s.history, f, a.threshold) : '<p class="note">Результатов нет.</p>') + '</div>' +
        '<div class="fc">' + (f ? 'Прогноз следующего: <b class="big">' + f.next + '</b> (80% интервал ' + f.low + '–' + f.high + ')<br>вероятность зачёта: ' +
          Math.round(f.p_pass * 100) + '% · темп ' + (f.trend > 0 ? '+' : '') + num(f.trend) + ' за занятие' +
          (f.lessons_to_pass > 1 ? ' · порог — примерно через ' + f.lessons_to_pass + ' зан.' : '') + '<br>' +
          (s.backtest.n ? 'на истории: ошибка ' + num(s.backtest.mae) + ' (наивный — ' + num(s.backtest.mae_naive) + '), ' + s.backtest.n + ' пров.' : 'проверка прогноза — со 2-го занятия') : '') +
        (s.recommendations.length ? '<ul class="recs">' + s.recommendations.slice(0, 3).map((r) => '<li><b>' + esc(r.mode + ' · №' + r.category + '. ' + r.title) +
          '</b><span class="who">' + r.count + ' за последние занятия</span><br>' + esc(r.text) + '</li>').join('') + '</ul>' : '') +
        '<div class="fb"><input maxlength="2000" data-fb-text="' + s.id + '" placeholder="Рекомендация обучающемуся (увидит в кабинете)"><button type="button" data-fb="' + s.id + '">Отправить</button></div>' +
        '</div></div>';
    }).join('') || '<p class="note">В группе нет обучающихся.</p>') + '</section>';
    $('a-body').innerHTML = h;
    $('a-body').dataset.recs = JSON.stringify(a.recommendations.map((r) => r.preset));
  }

  function kpi(value, label) { return '<div class="kpi"><b>' + esc(value) + '</b><span>' + esc(label) + '</span></div>'; }

  $('a-load').addEventListener('click', loadAnalytics);
  $('a-group').addEventListener('change', loadAnalytics);
  $('a-xlsx').addEventListener('click', () => download('/api/analytics.xlsx?' + query(), 'uspevaemost.xlsx'));
  $('a-csv').addEventListener('click', () => download('/api/analytics.csv?' + query(), 'uspevaemost.csv'));
  $('a-body').addEventListener('click', async (ev) => {
    const p = ev.target.closest('[data-preset]');
    if (p) { window.ScenarioPreset(JSON.parse($('a-body').dataset.recs)[Number(p.dataset.preset)]); return; }
    const b = ev.target.closest('[data-fb]');
    if (!b) return;
    const input = document.querySelector('[data-fb-text="' + b.dataset.fb + '"]');
    if (!input.value.trim()) return;
    b.disabled = true;
    try {
      await API.call('POST', '/api/students/' + b.dataset.fb + '/feedback', { text: input.value.trim() });
      input.value = '';
      input.placeholder = 'Отправлено — обучающийся увидит в кабинете';
    } catch (e) { fail('a-err', e); } finally { b.disabled = false; }
  });
  TeacherView.handlers.analytics = async () => { await loadAnalyticsGroups(); loadAnalytics(); };

  // ================= материалы =================
  async function loadMaterials() {
    let rows, groups;
    try { [rows, groups] = await Promise.all([API.call('GET', '/api/materials'), API.call('GET', '/api/groups')]); } catch (e) { return fail('m-err', e); }
    const keep = $('m-group').value;
    $('m-group').innerHTML = '<option value="">для всех обучающихся</option>' + groups.map((g) => '<option value="' + g.id + '">группа «' + esc(g.name) + '»</option>').join('');
    $('m-group').value = groups.some((g) => String(g.id) === keep) ? keep : '';
    $('m-list').innerHTML = rows.length ? rows.map((m) => '<div class="mrow"><div class="t"><b>' + esc(m.title) + '</b><span>' + esc(m.filename) + ' · ' +
      Math.max(1, Math.round(m.size / 1024)) + ' КБ · ' + (m.group_name ? 'группа «' + esc(m.group_name) + '»' : 'для всех') + ' · ' + esc(m.author) + ' · ' + Clock.dmy(m.created_at) +
      (m.description ? ' · ' + esc(m.description) : '') + '</span></div><button type="button" data-m-open="' + m.id + '" data-name="' + esc(m.filename) + '">Открыть</button>' +
      (m.uploaded_by === me.id ? '<button type="button" class="danger" data-m-del="' + m.id + '">Удалить</button>' : '') + '</div>').join('')
      : '<div class="none">Материалов пока нет.</div>';
  }

  $('m-upload').addEventListener('click', async () => {
    $('m-err').textContent = '';
    const f = $('m-file').files[0];
    if (!f) { $('m-err').textContent = 'Выберите файл.'; return; }
    const b = $('m-upload');
    b.disabled = true;
    try {
      const resp = await fetch('/api/materials', { method: 'POST', body: f, headers: {
        Authorization: 'Bearer ' + API.token, 'X-Filename': encodeURIComponent(f.name), 'X-Title': encodeURIComponent($('m-title').value.trim()),
        'X-Description': encodeURIComponent($('m-desc').value.trim()), 'X-Group': $('m-group').value } });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw Object.assign(new Error(data.detail || 'ошибка загрузки (' + resp.status + ')'), { status: resp.status });
      $('m-file').value = '';
      $('m-title').value = '';
      $('m-desc').value = '';
      loadMaterials();
    } catch (e) { fail('m-err', e); } finally { b.disabled = false; }
  });
  $('m-list').addEventListener('click', async (ev) => {
    const o = ev.target.closest('[data-m-open]');
    if (o) {
      const resp = await fetch('/api/materials/' + o.dataset.mOpen + '/file', { headers: { Authorization: 'Bearer ' + API.token } });
      if (!resp.ok) { alert('Файл недоступен (' + resp.status + ')'); return; }
      const url = URL.createObjectURL(await resp.blob());
      if (/^(application\/pdf|image\/|audio\/|video\/|text\/plain)/.test(resp.headers.get('content-type') || '')) window.open(url, '_blank');
      else { const a = document.createElement('a'); a.href = url; a.download = o.dataset.name; document.body.appendChild(a); a.click(); a.remove(); }
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      return;
    }
    const d = ev.target.closest('[data-m-del]');
    if (!d || !confirm('Удалить материал? Обучающиеся перестанут его видеть.')) return;
    try { await API.call('DELETE', '/api/materials/' + d.dataset.mDel); loadMaterials(); } catch (e) { fail('m-err', e); }
  });
  TeacherView.handlers.materials = loadMaterials;
})();
