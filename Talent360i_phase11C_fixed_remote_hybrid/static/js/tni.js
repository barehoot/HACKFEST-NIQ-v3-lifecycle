// TNI heatmap / calendar tooltips and the learning-curve line chart.
// Every label is inserted with textContent -- skill and course names come
// from the dataset, so they are never parsed as HTML.
(() => {
  const SVG = 'http://www.w3.org/2000/svg';

  // ---- Shared tooltip ----------------------------------------------------
  const tip = document.createElement('div');
  tip.className = 'viz-tip';
  tip.setAttribute('role', 'tooltip');
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(tip));

  function showTip(title, lines, x, y) {
    tip.replaceChildren();
    const strong = document.createElement('b');
    strong.textContent = title;
    tip.appendChild(strong);
    for (const line of lines) {
      if (!line) continue;
      const row = document.createElement('div');
      row.textContent = line;
      tip.appendChild(row);
    }
    tip.classList.add('show');
    const pad = 12;
    const rect = tip.getBoundingClientRect();
    let left = x + pad;
    let top = y + pad;
    if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
    if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }
  const hideTip = () => tip.classList.remove('show');

  function bindTips(root) {
    root.querySelectorAll('[data-tip-title]').forEach((el) => {
      const lines = () => (el.dataset.tip || '').split('\n');
      el.addEventListener('pointermove', (e) => showTip(el.dataset.tipTitle, lines(), e.clientX, e.clientY));
      el.addEventListener('pointerleave', hideTip);
      el.addEventListener('focus', () => {
        const r = el.getBoundingClientRect();
        showTip(el.dataset.tipTitle, lines(), r.right, r.top);
      });
      el.addEventListener('blur', hideTip);
    });
  }

  // ---- Line chart --------------------------------------------------------
  const el = (name, attrs = {}, parent) => {
    const node = document.createElementNS(SVG, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  };

  function niceStep(span) {
    for (const step of [1, 2, 4, 5, 10, 20]) if (span / step <= 8) return step;
    return Math.ceil(span / 8);
  }

  function renderLine(host, data) {
    host.replaceChildren();
    const fmt = (v) => (data.y_format === 'percent' ? `${Math.round(v)}%` : `L${v.toFixed(1)}`);
    const W = Math.max(280, host.clientWidth);
    const H = 260;
    const m = { l: 40, r: 16, t: 18, b: 38 };
    const pts = data.points;
    const xMax = Math.max(1, Math.ceil(pts[pts.length - 1].x));
    const x = (v) => m.l + (v / xMax) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - (v - data.y_min) / (data.y_max - data.y_min)) * (H - m.t - m.b);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': host.dataset.label || 'Learning curve' });
    const grid = el('g', { class: 'grid' }, svg);
    const axis = el('g', { class: 'axis' }, svg);
    for (const t of data.y_ticks) {
      el('line', { x1: m.l, x2: W - m.r, y1: y(t.v), y2: y(t.v) }, grid);
      const label = el('text', { x: m.l - 8, y: y(t.v) + 3, 'text-anchor': 'end' }, axis);
      label.textContent = t.label;
    }
    const step = niceStep(xMax);
    for (let v = 0; v <= xMax; v += step) {
      const label = el('text', { x: x(v), y: H - m.b + 16, 'text-anchor': 'middle' }, axis);
      label.textContent = String(v);
    }
    const xTitle = el('text', { class: 'x-title', x: W - m.r, y: H - 4, 'text-anchor': 'end' }, svg);
    xTitle.textContent = data.x_label;
    el('line', { class: 'baseline', x1: m.l, x2: W - m.r, y1: y(data.y_min), y2: y(data.y_min) }, svg);

    if (data.target) {
      el('line', { class: 'target', x1: m.l, x2: W - m.r, y1: y(data.target.v), y2: y(data.target.v) }, svg);
      const tl = el('text', { class: 'target-label', x: m.l + 6, y: y(data.target.v) - 6 }, svg);
      tl.textContent = data.target.label;
    }

    const line = pts.map((p) => `${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join(' ');
    el('polygon', { class: 'area', points: `${x(0)},${y(data.y_min)} ${line} ${x(pts[pts.length - 1].x)},${y(data.y_min)}` }, svg);
    el('polyline', { class: 'series', points: line }, svg);
    for (const p of [pts[0], pts[pts.length - 1]]) el('circle', { class: 'dot', cx: x(p.x), cy: y(p.y), r: 4 }, svg);

    const last = pts[pts.length - 1];
    const end = el('text', { class: 'end-label', x: x(last.x) - 8, y: y(last.y) - 10, 'text-anchor': 'end' }, svg);
    end.textContent = `${fmt(last.y)} by week ${last.x.toFixed(1).replace(/\.0$/, '')}`;

    const cross = el('line', { class: 'crosshair', y1: m.t, y2: H - m.b, visibility: 'hidden' }, svg);
    const hover = el('circle', { class: 'dot', r: 5, visibility: 'hidden' }, svg);
    host.appendChild(svg);

    let active = -1;
    const show = (i, cx, cy) => {
      active = Math.max(0, Math.min(pts.length - 1, i));
      const p = pts[active];
      for (const node of [cross, hover]) node.setAttribute('visibility', 'visible');
      cross.setAttribute('x1', x(p.x));
      cross.setAttribute('x2', x(p.x));
      hover.setAttribute('cx', x(p.x));
      hover.setAttribute('cy', y(p.y));
      const box = svg.getBoundingClientRect();
      showTip(fmt(p.y), [`Week ${p.x.toFixed(1).replace(/\.0$/, '')}`, p.note],
        cx ?? box.left + x(p.x), cy ?? box.top + y(p.y));
    };
    const hide = () => {
      for (const node of [cross, hover]) node.setAttribute('visibility', 'hidden');
      hideTip();
    };
    svg.addEventListener('pointermove', (e) => {
      const box = svg.getBoundingClientRect();
      const wx = ((e.clientX - box.left) / box.width) * W;
      let best = 0;
      pts.forEach((p, i) => { if (Math.abs(x(p.x) - wx) < Math.abs(x(pts[best].x) - wx)) best = i; });
      show(best, e.clientX, e.clientY);
    });
    svg.addEventListener('pointerleave', hide);
    host.onkeydown = (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        show(active < 0 ? 0 : active + (e.key === 'ArrowRight' ? 1 : -1));
      } else if (e.key === 'Escape') hide();
    };
    host.onblur = hide;
  }

  // Simulation integrity telemetry, sent with each response: characters pasted, time
  // from first interaction to submit (Agent 4's AI-use check), and how the answer
  // reached the box -- characters typed vs inserted some other way, corrections, and
  // pauses over 8 s (services/integrity.typing_signals).
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form.sim-form').forEach((form) => {
      const box = form.querySelector('textarea[name="response"]');
      if (!box) return;
      let started = null;
      let typed = 0, inserted = 0, corrections = 0, pauses = 0, lastInput = null, length = box.value.length;
      const start = () => { if (started === null) started = Date.now(); };
      box.addEventListener('focus', start);
      box.addEventListener('keydown', start);
      box.addEventListener('paste', (e) => {
        start();
        const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
        form.elements.paste_chars.value = String(Number(form.elements.paste_chars.value || 0) + text.length);
        form.elements.paste_events.value = String(Number(form.elements.paste_events.value || 0) + 1);
      });
      box.addEventListener('input', (e) => {
        start();
        const now = Date.now();
        if (lastInput !== null && now - lastInput > 8000) pauses += 1;
        lastInput = now;
        const kind = e.inputType || '';
        const grew = Math.max(box.value.length - length, 0);
        length = box.value.length;
        if (kind.startsWith('delete')) corrections += 1;
        else if (kind === 'insertText' || kind === 'insertCompositionText' || kind === 'insertLineBreak') typed += grew;
        else inserted += grew;  // paste that got through, drop, autocorrect, an extension
      });
      form.addEventListener('submit', () => {
        if (started !== null) form.elements.active_seconds.value = String(Math.round((Date.now() - started) / 1000));
        const set = (name, value) => { if (form.elements[name]) form.elements[name].value = String(value); };
        set('typed_chars', typed); set('inserted_chars', inserted); set('corrections', corrections); set('long_pauses', pauses);
      });
    });
  });

  // Slow AI steps (simulation probes, evidence report): show progress and block double-submits.
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.busyLabel) return;
    // A disabled button is left out of the submitted data, so carry the
    // clicked button's name/value (e.g. decision=approved) in a hidden field.
    const clicked = e.submitter;
    if (clicked && clicked.name) {
      const carry = document.createElement('input');
      carry.type = 'hidden';
      carry.name = clicked.name;
      carry.value = clicked.value;
      form.appendChild(carry);
    }
    const buttons = form.querySelectorAll('button[type="submit"]');
    buttons.forEach((b) => { b.disabled = true; });
    const label = clicked || buttons[0];
    if (label) label.textContent = form.dataset.busyLabel;
  });

  document.addEventListener('DOMContentLoaded', () => {
    bindTips(document);
    document.querySelectorAll('.line-chart[data-source]').forEach((host) => {
      const source = document.getElementById(host.dataset.source);
      if (!source) return;
      const data = JSON.parse(source.textContent);
      if (!data || !data.points || data.points.length < 2) return;
      renderLine(host, data);
      let timer;
      window.addEventListener('resize', () => {
        clearTimeout(timer);
        timer = setTimeout(() => renderLine(host, data), 120);
      });
    });
  });
})();
