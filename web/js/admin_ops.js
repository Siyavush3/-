// Вкладки администратора «Состояние», «Резервные копии», «Журналы», «Статистика», «Настройки» (узел 7).
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  if (!API.token || !API.user || API.user.role !== 'admin') return;
  const guarded = (...args) => window.AdminGuarded(...args);
  const MARK = { ok: 'в порядке', warn: 'требует внимания', fail: 'неисправность' };
  const num = (v) => (v === null || v === undefined ? '—' : String(v).replace('.', ','));
  const when = (t) => (t ? Clock.dmy(t) + ' ' + Clock.hms(t) : '—');
  const mb = (bytes) => (bytes < 1048576 ? Math.max(1, Math.round(bytes / 1024)) + ' КБ' : num(Math.round(bytes / 1048576 * 10) / 10) + ' МБ');
  const CHOICE = { background: 'фоном: 1 ядро, низкий приоритет', install: 'все ядра (когда занятий нет)' };
  let statusTimer = null;

  function fail(where, e) {
    if (e.status === 401) { API.clear(); location.replace('index.html#expired'); return; }
    $(where).textContent = e.message;
  }

  async function download(path, name) {
    const resp = await fetch(path, { headers: { Authorization: 'Bearer ' + API.token } });
    if (!resp.ok) { alert('Не удалось скачать (' + resp.status + ')'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await resp.blob());
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  // ================= состояние =================
  async function loadStatus() {
    let s, hist, svc;
    try {
      [s, hist, svc] = await Promise.all([API.call('GET', '/api/admin/status'), API.call('GET', '/api/admin/history'),
        API.call('GET', '/api/admin/services')]);
    } catch (e) { return fail('svc-err', e); }
    Clock.sync(s.server_time);
    const up = s.system.uptime_s;
    $('health').className = 'health ' + s.health;
    $('health').innerHTML = '<b>Система: ' + MARK[s.health] + '</b><span>версия ' + esc(s.version) + ' · работает ' +
      Math.floor(up / 3600) + ' ч ' + Math.floor(up % 3600 / 60) + ' мин · обновлено ' + Clock.hms(s.server_time) + ' (каждые 5 с)</span>';
    $('comps').innerHTML = s.components.map((c) => '<div class="comp ' + c.status + '"><b>' + esc(c.name) + '</b><span>' + esc(c.detail) + '</span></div>').join('');
    const sy = s.system, rq = s.requests, ls = s.lessons;
    $('load').innerHTML = [
      [num(sy.cpu_percent) + '%', 'процессор (' + sy.cpu_count + ' лог. ядер)'], [num(sy.memory_used_percent) + '%', 'память из ' + num(Math.round(sy.memory_total_mb / 102.4) / 10) + ' ГБ'],
      [num(sy.process_rss_mb) + ' МБ', 'память сервера, потоков ' + sy.process_threads], [num(sy.disk_free_gb) + ' ГБ', 'свободно на диске'],
      [rq.rpm, 'запросов за минуту, ошибок ' + rq.errors], [rq.p95_ms === null ? '—' : num(rq.p95_ms) + ' мс', '95% запросов быстрее (ТЗ: отклик ≤ 2 с)'],
      [ls.running + ' / ' + ls.limit, 'идёт занятий (предел), набор — ' + ls.recruiting], [ls.participants + ' · ' + ls.connections, 'участников · подключений'],
      [s.tts.pending, 'фраз ждут озвучки, озвучено ' + s.tts.rendered],
    ].map(([v, l]) => '<div class="kpi"><b>' + esc(v) + '</b><span>' + esc(l) + '</span></div>').join('');
    $('load-note').textContent = hist.length ? '· история за ' + Math.max(1, Math.round((hist[hist.length - 1].at - hist[0].at) / 60)) + ' мин, замер раз в 10 с' : '· история появится через 10 с';
    $('sparks').innerHTML = [['cpu', 'Процессор, %', 100, '#157dbd'], ['mem', 'Память, %', 100, '#8e44ad'], ['rss', 'Память сервера, МБ', 0, '#1f7a4a'],
      ['rpm', 'Запросов в минуту', 0, '#e67e22'], ['p95', 'Время ответа (95%), мс', 0, '#c0392b'], ['lessons', 'Идёт занятий', 0, '#2c3e50']]
      .map(([key, label, max, color]) => '<div><h3>' + label + '</h3>' + Charts.series(hist, { key, max, color, label }) + '</div>').join('');
    $('services').innerHTML = svc.map((x) => '<div class="svc"><span class="pill ' + (x.on ? 'ok' : 'no') + '">' + (x.on ? 'работает' : 'остановлен') + '</span>' +
      '<div class="t"><b>' + esc(x.title) + '</b><span>' + esc(x.detail) + '</span></div>' +
      (x.fixed ? '' : '<button type="button" data-svc="' + x.id + '" data-act="' + (x.on ? 'stop' : 'start') + '">' + (x.on ? 'Остановить' : 'Запустить') + '</button>') + '</div>').join('');
  }

  $('services').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-svc]');
    if (!b) return;
    if (b.dataset.svc === 'maintenance' && b.dataset.act === 'start' &&
      !confirm('Включить режим обслуживания? Обучающиеся не смогут войти, новые занятия не откроются; идущие занятия продолжатся.')) return;
    b.disabled = true;
    try { await guarded('POST', '/api/admin/services/' + b.dataset.svc, { action: b.dataset.act }); await loadStatus(); } catch (e) { fail('svc-err', e); } finally { b.disabled = false; }
  });

  // ================= резервные копии =================
  async function loadBackups() {
    let r;
    try { r = await API.call('GET', '/api/admin/backups'); } catch (e) { return fail('bk-err', e); }
    $('bk-info').innerHTML = (r.enabled ? 'По расписанию: ежедневно в <b>' + esc(r.time) + '</b>, следующая — ' + when(r.next_at) : '<b>Копирование по расписанию выключено</b> (ТЗ: не реже раза в сутки) — включите в «Настройках»') +
      ' · хранится копий: ' + r.keep + ' · папка: <code>' + esc(r.dir) + '</code>' + (r.error ? '<br><span class="err">Последняя попытка не удалась: ' + esc(r.error) + '</span>' : '');
    $('bk-list').innerHTML = r.items.length ? r.items.map((b) => '<tr><td>' + esc(b.name) + (b.broken ? ' <span class="pill no">повреждена</span>' : '') + '</td><td>' + when(b.created_at) +
      '</td><td>' + esc(b.reason || '—') + (b.by ? ' · ' + esc(b.by) : '') + '</td><td class="n">' + mb(b.size) + '</td><td class="n">' + (b.files ?? '—') +
      (b.materials ? ' (материалов ' + b.materials + ')' : '') + '</td><td><button type="button" class="mini" data-verify="' + esc(b.name) + '">Проверить</button>' +
      '<button type="button" class="mini" data-get="' + esc(b.name) + '">Скачать</button><span class="note" id="v-' + esc(b.name) + '"></span></td></tr>').join('')
      : '<tr><td colspan="6" class="none">Копий пока нет.</td></tr>';
  }

  $('bk-now').addEventListener('click', async () => {
    const b = $('bk-now');
    b.disabled = true;
    $('bk-msg').textContent = 'Создаём копию…';
    $('bk-err').textContent = '';
    try {
      const item = await API.call('POST', '/api/admin/backups');
      $('bk-msg').textContent = 'Готово: ' + item.name + ', ' + mb(item.size);
      await loadBackups();
    } catch (e) { $('bk-msg').textContent = ''; fail('bk-err', e); } finally { b.disabled = false; }
  });
  $('bk-list').addEventListener('click', async (ev) => {
    const v = ev.target.closest('[data-verify]');
    const g = ev.target.closest('[data-get]');
    if (g) { download('/api/admin/backups/' + encodeURIComponent(g.dataset.get) + '/download', g.dataset.get); return; }
    if (!v) return;
    const out = document.getElementById('v-' + v.dataset.verify);
    out.textContent = ' проверяем…';
    try {
      const r = await API.call('POST', '/api/admin/backups/' + encodeURIComponent(v.dataset.verify) + '/verify');
      out.textContent = r.ok ? ' цела: файлов ' + r.checked : ' ПОВРЕЖДЕНА: ' + r.bad.join(', ');
      out.className = r.ok ? 'verify ok' : 'verify bad';
    } catch (e) { out.textContent = ' ' + e.message; }
  });

  // ================= журналы =================
  async function loadLogs() {
    const q = new URLSearchParams({ level: $('log-level').value, q: $('log-q').value.trim(), limit: 300 });
    let rows, errs;
    try { [rows, errs] = await Promise.all([API.call('GET', '/api/admin/logs?' + q), API.call('GET', '/api/admin/errors')]); } catch (e) { return; }
    const skip = new Set(['at', 'level', 'logger', 'msg']);
    $('log-list').innerHTML = rows.length ? rows.map((r) => '<tr><td>' + when(r.at) + '</td><td class="lv-' + esc(r.level) + '">' + esc(r.level) + '</td><td>' + esc(r.msg) +
      '</td><td class="det">' + esc(Object.entries(r).filter(([k]) => !skip.has(k)).map(([k, v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v)).join(' ')) + '</td></tr>').join('')
      : '<tr><td colspan="4" class="none">Записей нет.</td></tr>';
    $('err-list').innerHTML = errs.length ? errs.slice(0, 200).map((e) => '<tr><td>' + when(e.at) + '</td><td>' + esc(e.source) + '</td><td>' + esc(e.message) + '</td></tr>').join('')
      : '<tr><td colspan="3" class="none">Сбоев не было.</td></tr>';
  }
  $('log-form').addEventListener('submit', (ev) => { ev.preventDefault(); loadLogs(); });
  $('err-csv').addEventListener('click', () => download('/api/admin/errors.csv', 'errors.csv'));

  // ================= статистика =================
  async function loadStats() {
    let s;
    try { s = await API.call('GET', '/api/admin/stats?days=14'); } catch (e) { return; }
    const t = s.totals;
    const roles = t.users || {};
    $('st-totals').innerHTML = [[(roles.student || 0) + ' / ' + (roles.teacher || 0) + ' / ' + (roles.admin || 0), 'обучающихся / преподавателей / администраторов'],
      [t.blocked, 'заблокировано учётных записей'], [t.lessons, 'занятий всего, прервано — ' + t.interrupted], [t.results, 'результатов обучающихся'],
      [(t.scenarios.approved || 0) + ' / ' + (t.scenarios.draft || 0), 'сценариев утверждено / черновиков'], [t.groups + ' / ' + t.tasks, 'групп / действующих заданий'],
      [t.materials + ' · ' + num(t.materials_mb) + ' МБ', 'учебных материалов'], [t.audit_records, 'записей в журнале аудита']]
      .map(([v, l]) => '<div class="kpi"><b>' + esc(v) + '</b><span>' + esc(l) + '</span></div>').join('');
    $('st-charts').innerHTML = [['logins', 'Входов в систему по дням', '#157dbd'], ['active_users', 'Активных пользователей по дням', '#8e44ad'],
      ['lessons', 'Проведено занятий по дням', '#1f7a4a'], ['results', 'Результатов обучающихся по дням', '#e67e22'], ['failed', 'Неудачных попыток входа по дням', '#c0392b'],
      ['avg_score', 'Средний балл по дням', '#2c3e50']]
      .map(([key, label, color]) => '<section class="panel"><h2>' + label + '</h2>' + Charts.bars(s.days, { key, color, label }) + '</section>').join('');
    $('st-routes').innerHTML = s.requests_by_route.map((r) => '<tr><td>' + esc(r.route) + '</td><td class="n">' + r.count + '</td></tr>').join('') ||
      '<tr><td class="none">Запросов пока нет.</td></tr>';
  }

  // ================= настройки =================
  let settings = [];
  async function loadSettings() {
    let st, tls;
    try { [settings, st, tls] = await Promise.all([API.call('GET', '/api/admin/settings'), API.call('GET', '/api/admin/status'), API.call('GET', '/api/admin/tls')]); } catch (e) { return fail('set-err', e); }
    let section = '';
    $('set-form').innerHTML = settings.map((x) => {
      let h = '';
      if (x.section !== section) { section = x.section; h += '<h3>' + esc(section) + '</h3>'; }
      let input;
      if (x.type === 'bool') input = '<input type="checkbox" data-key="' + x.key + '"' + (x.value ? ' checked' : '') + '>';
      else if (x.type === 'choice') input = '<select data-key="' + x.key + '">' + x.limits.map((c) => '<option value="' + esc(c) + '"' + (c === x.value ? ' selected' : '') + '>' + esc(CHOICE[c] || c) + '</option>').join('') + '</select>';
      else if (x.type === 'time') input = '<input type="time" data-key="' + x.key + '" value="' + esc(x.value) + '">';
      else input = '<input type="number" data-key="' + x.key + '" value="' + x.value + '" min="' + x.limits[0] + '" max="' + x.limits[1] + '"' + (x.type === 'float' ? ' step="0.5"' : '') + '>';
      return h + '<label>' + (x.type === 'bool' ? input + ' ' + esc(x.title) : esc(x.title) + '<br>' + input) +
        '<span>' + esc([x.hint, x.type !== 'bool' && x.limits && x.type !== 'choice' ? 'от ' + x.limits[0] + ' до ' + x.limits[1] : '', 'по умолчанию: ' + (x.type === 'bool' ? (x.default ? 'да' : 'нет') : x.default)].filter(Boolean).join(' · ')) + '</span></label>';
    }).join('');
    const c = tls.cert;
    $('tls').innerHTML = (tls.https ? '<b>Соединение защищено (HTTPS).</b>' : '<b>Сейчас соединение не защищено (HTTP).</b>') +
      (c ? '<br>Сертификат: ' + esc(c.file) + (c.error ? ' — ошибка: ' + esc(c.error) : ', ' + esc(c.subject) + ', действует ещё ' + c.days_left + ' дн.') : '') +
      '<br>Для класса: <code>python server/manage.py make-cert --host &lt;адрес сервера&gt;</code>, затем запуск с <code>--certfile</code> и <code>--keyfile</code>.';
    $('db-info').innerHTML = 'СУБД: <b>' + esc(st.db.dialect) + '</b><br>Адрес: <code>' + esc(st.db.url) + '</code><br>Версия схемы: ' + st.db.schema_version +
      (st.db.size_mb !== null ? '<br>Размер: ' + num(st.db.size_mb) + ' МБ' : '') + '<br>Журнал: ' + num(st.logs.size_mb) + ' МБ, уровень ' + esc(st.logs.level) +
      '<br>Смена СУБД — переменная <code>A112_DB</code> и перезапуск (инструкция по установке).';
  }

  function valueOf(el) {
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'number') return Number(el.value);
    return el.value;
  }
  $('set-form').addEventListener('input', (ev) => {
    const el = ev.target.closest('[data-key]');
    if (el) el.parentElement.classList.toggle('changed', valueOf(el) !== settings.find((x) => x.key === el.dataset.key).value);
  });
  $('set-save').addEventListener('click', async () => {
    $('set-err').textContent = '';
    const changes = {};
    for (const el of document.querySelectorAll('#set-form [data-key]')) {
      const cur = settings.find((x) => x.key === el.dataset.key);
      if (valueOf(el) !== cur.value) changes[el.dataset.key] = valueOf(el);
    }
    if (!Object.keys(changes).length) { $('set-msg').textContent = 'Изменений нет.'; return; }
    try {
      await guarded('PUT', '/api/admin/settings', { changes });
      $('set-msg').textContent = 'Сохранено и применено: ' + Object.keys(changes).map((k) => settings.find((x) => x.key === k).title).join('; ') + '.';
      await loadSettings();
    } catch (e) { fail('set-err', e); }
  });
  $('int-check').addEventListener('click', async () => {
    $('int-result').textContent = 'Проверяем…';
    try {
      const r = await API.call('POST', '/api/admin/integrity');
      $('int-result').innerHTML = r.ok === null ? esc(r.message) : r.ok ? '<b class="verify ok">Файлы совпадают с эталоном</b> (' + r.checked + ' файлов, ' + num(r.elapsed_s) + ' с)'
        : '<b class="verify bad">Целостность нарушена</b>' + ['changed', 'missing', 'added'].map((k) => r[k].length ? '<br>' + { changed: 'изменены', missing: 'пропали', added: 'лишние' }[k] + ': ' + r[k].slice(0, 15).map(esc).join(', ') : '').join('');
    } catch (e) { $('int-result').textContent = e.message; }
  });
  $('upd-go').addEventListener('click', async () => {
    const f = $('upd-file').files[0];
    if (!f) { $('upd-result').textContent = 'Выберите zip-архив.'; return; }
    $('upd-result').textContent = 'Проверяем и устанавливаем…';
    const send = () => fetch('/api/admin/content', { method: 'POST', body: f, headers: { Authorization: 'Bearer ' + API.token } });
    try {
      let resp = await send();
      if (resp.status === 428) {  // нужен пароль администратора — то же окно подтверждения
        if (!(await window.AdminAskConfirm())) { $('upd-result').textContent = 'Действие отменено.'; return; }
        resp = await send();
      }
      const r = await resp.json();
      if (!resp.ok) throw new Error(r.detail || 'ошибка ' + resp.status);
      $('upd-result').innerHTML = 'Установлено: ' + (r.installed.concat(r.replaced).map(esc).join(', ') || 'ничего') +
        (r.backup ? '<br>Резервная копия перед обновлением: ' + esc(r.backup) : '') +
        (r.rejected.length ? '<br><span class="err">Не приняты: ' + r.rejected.map((x) => esc(x.file + ' — ' + x.error)).join('; ') + '</span>' : '');
    } catch (e) { $('upd-result').textContent = e.message; }
  });

  // ================= вкладки =================
  $('tabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-tab]');
    if (!b) return;
    clearInterval(statusTimer);
    const tab = b.dataset.tab;
    if (tab === 'status') { loadStatus(); statusTimer = setInterval(loadStatus, 5000); }
    if (tab === 'backups') loadBackups();
    if (tab === 'logs') loadLogs();
    if (tab === 'stats') loadStats();
    if (tab === 'settings') loadSettings();
  });
  for (const s of document.querySelectorAll('main > section')) s.hidden = s.id !== 'tab-status';
  loadStatus();
  statusTimer = setInterval(loadStatus, 5000);
})();
