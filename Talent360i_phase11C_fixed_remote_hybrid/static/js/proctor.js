// Proctored assessment (templates/partials/proctor.html). A page cannot stop someone
// leaving it, so this makes the rules explicit, keeps the assessment in full screen,
// blocks copy and paste, notices every exit and second display, and reports each to
// /employee/integrity/<cycle>. The server (services/integrity.py) decides what is a
// strike, answers with the count, and ends the attempt at the limit.
(() => {
  const root = document.getElementById('proctor');
  if (!root) return;
  const doc = document.documentElement;
  const canFullscreen = Boolean(doc.requestFullscreen) && document.fullscreenEnabled !== false;
  const cfg = {
    cycle: root.dataset.cycle, stage: root.dataset.stage, endpoint: root.dataset.endpoint, csrf: root.dataset.csrf,
    max: Number(root.dataset.max), strikes: Number(root.dataset.strikes), accepted: root.dataset.accepted === '1',
    fullscreen: root.dataset.fullscreen === '1' && canFullscreen,
  };
  const navKey = `t360-proctor-nav:${cfg.cycle}`;
  const overlay = document.getElementById('proctor-overlay');
  const $ = (id) => document.getElementById(id);
  let active = false;      // monitoring (after the pledge, while the page is in use)
  let leaving = false;     // our own submit is navigating away
  let ended = false;
  let away = null;         // {start, via, fullscreenLost} while focus is elsewhere
  let extended = null;     // last display check: true / false / null (unsupported)
  let pausedFor = null;    // 'fullscreen' | 'screen'
  const lastMinor = {};

  const screenState = () => ('isExtended' in window.screen ? window.screen.isExtended : null);
  const isFull = () => Boolean(document.fullscreenElement);
  const enterFull = () => (cfg.fullscreen && !isFull() ? doc.requestFullscreen().catch(() => {}) : Promise.resolve());

  function show(view) {
    overlay.hidden = !view;
    overlay.querySelectorAll('[data-view]').forEach((el) => { el.hidden = el.dataset.view !== view; });
    document.body.classList.toggle('proctor-paused', Boolean(view));
  }

  function updateBar() {
    const strikes = $('pb-strikes');
    if (strikes) strikes.innerHTML = `Strikes <b>${cfg.strikes}</b> of ${cfg.max}`;
    strikes?.classList.toggle('bad', cfg.strikes > 0);
    const scr = $('pb-screen');
    if (scr) {
      scr.textContent = extended === true ? 'Display: 2+ connected' : extended === false ? 'Display: one ✓' : 'Display: not checkable';
      scr.classList.toggle('bad', extended === true);
    }
    const full = $('pb-full');
    if (full) { full.textContent = isFull() ? 'Full screen ✓' : 'Not full screen'; full.classList.toggle('bad', active && !isFull()); }
  }

  // ---- reporting -------------------------------------------------------------
  function send(events, beacon = false) {
    const body = JSON.stringify({ csrf_token: cfg.csrf, stage: cfg.stage, events });
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(cfg.endpoint, new Blob([body], { type: 'application/json' }));
      return Promise.resolve(null);
    }
    return fetch(cfg.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true,
                                 credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null)).then(handle).catch(() => null);
  }
  function minor(kind) {
    const now = Date.now();
    if (now - (lastMinor[kind] || 0) < 3000) return;  // one report per burst
    lastMinor[kind] = now;
    send([{ kind }]);
  }
  function handle(result) {
    if (!result || result.ignored) return result;
    if (typeof result.strikes === 'number') { cfg.strikes = result.strikes; updateBar(); }
    if (result.limit) end(result);
    else if (result.warning) warn(result.warning);
    return result;
  }
  function warn(w) {
    const left = Math.max(cfg.max - cfg.strikes, 0);
    $('proctor-warning-title').textContent = w.label;
    $('proctor-warning-text').textContent =
      `${w.seconds ? `You were away for ${Math.round(w.seconds)} s. ` : ''}This is recorded and shared with your manager. `
      + `${left} strike${left === 1 ? '' : 's'} left before your attempt is submitted automatically.`;
    show('warning');
  }
  function end(result) {
    if (ended) return;
    ended = true;
    active = false;
    show('ended');
    markLeaving();
    if (result.redirect) { window.location.assign(result.redirect); return; }
    if (result.action === 'submit_mcq') {
      const confirm = $('confirm-submit');
      if (confirm) confirm.click(); else $('assessment-form')?.submit();
    }
  }

  // ---- pausing: full screen and displays ------------------------------------------
  function pause(reason) {
    pausedFor = reason;
    if (reason === 'screen') {
      $('proctor-paused-title').textContent = 'Disconnect the extra display';
      $('proctor-paused-text').textContent = 'A second display was detected and recorded. Disconnect it to continue; the check updates by itself.';
      $('proctor-paused-go').textContent = 'Check again';
    } else {
      $('proctor-paused-title').textContent = 'Return to full screen';
      $('proctor-paused-text').textContent = 'The assessment continues in full screen. Leaving it was recorded.';
      $('proctor-paused-go').textContent = 'Return to full screen';
    }
    show('paused');
  }
  function resumeIfClear() {
    if (extended === true) { pause('screen'); return; }
    enterFull().then(() => {
      if (cfg.fullscreen && !isFull()) { pause('fullscreen'); return; }
      pausedFor = null;
      show(null);
      updateBar();
    });
  }
  $('proctor-paused-go').addEventListener('click', resumeIfClear);
  $('proctor-warning-ok').addEventListener('click', resumeIfClear);

  function checkScreen() {
    const now = screenState();
    if (active && now === true && extended !== true) {
      send([{ kind: 'multi_screen', detail: { extended: true } }]);
      extended = now;
      pause('screen');
    } else {
      extended = now;
      if (pausedFor === 'screen' && now !== true) resumeIfClear();
    }
    updateBar();
  }
  window.screen.addEventListener?.('change', checkScreen);
  window.setInterval(checkScreen, 4000);

  // ---- leaving the page or the window ---------------------------------------------
  function startAway(via) {
    if (!active || away) return;
    away = { start: Date.now(), via, fullscreenLost: false };
  }
  function endAway() {
    if (!away) return;
    const seconds = (Date.now() - away.start) / 1000;
    const detail = { via: away.via, fullscreen_lost: away.fullscreenLost };
    away = null;
    send([{ kind: 'focus_lost', seconds, detail }]);
    if (cfg.fullscreen && !isFull() && !ended) pause('fullscreen');
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) startAway('tab');
    else if (document.hasFocus()) endAway();
  });
  window.addEventListener('blur', () => {
    window.setTimeout(() => { if (!document.hasFocus()) startAway(document.hidden ? 'tab' : 'window'); }, 0);
  });
  window.addEventListener('focus', () => { if (!document.hidden) endAway(); });
  document.addEventListener('fullscreenchange', () => {
    updateBar();
    if (!active || ended || leaving || isFull()) return;
    window.setTimeout(() => {
      if (away || document.hidden || !document.hasFocus()) { if (away) away.fullscreenLost = true; return; }  // part of leaving
      send([{ kind: 'fullscreen_exit' }]);
      pause('fullscreen');
    }, 400);
  });

  function markLeaving() {
    leaving = true;
    active = false;
    try { sessionStorage.setItem(navKey, '1'); } catch (e) { /* storage blocked: a reload then reads as a reopen */ }
  }
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (form instanceof HTMLFormElement && /^\/employee\/(assessment|evidence)\//.test(new URL(form.action, window.location.href).pathname)) {
      markLeaving();
    }
  }, true);
  $('confirm-submit')?.addEventListener('click', markLeaving, true);  // the MCQ submits with form.submit(): no submit event
  window.addEventListener('beforeunload', (e) => {
    if (active && !leaving && !ended) { e.preventDefault(); e.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => {
    if (!active || leaving || ended) return;
    send([{ kind: 'left_page', seconds: away ? (Date.now() - away.start) / 1000 : 0 }], true);
  });

  // ---- copying, pasting and shortcuts ----------------------------------------------
  const block = (kind) => (e) => {
    if (!active) return;
    e.preventDefault();
    e.stopPropagation();  // before the answer box's own paste counter sees it
    minor(kind);
  };
  document.addEventListener('copy', block('copy_blocked'), true);
  document.addEventListener('cut', block('copy_blocked'), true);
  document.addEventListener('paste', block('paste_blocked'), true);
  document.addEventListener('drop', block('drop_blocked'), true);
  document.addEventListener('contextmenu', block('context_menu'), true);
  document.addEventListener('keydown', (e) => {
    if (!active) return;
    const key = (e.key || '').toLowerCase();
    const mod = e.ctrlKey || e.metaKey;
    if (key === 'f12' || (mod && e.shiftKey && ['i', 'j', 'c'].includes(key)) || (e.metaKey && e.altKey && ['i', 'j', 'c'].includes(key))) {
      e.preventDefault();
      minor('devtools_shortcut');
    } else if (mod && ['p', 's', 'u'].includes(key)) {
      e.preventDefault();  // print, save page, view source
    }
  }, true);
  document.addEventListener('keyup', (e) => { if (active && e.key === 'PrintScreen') minor('screenshot_key'); }, true);

  // ---- starting --------------------------------------------------------------------
  function startMonitoring(firstTime) {
    active = true;
    const now = screenState();
    extended = now === true ? false : now;  // a display already attached is reported and pauses (checkScreen)
    document.body.classList.add('proctor-active');
    checkScreen();
    if (firstTime) {
      const events = [{ kind: 'rules_accepted', detail: { extended } }];
      if (extended === null) events.push({ kind: 'screen_check_unavailable' });
      send(events);
    }
  }

  function setupRules() {
    const pledge = $('proctor-pledge');
    const start = $('proctor-start');
    const line = $('proctor-screen');
    const refresh = () => {
      const ext = screenState();
      line.textContent = ext === true ? '✗ More than one display is connected. Disconnect the extra display; this check updates by itself.'
        : ext === false ? '✓ One display detected.'
        : 'This browser can’t check for a second display, so that is noted on your record. Chrome or Edge can check.';
      line.className = `proctor-screen ${ext === true ? 'bad' : ext === false ? 'good' : ''}`;
      start.disabled = !pledge.checked || ext === true;
    };
    pledge.addEventListener('change', refresh);
    window.screen.addEventListener?.('change', refresh);
    window.setInterval(refresh, 2000);
    refresh();
    start.addEventListener('click', () => {
      enterFull().then(() => {
        if (cfg.fullscreen && !isFull()) { line.textContent = 'The browser blocked full screen. Allow it and press Start again.'; return; }
        show(null);
        startMonitoring(true);
      });
    });
    show('rules');
  }

  let internal = false;
  try { internal = sessionStorage.getItem(navKey) === '1'; sessionStorage.removeItem(navKey); } catch (e) { /* see markLeaving */ }
  extended = screenState();
  updateBar();
  if (!cfg.accepted) {
    setupRules();
  } else {
    send([...(internal ? [] : [{ kind: 'reopened' }]), { kind: 'page_loaded' }]);
    $('proctor-resume-text').textContent = internal
      ? 'Full screen pauses while the page loads your next step. Continue when you’re ready.'
      : 'You left this page earlier; that was recorded. Continue when you’re ready.';
    $('proctor-resume').addEventListener('click', () => {
      enterFull().then(() => {
        if (cfg.fullscreen && !isFull()) return;
        show(null);
        startMonitoring(false);
      });
    });
    if (cfg.fullscreen) show('resume'); else startMonitoring(false);
  }
})();
