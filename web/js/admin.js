// Экран администратора: учётные записи и журнал аудита.
// Изменение учётных записей требует подтверждения паролем (сервер отвечает 428) — окно подтверждения
// появляется само, после него запрос повторяется.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  if (me.role !== 'admin') { location.replace(me.role === 'teacher' ? 'teacher.html' : 'arm.html'); return; }
  $('who').textContent = me.name;

  const ROLE = { admin: 'администратор', teacher: 'преподаватель', student: 'обучающийся' };
  const ACTION = {
    'login.ok': 'Вход', 'login.fail': 'Неудачный вход', 'logout': 'Выход',
    'password.change': 'Смена пароля', 'password.fail': 'Неверный текущий пароль',
    'user.create': 'Создана учётная запись', 'user.update': 'Изменена учётная запись',
    'user.block': 'Блокировка', 'user.unblock': 'Разблокировка', 'user.reset_password': 'Сброс пароля',
    'admin.confirm': 'Подтверждение паролем', 'admin.confirm_fail': 'Неверный пароль подтверждения',
    'access.denied': 'Отказ в доступе', 'audit.verify': 'Проверка журнала',
    'lesson.create': 'Создано занятие', 'lesson.join': 'Вход в занятие', 'lesson.role': 'Назначена роль',
    'lesson.start': 'Старт занятия', 'lesson.finish': 'Завершение занятия',
    'system.lessons_interrupted': 'Занятия прерваны перезапуском сервера',
  };
  let auditBefore = 0;

  function fmt(t) {
    if (!t) return '—';
    const d = new Date(t * 1000);
    return d.toLocaleDateString('ru-RU') + ' ' + d.toLocaleTimeString('ru-RU');
  }

  async function logout(reason) {
    try { await API.call('POST', '/api/logout'); } catch (e) { /* сеанс уже недействителен */ }
    API.clear();
    location.replace(reason === 'expired' ? 'index.html#expired' : 'index.html');
  }

  // ---------- подтверждение паролем ----------
  let confirmWaiter = null;
  function askConfirm() {
    return new Promise((resolve) => {
      confirmWaiter = resolve;
      $('confirm-err').textContent = '';
      $('confirm-password').value = '';
      $('confirm').hidden = false;
      $('confirm-password').focus();
    });
  }
  $('confirm-cancel').addEventListener('click', () => { $('confirm').hidden = true; confirmWaiter && confirmWaiter(false); });
  $('confirm-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      await API.call('POST', '/api/admin/confirm', { password: $('confirm-password').value });
      $('confirm').hidden = true;
      confirmWaiter && confirmWaiter(true);
    } catch (e) {
      $('confirm-err').textContent = e.message;
    }
  });

  async function guarded(method, path, body) {
    try {
      return await API.call(method, path, body);
    } catch (e) {
      if (e.status === 401) { logout('expired'); throw e; }
      if (e.status !== 428) throw e;
      if (!(await askConfirm())) throw new Error('действие отменено');
      return API.call(method, path, body);
    }
  }

  window.AdminGuarded = guarded; // вкладки узла 7 (admin_ops.js) — то же подтверждение паролем
  window.AdminAskConfirm = askConfirm;

  // ---------- учётные записи ----------
  async function loadUsers() {
    let users;
    try {
      users = await API.call('GET', '/api/admin/users');
    } catch (e) {
      if (e.status === 401) return logout('expired');
      $('users-err').textContent = e.message;
      return;
    }
    $('users-count').textContent = '· всего ' + users.length;
    const now = Date.now() / 1000;
    $('users').innerHTML = users.map((u) => {
      const locked = u.locked_until && u.locked_until > now;
      const state = u.blocked ? '<span class="state-blocked">заблокирована</span>'
        : locked ? '<span class="state-locked">вход закрыт до ' + new Date(u.locked_until * 1000).toLocaleTimeString('ru-RU') + '</span>'
          : u.must_change ? 'временный пароль' : 'активна';
      const self = u.id === me.id;
      const roles = Object.keys(ROLE).map((r) => '<option value="' + r + '"' + (r === u.role ? ' selected' : '') + '>' +
        ROLE[r] + '</option>').join('');
      return '<tr><td><b>' + esc(u.login) + '</b></td><td>' + esc(u.name) + '</td>' +
        '<td><select data-role="' + u.id + '"' + (self ? ' disabled' : '') + '>' + roles + '</select></td>' +
        '<td>' + state + '</td><td>' + fmt(u.last_login) + '</td><td class="n">' +
        (self ? '' : '<button type="button" class="mini" data-block="' + u.id + '" data-to="' + (u.blocked ? 0 : 1) + '">' +
          (u.blocked ? 'Разблокировать' : 'Заблокировать') + '</button>') +
        '<button type="button" class="mini" data-reset="' + u.id + '" data-login="' + esc(u.login) + '">Сбросить пароль</button></td></tr>';
    }).join('');
  }

  function showSecret(login, password) {
    $('secret').hidden = false;
    $('secret').innerHTML = 'Временный пароль для <b>' + esc(login) + '</b>: <code>' + esc(password) +
      '</code> — передайте пользователю; при первом входе он сменит пароль. Больше этот пароль не показывается.';
  }

  $('create-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    $('users-err').textContent = '';
    try {
      const res = await guarded('POST', '/api/admin/users',
        { login: $('new-login').value, name: $('new-name').value, role: $('new-role').value });
      showSecret(res.user.login, res.temporary_password);
      $('new-login').value = '';
      $('new-name').value = '';
      loadUsers();
    } catch (e) { $('users-err').textContent = e.message; }
  });

  $('users').addEventListener('click', async (ev) => {
    const block = ev.target.closest('[data-block]');
    const reset = ev.target.closest('[data-reset]');
    $('users-err').textContent = '';
    try {
      if (block) {
        await guarded('PATCH', '/api/admin/users/' + block.dataset.block, { blocked: block.dataset.to === '1' });
        loadUsers();
      } else if (reset) {
        if (!confirm('Сбросить пароль пользователя ' + reset.dataset.login + '? Его текущие сеансы будут завершены.')) return;
        const res = await guarded('POST', '/api/admin/users/' + reset.dataset.reset + '/reset-password');
        showSecret(reset.dataset.login, res.temporary_password);
        loadUsers();
      }
    } catch (e) { $('users-err').textContent = e.message; }
  });

  $('users').addEventListener('change', async (ev) => {
    const sel = ev.target.closest('[data-role]');
    if (!sel) return;
    try {
      await guarded('PATCH', '/api/admin/users/' + sel.dataset.role, { role: sel.value });
    } catch (e) { $('users-err').textContent = e.message; }
    loadUsers();
  });

  // ---------- аудит ----------
  async function loadAudit(more) {
    if (!more) auditBefore = 0;
    const q = new URLSearchParams({ limit: 100 });
    if ($('f-action').value.trim()) q.set('action', $('f-action').value.trim());
    if ($('f-login').value.trim()) q.set('login', $('f-login').value.trim().toLowerCase());
    if (auditBefore) q.set('before', auditBefore);
    let rows;
    try {
      rows = await API.call('GET', '/api/admin/audit?' + q.toString());
    } catch (e) {
      if (e.status === 401) return logout('expired');
      return;
    }
    const html = rows.map((r) => {
      const det = Object.keys(r.details || {}).length ? JSON.stringify(r.details) : '';
      return '<tr><td>' + fmt(r.at) + '</td><td>' + esc(r.login || '—') + '</td><td>' + esc(ACTION[r.action] || r.action) +
        '</td><td>' + esc(r.object || '') + '</td><td class="det">' + esc(det) + '</td><td>' + esc(r.ip || '') + '</td></tr>';
    }).join('');
    if (more) $('audit').insertAdjacentHTML('beforeend', html); else $('audit').innerHTML = html;
    if (rows.length) auditBefore = rows[rows.length - 1].id;
    $('audit-more').hidden = rows.length < 100;
  }

  $('audit-form').addEventListener('submit', (ev) => { ev.preventDefault(); loadAudit(false); });
  $('audit-more').addEventListener('click', () => loadAudit(true));
  $('verify').addEventListener('click', async () => {
    const out = $('verify-result');
    out.className = 'verify';
    out.textContent = 'проверка…';
    try {
      const r = await API.call('GET', '/api/admin/audit/verify');
      out.className = 'verify ' + (r.ok ? 'ok' : 'bad');
      out.textContent = r.ok ? 'журнал цел: проверено записей — ' + r.checked
        : 'нарушена целостность: запись №' + r.broken_at + ' (до неё проверено ' + r.checked + ')';
    } catch (e) { out.textContent = e.message; }
  });

  // ---------- вкладки ----------
  $('tabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-tab]');
    if (!b) return;
    for (const t of document.querySelectorAll('[data-tab]')) t.classList.toggle('on', t === b);
    for (const s of document.querySelectorAll('main > section')) s.hidden = s.id !== 'tab-' + b.dataset.tab;
    if (b.dataset.tab === 'audit') loadAudit(false);
    if (b.dataset.tab === 'users') loadUsers();
  });
  $('exit').addEventListener('click', () => logout());

  loadUsers();
})();
