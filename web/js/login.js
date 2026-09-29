'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const form = $('login');
  const err = $('err');
  const go = $('go');
  let pendingOld = null; // временный пароль — нужен для его смены

  const HOME = { admin: 'admin.html', teacher: 'teacher.html', student: 'cabinet.html' };

  function next(user) {
    location.replace(HOME[user.role] || 'index.html');
  }

  if (location.hash === '#expired') {
    err.textContent = 'Сеанс завершён — войдите снова.';
    history.replaceState(null, '', location.pathname);
  }

  // Уже вошли в этой вкладке — сразу на рабочий экран (сервер проверит токен).
  if (API.token && API.user) {
    API.call('GET', '/api/me').then((u) => { if (!u.must_change) next(u); }).catch(() => API.clear());
  }

  function askNewPassword(oldPassword) {
    pendingOld = oldPassword;
    $('step-login').hidden = true;
    $('step-change').hidden = false;
    go.textContent = 'СМЕНИТЬ ПАРОЛЬ';
    $('new-password').focus();
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    err.textContent = '';
    go.disabled = true;
    try {
      if (pendingOld === null) {
        const password = $('password').value;
        const res = await API.call('POST', '/api/login', { login: $('login-name').value, password });
        API.save(res.token, res.user);
        if (res.user.must_change) askNewPassword(password);
        else next(res.user);
      } else {
        const a = $('new-password').value;
        if (a !== $('new-password2').value) throw new Error('пароли не совпадают');
        const user = await API.call('POST', '/api/password', { old: pendingOld, new: a });
        API.save(API.token, user);
        next(user);
      }
    } catch (e) {
      err.textContent = e.message;
    } finally {
      go.disabled = false;
    }
  });
})();
