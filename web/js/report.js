// Отчёт о практическом занятии (ТЗ): действия, замечания (ошибки), время заполнения карточки и отклонение
// от норматива, грамматика. PDF — печатью браузера; Excel и CSV — выгрузка с сервера.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const me = API.user;
  if (!API.token || !me) { location.replace('index.html'); return; }
  const lesson = new URLSearchParams(location.search).get('lesson') || '';
  const teacher = me.role === 'teacher';
  const num = (v) => (v === null || v === undefined || v === '' ? '—' : String(v).replace('.', ','));
  const date = (t) => (t ? Clock.dmy(t) + ' ' + Clock.hm(t) : '—');

  async function download(kind) {
    const resp = await fetch('/api/lessons/' + encodeURIComponent(lesson) + '/report.' + kind,
      { headers: { Authorization: 'Bearer ' + API.token } });
    if (!resp.ok) { alert('Не удалось выгрузить отчёт (' + resp.status + ')'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await resp.blob());
    a.download = 'report-' + lesson + '.' + kind;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  function render(r) {
    const L = r.lesson;
    const cards = L.mode === 'карточки';
    const cfg = L.config || {};
    const crit = (r.participants[0] && r.participants[0].criteria) || {};
    document.title = 'Отчёт о занятии ' + L.id;
    const cats = r.categories || [];
    let h = '<h1>Отчёт о практическом занятии ' + esc(L.id) + '</h1><div class="muted">' + esc(L.title || '') + '</div>' +
      '<div class="meta">' +
      '<div><span>Режим:</span> ' + (cards ? '«Карточки» — оператор Службы 112' : '«Действия с карточками» — диспетчер ДДС') + '</div>' +
      '<div><span>Преподаватель:</span> ' + esc(L.teacher || '—') + '</div>' +
      '<div><span>Начато:</span> ' + date(L.started_at) + '</div><div><span>Завершено:</span> ' + date(L.finished_at) + '</div>' +
      '<div><span>Норматив:</span> ' + (cards ? 'заполнение карточки ' + num(cfg.norm_s || (r.participants[0] && r.participants[0].timing && r.participants[0].timing.norm_s)) + ' с'
        : 'первичный статус ' + num(cfg.primary_norm_s || 30) + ' с') + '</div>' +
      '<div><span>Зачёт:</span> от ' + num(crit.pass_score) + ' баллов' + (crit.max_errors !== null && crit.max_errors !== undefined ? ', замечаний не больше ' + crit.max_errors : '') +
      (crit.text_check === false ? ', орфография не учитывается' : '') + '</div>' +
      '<div><span>Отчёт сформирован за:</span> ' + num(r.elapsed_ms) + ' мс</div></div>';

    h += '<h2>Итоги</h2><table><thead><tr><th>Обучающийся</th><th class="n">Итог</th><th class="n">Авто</th><th class="n">Эксперт</th><th>Зачёт</th>' +
      '<th class="n">Замечаний</th><th class="n">' + (cards ? 'Среднее время карточки, с' : 'Средняя задержка статуса, с') + '</th>' +
      '<th class="n">Сверх норматива</th><th class="n">Ошибок в тексте</th></tr></thead><tbody>' +
      r.participants.map((p) => {
        const t = p.timing || {};
        return '<tr><td>' + esc(p.name) + '</td><td class="n"><b>' + p.score + '</b></td><td class="n">' + p.auto + '</td><td class="n">' + num(p.expert) + '</td>' +
          '<td>' + (p.passed ? 'зачтено' : '<span class="bad">не зачтено</span>') + '</td><td class="n">' + p.errors + '</td>' +
          '<td class="n">' + num(cards ? t.avg_fill_s : t.avg_primary_s) + '</td><td class="n' + (t.over_norm ? ' bad' : '') + '">' + num(t.over_norm) + ' из ' + num(cards ? (p.cards || []).length : t.cards) + '</td>' +
          '<td class="n">' + p.grammar.length + '</td></tr>';
      }).join('') + '</tbody></table>';

    h += '<h2>Замечания по категориям</h2><table><thead><tr><th>Обучающийся</th>' +
      cats.map((c) => '<th class="n" title="' + esc(c.title) + '">№' + c.category + '</th>').join('') + '</tr></thead><tbody>' +
      r.participants.map((p) => '<tr><td>' + esc(p.name) + '</td>' + cats.map((c) => {
        const v = p.by_category[String(c.category)] || 0;
        return '<td class="n' + (v ? ' bad' : '') + '">' + (v || '·') + '</td>';
      }).join('') + '</tr>').join('') + '</tbody></table><div class="small muted">' +
      cats.map((c) => '№' + c.category + ' — ' + esc(c.title)).join('; ') + '</div>';

    for (const p of r.participants) {
      h += '<section class="student"><h2>' + esc(p.name) + ' — ' + p.score + ' баллов, ' + (p.passed ? 'зачтено' : 'не зачтено') + '</h2>';
      if (p.expert_comment) h += '<p><b>Комментарий преподавателя:</b> ' + esc(p.expert_comment) + '</p>';
      if (cards) {
        h += '<table><thead><tr><th>Карточка</th><th>Тип по эталону</th><th class="n">Балл</th><th class="n">Принят через, с</th>' +
          '<th class="n">Заполнена за, с</th><th class="n">Сверх норматива, с</th><th>Замечания</th></tr></thead><tbody>' +
          p.cards.map((c) => '<tr><td>' + esc(c.card) + '</td><td>' + esc(c.type || '—') + '</td><td class="n">' + c.score + '</td>' +
            '<td class="n">' + num(c.answer_s) + '</td><td class="n">' + num(c.fill_s) + '</td><td class="n' + (c.over_s > 0 ? ' bad' : '') + '">' + (c.over_s > 0 ? num(c.over_s) : '—') + '</td>' +
            '<td class="small">' + (c.findings.length ? c.findings.map(esc).join('<br>') : 'нет') + '</td></tr>').join('') + '</tbody></table>';
      } else {
        h += '<table><thead><tr><th>Карточка</th><th class="n">Первичный статус через, с</th><th class="n">Сверх норматива, с</th>' +
          '<th>Действия (через сколько секунд после поступления карточки)</th><th>Замечания</th></tr></thead><tbody>' +
          p.cards.map((c) => '<tr><td>' + esc(c.card) + '</td><td class="n">' + num(c.primary_s) + '</td><td class="n' + (c.over_s ? ' bad' : '') + '">' + num(c.over_s) + '</td>' +
            '<td class="small"><ol class="acts">' + (c.actions.length ? c.actions.map((a) => '<li>+' + num(a.after_s) + ' с — <b>' + esc(a.what) + '</b>' +
              (a.comment ? ': ' + esc(a.comment) : '') + '</li>').join('') : '<li>действий нет</li>') + '</ol></td>' +
            '<td class="small">' + (c.findings.length ? c.findings.map(esc).join('<br>') : 'нет') + '</td></tr>').join('') + '</tbody></table>';
      }
      h += '<h3>Грамматика</h3><p class="small">' + (p.grammar.length ? p.grammar.map(esc).join('; ') : 'Ошибок в тексте нет.') + '</p></section>';
    }
    if (teacher) h += '<div class="sign"><div>Преподаватель: ' + esc(L.teacher || '') + '</div><div>Дата, подпись</div></div>';
    $('sheet').innerHTML = h;
  }

  $('back').addEventListener('click', () => (history.length > 1 ? history.back() : location.replace(teacher ? 'teacher.html' : 'cabinet.html')));
  $('print').addEventListener('click', () => window.print());
  $('csv').hidden = $('xlsx').hidden = !teacher;
  $('csv').addEventListener('click', () => download('csv'));
  $('xlsx').addEventListener('click', () => download('xlsx'));
  API.call('GET', '/api/lessons/' + encodeURIComponent(lesson) + '/report')
    .then(render)
    .catch((e) => {
      if (e.status === 401) { location.replace('index.html#expired'); return; }
      $('status').textContent = e.message;
    });
})();
