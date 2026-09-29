// Вкладка «Сценарии» экрана преподавателя (узел 4, ТЗ «Настройка учебной среды»):
// генерация по категориям и сложности → проверка эталона (правильные ответы подсвечены) →
// исправление карточки по комментарию → утверждение целиком или частично.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  if (!API.token || !API.user || API.user.role !== 'teacher') return;

  const STATUS = { draft: 'черновик', approved: 'утверждён', archived: 'в архиве' };
  const LEVEL_HELP = {
    card_actions: {
      1: 'Базовая: в основном профильные карточки, изредка дубль или дом чужой управляющей компании.',
      2: 'Средняя: половина карточек — «ловушки» из памятки: чужая УК, чужой лифт, территория ГБУ «Автодороги», электрощитовая ОЭК, дубль.',
      3: 'Высокая: больше ловушек, отказ от работ после принятия, карточки идут чаще (раз в 45 секунд).',
    },
    cards: {
      1: 'Базовая: заявитель сразу рассказывает, что случилось, и называет точный адрес.',
      2: 'Средняя: заявитель сначала называет ориентир — точный адрес нужно уточнить.',
      3: 'Высокая: заявитель взволнован, суть происшествия и адрес нужно выспрашивать.',
    },
  };
  const S = { options: null, list: [], sel: null, data: null, busy: false, note: '' };
  function cards(n) { // 1 карточка, 2 карточки, 5 карточек
    const d = n % 10, h = n % 100;
    return n + ' ' + (d === 1 && h !== 11 ? 'карточка' : d >= 2 && d <= 4 && (h < 12 || h > 14) ? 'карточки' : 'карточек');
  }
  const me = API.user;

  function fail(e) {
    if (e.status === 401) { API.clear(); location.replace('index.html#expired'); return; }
    $('sc-err').textContent = e.message;
  }

  // ---------- вкладки ----------
  TeacherView.handlers.scenarios = () => { loadOptions(); loadList(); };

  // Рекомендация аналитики → готовые параметры тренировки в форме генерации (узел 6).
  window.ScenarioPreset = async (preset) => {
    TeacherView.show('scenarios');
    await loadOptions();
    $('g-mode').value = preset.mode;
    modeChanged();
    $('g-level').value = String(preset.difficulty || 1);
    levelHelp();
    S.note = 'Параметры тренировки подставлены из рекомендации аналитики — проверьте район и категории и нажмите «Сгенерировать».';
    $('sc-note').hidden = false;
    $('sc-note').textContent = S.note;
    $('sc-empty').hidden = true;
  };

  // ---------- форма генерации ----------
  async function loadOptions() {
    if (S.options) return undefined;
    try { S.options = await API.call('GET', '/api/scenarios/options'); } catch (e) { fail(e); return; }
    $('g-district').innerHTML = S.options.districts.map((d) =>
      '<option value="' + esc(d.name) + '">' + esc(d.name) + ' (' + esc(d.okrug) + ')</option>').join('');
    $('g-level').innerHTML = S.options.levels.map((l) => '<option value="' + l.level + '">' + esc(l.title) + '</option>').join('');
    modeChanged();
  }

  function modeChanged() {
    const mode = $('g-mode').value;
    const dds = mode === 'card_actions';
    $('g-district-box').hidden = !dds;
    const groups = dds ? S.options.dds_groups : S.options.call_groups;
    $('g-groups').innerHTML = groups.map((g) => '<label><input type="checkbox" value="' + esc(g.group) + '"> ' +
      esc(g.title) + '<small>гр. ' + esc(g.group) + '</small></label>').join('');
    $('g-count').min = dds ? 3 : 1;
    $('g-count').max = dds ? 30 : 50;
    $('g-count').value = dds ? 10 : 8;
    levelHelp();
  }

  function levelHelp() {
    const llm = S.options && S.options.llm ? ' Комментарии, которые правила не поймут, перепишет локальная языковая модель.' : '';
    $('g-help').textContent = (LEVEL_HELP[$('g-mode').value][$('g-level').value] || '') +
      ' Эталон — правильные действия — система формирует сама, вы его проверяете и утверждаете.' + llm;
  }

  $('g-import').addEventListener('change', async () => {
    const f = $('g-import').files[0];
    if (!f) return;
    $('g-err').textContent = '';
    try {
      const raw = JSON.parse(await f.text());
      S.data = await API.call('POST', '/api/scenarios/import', raw);
      S.sel = S.data.id;
      S.note = 'Сценарий из файла «' + f.name + '» загружен черновиком: ' + cards(S.data.cards) + '. Проверьте эталон и утвердите.';
      await loadList();
      render();
    } catch (e) {
      if (e.status === 401) fail(e); else $('g-err').textContent = e instanceof SyntaxError ? 'Файл — не JSON: ' + e.message : e.message;
    } finally { $('g-import').value = ''; }
  });
  $('g-mode').addEventListener('change', modeChanged);
  $('g-level').addEventListener('change', levelHelp);
  $('g-go').addEventListener('click', async () => {
    $('g-err').textContent = '';
    const body = {
      mode: $('g-mode').value, difficulty: Number($('g-level').value), count: Number($('g-count').value) || 10,
      groups: [...document.querySelectorAll('#g-groups input:checked')].map((x) => x.value),
    };
    if (body.mode === 'card_actions') body.district = $('g-district').value;
    $('g-go').disabled = true;
    try {
      S.data = await API.call('POST', '/api/scenarios/generate', body);
      S.sel = S.data.id;
      S.note = 'Сценарий сгенерирован: ' + cards(S.data.cards) + '. Проверьте эталон, при необходимости исправьте карточки и утвердите.';
      await loadList();
      render();
    } catch (e) {
      if (e.status === 401) fail(e); else $('g-err').textContent = e.message;
    } finally { $('g-go').disabled = false; }
  });

  // ---------- список ----------
  async function loadList() {
    try { S.list = await API.call('GET', '/api/scenarios'); } catch (e) { fail(e); return; }
    $('sc-list').innerHTML = S.list.length ? S.list.map((x) =>
      '<button type="button" class="lesson-item' + (x.id === S.sel ? ' sel' : '') + '" data-sc="' + x.id + '"><b>' +
      esc(x.title) + '</b><span><i class="st st-' + esc(x.status) + '">' + esc(STATUS[x.status] || x.status) + '</i> · ' +
      (x.mode === 'cards' ? 'оператор 112' : esc(x.role_title)) + ' · карточек: ' + x.cards +
      (x.author_id === me.id ? '' : ' · автор: ' + esc(x.author || '—')) + '</span></button>').join('')
      : '<div class="none">Пока нет сгенерированных сценариев.</div>';
  }

  $('sc-list').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-sc]');
    if (!b) return;
    S.sel = Number(b.dataset.sc);
    S.note = '';
    try { S.data = await API.call('GET', '/api/scenarios/' + S.sel); } catch (e) { fail(e); return; }
    await loadList();
    render();
  });

  // ---------- просмотр сценария ----------
  function render() {
    const d = S.data;
    $('sc-empty').hidden = !!d;
    $('sc').hidden = !d;
    if (!d) return;
    $('sc-err').textContent = '';
    const own = d.author_id === me.id;
    const draft = d.status === 'draft' && own;
    $('sc-title').value = d.title;
    $('sc-title').readOnly = !draft;
    $('sc-meta').innerHTML = '<span class="badge st-' + esc(d.status) + '">' + esc(STATUS[d.status] || d.status) + '</span>' +
      (d.mode === 'cards' ? 'оператор Службы 112' : esc(d.role_title)) + ' · сложность: ' +
      esc(({ 1: 'базовая', 2: 'средняя', 3: 'высокая' })[d.difficulty] || d.difficulty) + ' · карточек: ' + d.cards +
      ' · ' + esc(d.slug) + (own ? '' : ' · автор: ' + esc(d.author || '—'));
    $('sc-approve').hidden = !draft;
    $('sc-archive').hidden = !own || d.status === 'archived';
    $('sc-note').hidden = !S.note;
    $('sc-note').textContent = S.note;
    $('sc-ref').hidden = !d.reference;
    if (d.reference) {
      $('sc-ref-body').innerHTML = d.reference.sections.map((s) => '<h4>' + esc(s.title) + '</h4><ul>' +
        s.items.map((i) => '<li>' + esc(i) + '</li>').join('') + '</ul>').join('');
    }
    $('sc-items').innerHTML = d.items.map((it) => (it.mode === 'cards' ? callHtml(it, draft) : cardHtml(it, draft))).join('');
  }

  function issuesHtml(it) {
    if (!it.issues.length) return '';
    return '<div class="sc-issues">Проверка орфографии: ' + it.issues.map((i) => '«' + esc(i.word) + '»' +
      (i.suggestions.length ? ' → ' + esc(i.suggestions.join(', ')) : '')).join('; ') + '</div>';
  }

  function footHtml(it, draft) {
    const hist = it.history.length ? '<div class="sc-hist">' + it.history.map((h) => '<div>«' + esc(h.comment) + '» → ' +
      esc(h.changes.join('; ')) + '</div>').join('') + '</div>' : '';
    if (!draft) return hist;
    return hist + '<footer><input class="cmt" data-cmt="' + esc(it.key) + '" maxlength="1000" placeholder="Комментарий: ' +
      (it.mode === 'cards' ? 'сложнее, проще, другой адрес, другой тип, без пострадавших…' : 'сложнее, проще, другой адрес, другой тип, короче, подробнее…') +
      '"><button type="button" data-revise="' + esc(it.key) + '">Исправить по комментарию</button>' +
      '<button type="button" class="danger" data-exclude="' + esc(it.key) + '">Исключить</button></footer>';
  }

  function addr(a) {
    return [a.street, a.house && 'д. ' + a.house, a.entrance && 'подъезд ' + a.entrance, a.flat && 'кв. ' + a.flat]
      .filter(Boolean).join(', ');
  }

  function mmss(s) { return '+' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }

  function cardHtml(it, draft) {
    const e = it.expect;
    const opt = (name) => '<span class="opt' + (e.primary === name ? ' on' : '') + '">' + name + '</span>';
    const facts = (groups) => (groups || []).map((g) => '<span class="fact">' + esc(g.slice(0, 3).join(' / ')) + '</span>').join(' ');
    const steps = it.brigade.length ? '<ol class="steps">' + it.brigade.map((r) => '<li><b>' + esc(r.status) + '</b> через ' +
      r.after + ' с после «Принята» · <i>«' + esc(r.text) + '»</i></li>').join('') + '</ol>' : '';
    return '<article class="sc-card k-' + esc(it.kind || 'file') + '"><header>' +
      '<label><input type="checkbox" data-keep="' + esc(it.key) + '" checked' + (draft ? '' : ' disabled') + '> <b>' + esc(it.key) + '</b></label>' +
      '<span class="t">' + mmss(it.at) + '</span><span class="kind">' + esc(it.kind_title) + '</span>' +
      '<span class="code">' + esc(it.code) + ' · ' + esc(it.type) + '</span></header><div class="sc-body"><div class="what">' +
      '<div><span class="lbl">Адрес</span>' + esc(addr(it.address)) + '</div>' +
      '<div><span class="lbl">Описание</span>' + esc(it.description) + '</div>' +
      '<div><span class="lbl">Заявитель</span>' + esc([it.caller.name, it.caller.status, it.caller.phone].filter(Boolean).join(', ')) + '</div>' +
      (it.flags.length ? '<div><span class="lbl">Признаки</span>' + esc(it.flags.join(', ')) + '</div>' : '') +
      issuesHtml(it) + '</div><div class="gold"><div class="gold-h">Эталон</div>' +
      '<div class="opts">' + opt('Принята') + opt('Не принята') + '</div>' +
      '<div class="why">' + esc(e.why || '') + '</div>' +
      (e.comment_facts ? '<div class="need">В комментарии должно быть: ' + facts(e.comment_facts) + '</div>' : '') +
      (e.sample_comment ? '<div class="sample">Образец: «' + esc(e.sample_comment) + '»</div>' : '') + steps +
      (e.final ? '<div class="final">Итог: <b>' + esc(e.final) + '</b></div>' : '') +
      (e.final_comment_facts ? '<div class="need">В комментарии к итогу: ' + facts(e.final_comment_facts) + '</div>' : '') +
      (e.sample_final_comment ? '<div class="sample">Образец: «' + esc(e.sample_final_comment) + '»</div>' : '') +
      '</div></div>' + footHtml(it, draft) + '</article>';
  }

  function callHtml(it, draft) {
    const d = it.dialogue;
    const yes = (v) => (v === true ? 'да' : v === false ? 'нет' : 'не известно');
    const qs = it.questions.length ? '<ul class="qs">' + it.questions.map((q) => '<li class="' + (q.answer ? 'on' : '') + '">' +
      esc(q.text) + ' — <b>' + (q.answer ? 'да' : 'нет') + '</b></li>').join('') + '</ul>' : '';
    const lvl = ({ 1: 'базовая', 2: 'средняя', 3: 'высокая' })[it.difficulty] || 'из билетов';
    return '<article class="sc-card k-call"><header>' +
      '<label><input type="checkbox" data-keep="' + esc(it.key) + '" checked' + (draft ? '' : ' disabled') + '> <b>' + esc(it.key) + '</b></label>' +
      '<span class="kind">сложность: ' + esc(lvl) + '</span><span class="code">' + esc(it.code) + ' · ' + esc(it.type) + '</span></header>' +
      '<div class="sc-body"><div class="what">' +
      '<div><span class="lbl">Заявитель говорит</span>«' + esc(d.opening) + '»</div>' +
      '<div><span class="lbl">Об адресе</span>«' + esc(d.address) + '»' + (d.address !== d.address_clarified
        ? '<br><small>при уточнении: «' + esc(d.address_clarified) + '»</small>' : '') + '</div>' +
      '<div><span class="lbl">Подробнее</span>«' + esc(d.details) + '»</div>' +
      issuesHtml(it) + '</div><div class="gold"><div class="gold-h">Эталон карточки</div>' +
      '<div class="path">' + it.path.map((p) => '<span class="opt on">' + esc(p) + '</span>').join(' → ') + '</div>' + qs +
      '<div><span class="lbl">Пострадавшие</span><b>' + yes(it.victims) + '</b></div>' +
      '<div><span class="lbl">Заявитель</span>' + esc(it.name || '—') + ' · ' + esc((it.statuses || []).join(', ')) + '</div>' +
      '<div><span class="lbl">Адрес</span>' + esc(addr(it.address)) + '</div>' +
      '<div class="need">В описании должно быть: ' + it.facts.map((g) => '<span class="fact">' + esc(g.slice(0, 3).join(' / ')) +
        '</span>').join(' ') + '</div></div></div>' + footHtml(it, draft) + '</article>';
  }

  // ---------- действия ----------
  async function act(fn, note) {
    if (S.busy) return;
    S.busy = true;
    $('sc-err').textContent = '';
    try {
      const res = await fn();
      if (res) S.data = res;
      S.note = typeof note === 'function' ? note(res) : note || '';
      await loadList();
      render();
    } catch (e) { fail(e); } finally { S.busy = false; }
  }

  $('sc-items').addEventListener('click', (ev) => {
    const rev = ev.target.closest('[data-revise]');
    const exc = ev.target.closest('[data-exclude]');
    if (rev) {
      const key = rev.dataset.revise;
      const input = document.querySelector('[data-cmt="' + CSS.escape(key) + '"]');
      const comment = input ? input.value.trim() : '';
      if (!comment) { $('sc-err').textContent = 'Напишите комментарий к карточке ' + key + '.'; return; }
      act(() => API.call('POST', '/api/scenarios/' + S.sel + '/cards/' + encodeURIComponent(key) + '/revise', { comment }),
        (res) => 'Карточка ' + key + ': ' + res.changes.join('; '));
    } else if (exc) {
      const key = exc.dataset.exclude;
      act(() => API.call('POST', '/api/scenarios/' + S.sel + '/cards/' + encodeURIComponent(key) + '/exclude'),
        'Карточка ' + key + ' исключена из сценария.');
    }
  });
  $('sc-items').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target.matches('.cmt')) {
      const b = document.querySelector('[data-revise="' + CSS.escape(ev.target.dataset.cmt) + '"]');
      if (b) b.click();
    }
  });
  $('sc-approve').addEventListener('click', () => {
    const keys = [...document.querySelectorAll('[data-keep]:checked')].map((x) => x.dataset.keep);
    const total = document.querySelectorAll('[data-keep]').length;
    if (!keys.length) { $('sc-err').textContent = 'Отметьте карточки, эталон которых вы подтверждаете.'; return; }
    act(() => API.call('POST', '/api/scenarios/' + S.sel + '/approve', { keys, title: $('sc-title').value }),
      'Сценарий утверждён' + (keys.length < total ? ' частично: ' + keys.length + ' из ' + cards(total) : '') +
      ' — он появился в списке сценариев на вкладке «Занятия».');
  });
  $('sc-archive').addEventListener('click', () => {
    if (!confirm('Убрать сценарий в архив? Для новых занятий он станет недоступен.')) return;
    act(() => API.call('POST', '/api/scenarios/' + S.sel + '/archive'), 'Сценарий в архиве.');
  });
  $('sc-copy').addEventListener('click', () => act(async () => {
    const res = await API.call('POST', '/api/scenarios/' + S.sel + '/copy');
    S.sel = res.id;
    return res;
  }, 'Создана копия-черновик: её можно править и утвердить отдельно.'));
})();
