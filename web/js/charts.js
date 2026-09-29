// Диаграммы аналитики (узел 6): SVG и таблицы без внешних библиотек — контур изолирован.
// Правила честной диаграммы: шкала баллов всегда от 0 до 100 (без обрезки), подписаны оси и значения,
// порог зачёта — отдельной линией с подписью, прогноз отличается от факта и показан с интервалом.
'use strict';

const Charts = (() => {
  const C = { actual: '#157dbd', predicted: '#8e44ad', forecast: '#e67e22', threshold: '#c0392b', grid: '#dde3e7', ink: '#1d2327', muted: '#5d6a72' };

  // История баллов обучающегося: факт, прогноз на истории (каждый — по предыдущим занятиям), прогноз следующего.
  function progress(history, forecast, threshold, opts) {
    const o = Object.assign({ width: 560, height: 230 }, opts || {});
    const padL = 40, padR = 58, padT = 14, padB = 44;
    const n = history.length + (forecast ? 1 : 0);
    const w = o.width - padL - padR, h = o.height - padT - padB;
    const x = (i) => padL + (n <= 1 ? w / 2 : (i * w) / (n - 1));
    const y = (v) => padT + h - (v / 100) * h;
    let s = '<svg class="chart" viewBox="0 0 ' + o.width + ' ' + o.height + '" role="img" aria-label="Баллы по занятиям и прогноз">';
    for (const v of [0, 25, 50, 75, 100]) {
      s += '<line x1="' + padL + '" x2="' + (padL + w) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + C.grid + '"/>' +
        '<text x="' + (padL - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end" font-size="11" fill="' + C.muted + '">' + v + '</text>';
    }
    s += '<text x="12" y="' + (padT + h / 2) + '" font-size="11" fill="' + C.muted + '" transform="rotate(-90 12 ' + (padT + h / 2) + ')" text-anchor="middle">баллы из 100</text>';
    if (threshold !== undefined && threshold !== null) {
      s += '<line x1="' + padL + '" x2="' + (padL + w) + '" y1="' + y(threshold) + '" y2="' + y(threshold) + '" stroke="' + C.threshold +
        '" stroke-dasharray="6 4"/><text x="' + (padL + w + 6) + '" y="' + (y(threshold) - 2) + '" font-size="11" fill="' + C.threshold + '">порог</text>' +
        '<text x="' + (padL + w + 6) + '" y="' + (y(threshold) + 11) + '" font-size="11" fill="' + C.threshold + '">' + threshold + '</text>';
    }
    history.forEach((p, i) => {
      s += '<text x="' + x(i) + '" y="' + (padT + h + 16) + '" text-anchor="middle" font-size="11" fill="' + C.muted + '">' + esc(p.lesson) + '</text>';
    });
    if (forecast) s += '<text x="' + x(n - 1) + '" y="' + (padT + h + 16) + '" text-anchor="middle" font-size="11" fill="' + C.forecast + '">далее</text>';
    s += '<text x="' + (padL + w / 2) + '" y="' + (o.height - 6) + '" text-anchor="middle" font-size="11" fill="' + C.muted + '">занятия по порядку</text>';
    const pred = history.map((p, i) => [i, p.predicted]).filter((p) => p[1] !== null && p[1] !== undefined);
    if (pred.length > 1) s += '<polyline fill="none" stroke="' + C.predicted + '" stroke-width="1.5" stroke-dasharray="4 3" points="' + pred.map((p) => x(p[0]) + ',' + y(p[1])).join(' ') + '"/>';
    for (const [i, v] of pred) s += '<circle cx="' + x(i) + '" cy="' + y(v) + '" r="4" fill="#fff" stroke="' + C.predicted + '" stroke-width="1.5"><title>прогноз на это занятие: ' + v + '</title></circle>';
    if (history.length > 1) s += '<polyline fill="none" stroke="' + C.actual + '" stroke-width="2.2" points="' + history.map((p, i) => x(i) + ',' + y(p.score)).join(' ') + '"/>';
    history.forEach((p, i) => {
      s += '<circle cx="' + x(i) + '" cy="' + y(p.score) + '" r="4.5" fill="' + (p.passed ? C.actual : '#fff') + '" stroke="' + C.actual + '" stroke-width="2"><title>' +
        esc(p.lesson + ' · ' + (p.title || '') + ': ' + p.score + (p.expert !== null && p.expert !== undefined ? ' (экспертная оценка)' : '')) + '</title></circle>' +
        '<text x="' + x(i) + '" y="' + (y(p.score) - 9) + '" text-anchor="middle" font-size="11" font-weight="600" fill="' + C.ink + '">' + p.score + '</text>';
    });
    if (forecast) {
      const fx = x(n - 1);
      s += '<line x1="' + fx + '" x2="' + fx + '" y1="' + y(forecast.high) + '" y2="' + y(forecast.low) + '" stroke="' + C.forecast + '" stroke-width="6" stroke-opacity=".3" stroke-linecap="round"><title>80% интервал: ' + forecast.low + '–' + forecast.high + '</title></line>' +
        '<rect x="' + (fx - 5) + '" y="' + (y(forecast.next) - 5) + '" width="10" height="10" fill="' + C.forecast + '" transform="rotate(45 ' + fx + ' ' + y(forecast.next) + ')"><title>прогноз: ' + forecast.next + '</title></rect>' +
        '<text x="' + (fx - 9) + '" y="' + (y(forecast.next) + 4) + '" text-anchor="end" font-size="11" font-weight="600" fill="' + C.forecast + '">' + forecast.next + '</text>';
    }
    return s + '</svg>' + legend([
      ['line', C.actual, 'факт (закрашено — зачёт)'], ['dash', C.predicted, 'прогноз на это занятие по предыдущим'],
      ['diamond', C.forecast, 'прогноз следующего, полоса — 80% интервал'], ['dash', C.threshold, 'порог зачёта']]);
  }

  function legend(items) {
    return '<div class="legend">' + items.map(([kind, color, text]) => {
      const mark = kind === 'diamond' ? '<i style="background:' + color + ';transform:rotate(45deg);width:8px;height:8px"></i>'
        : '<i style="border-top:' + (kind === 'dash' ? '2px dashed ' : '3px solid ') + color + ';height:0;width:18px"></i>';
      return '<span>' + mark + esc(text) + '</span>';
    }).join('') + '</div>';
  }

  // Тепловая карта ошибок: строки — обучающиеся, столбцы — категории, в ячейке — ошибок на 10 карточек.
  function heatmap(rows, cols, opts) {
    const o = Object.assign({ unit: 'на 10 карточек' }, opts || {});
    let max = 0;
    for (const r of rows) for (const c of cols) max = Math.max(max, r.values[c.category] || 0);
    const color = (v) => {
      if (!v) return '#f7f9fa';
      const t = Math.min(1, v / (max || 1));
      const l = 94 - t * 50;   // от бледно-жёлтого к насыщенному красному
      return 'hsl(' + (48 - t * 44) + ',85%,' + l + '%)';
    };
    let s = '<table class="heat"><thead><tr><th>Обучающийся</th>' + cols.map((c) =>
      '<th title="' + esc(c.title) + '">№' + c.category + '</th>').join('') + '<th>Карточек</th></tr></thead><tbody>';
    for (const r of rows) {
      s += '<tr><td>' + esc(r.name) + '</td>' + cols.map((c) => {
        const v = r.values[c.category] || 0;
        return '<td style="background:' + color(v) + (v / (max || 1) > 0.6 ? ';color:#fff' : '') + '" title="' + esc(r.name + ' · №' + c.category + ' ' + c.title + ': ' + v + ' ' + o.unit + ' (' + (r.counts[c.category] || 0) + ' всего)') + '">' +
          (v ? String(Math.round(v * 10) / 10).replace('.', ',') : '·') + '</td>';
      }).join('') + '<td class="n">' + r.cards + '</td></tr>';
    }
    s += '</tbody></table><div class="heat-key">' + cols.map((c) => '<span><b>№' + c.category + '</b> ' + esc(c.title) + '</span>').join('') +
      '</div><div class="heat-scale">Ошибок ' + esc(o.unit) + ': <i style="background:' + color(0) + '"></i>0 <i style="background:' + color(max / 2) + '"></i>' +
      String(Math.round(max * 5) / 10).replace('.', ',') + ' <i style="background:' + color(max) + '"></i>' + String(max).replace('.', ',') + '</div>';
    return s;
  }

  // Ряд во времени (мониторинг): ось от нуля, верх — max или наибольшее значение, подписи начала и конца.
  function series(points, o) {
    o = Object.assign({ width: 360, height: 120, color: C.actual, unit: '' }, o || {});
    const padL = 34, padR = 8, padT = 10, padB = 20;
    const w = o.width - padL - padR, h = o.height - padT - padB;
    const vals = points.map((p) => p[o.key]).filter((v) => v !== null && v !== undefined);
    const top = o.max || Math.max(1, ...vals) * 1.15;
    const x = (i) => padL + (points.length <= 1 ? w : (i * w) / (points.length - 1));
    const y = (v) => padT + h - (Math.min(v, top) / top) * h;
    let s = '<svg class="chart spark" viewBox="0 0 ' + o.width + ' ' + o.height + '" role="img" aria-label="' + esc(o.label || '') + '">';
    for (const v of [0, top / 2, top]) {
      s += '<line x1="' + padL + '" x2="' + (padL + w) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="' + C.grid + '"/>' +
        '<text x="' + (padL - 4) + '" y="' + (y(v) + 4) + '" text-anchor="end" font-size="10" fill="' + C.muted + '">' + String(v >= 10 ? Math.round(v) : Math.round(v * 10) / 10).replace('.', ',') + o.unit + '</text>';
    }
    const line = points.map((p, i) => (p[o.key] === null || p[o.key] === undefined ? null : x(i) + ',' + y(p[o.key]))).filter(Boolean);
    if (line.length > 1) s += '<polyline fill="none" stroke="' + o.color + '" stroke-width="1.8" points="' + line.join(' ') + '"/>';
    else if (line.length === 1) s += '<circle cx="' + line[0].split(',')[0] + '" cy="' + line[0].split(',')[1] + '" r="3" fill="' + o.color + '"/>';
    if (points.length) {
      s += '<text x="' + padL + '" y="' + (o.height - 5) + '" font-size="10" fill="' + C.muted + '">' + Clock.hm(points[0].at) + '</text>' +
        '<text x="' + (padL + w) + '" y="' + (o.height - 5) + '" text-anchor="end" font-size="10" fill="' + C.muted + '">' + Clock.hm(points[points.length - 1].at) + '</text>';
    }
    return s + '</svg>';
  }

  // Столбцы по дням (статистика): ось от нуля, значение над столбцом, дата под ним.
  function bars(items, o) {
    o = Object.assign({ width: 420, height: 160, color: C.actual }, o || {});
    const padL = 30, padR = 6, padT = 14, padB = 22;
    const w = o.width - padL - padR, h = o.height - padT - padB;
    const vals = items.map((d) => d[o.key] || 0);
    const top = Math.max(1, ...vals);
    const bw = w / Math.max(1, items.length);
    let s = '<svg class="chart" viewBox="0 0 ' + o.width + ' ' + o.height + '" role="img" aria-label="' + esc(o.label || '') + '">' +
      '<line x1="' + padL + '" x2="' + (padL + w) + '" y1="' + (padT + h) + '" y2="' + (padT + h) + '" stroke="' + C.muted + '"/>' +
      '<text x="' + (padL - 4) + '" y="' + (padT + 4) + '" text-anchor="end" font-size="10" fill="' + C.muted + '">' + top + '</text>' +
      '<text x="' + (padL - 4) + '" y="' + (padT + h) + '" text-anchor="end" font-size="10" fill="' + C.muted + '">0</text>';
    items.forEach((d, i) => {
      const v = d[o.key] || 0;
      const bh = (v / top) * h;
      const bx = padL + i * bw + bw * 0.15;
      s += '<rect x="' + bx + '" y="' + (padT + h - bh) + '" width="' + bw * 0.7 + '" height="' + bh + '" fill="' + o.color + '"><title>' + esc(d.date + ': ' + v) + '</title></rect>';
      if (v) s += '<text x="' + (bx + bw * 0.35) + '" y="' + (padT + h - bh - 3) + '" text-anchor="middle" font-size="11" fill="' + C.ink + '">' + v + '</text>';
      if (items.length <= 8 || i % 2 === 0) s += '<text x="' + (bx + bw * 0.35) + '" y="' + (o.height - 6) + '" text-anchor="middle" font-size="10" fill="' + C.muted + '">' + esc(d.date.slice(8) + '.' + d.date.slice(5, 7)) + '</text>';
    });
    return s + '</svg>';
  }

  return { progress, heatmap, series, bars };
})();
