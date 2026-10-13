// Talent 360i v3 -- employee lifecycle interactions. Progressive enhancement:
// every card is also a plain link to the full profile, and every form works
// without JavaScript.
(() => {
  'use strict';

  // ---------------- people cards -> quick-view dialog ----------------
  let dialog;
  let lastTrigger = null;

  const ensureDialog = () => {
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.className = 'person-dialog';
    dialog.setAttribute('aria-label', 'Employee quick view');
    dialog.innerHTML = '<button class="pd-close" type="button" aria-label="Close">×</button><div class="pd-scroll"></div>';
    document.body.appendChild(dialog);
    dialog.querySelector('.pd-close').addEventListener('click', () => dialog.close());
    // Clicking the backdrop (the dialog element itself, outside its content) closes it.
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => { if (lastTrigger) lastTrigger.focus(); });
    return dialog;
  };

  const openCard = async (card) => {
    const url = card.dataset.cardUrl;
    if (!url) return;
    const dlg = ensureDialog();
    const body = dlg.querySelector('.pd-scroll');
    body.innerHTML = '<div class="pd-loading" role="status">Loading…</div>';
    if (!dlg.open) dlg.showModal();
    try {
      const res = await fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      // Server-rendered, autoescaped fragment from our own origin.
      body.innerHTML = await res.text();
      const heading = body.querySelector('h2');
      if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }
    } catch (err) {
      const href = card.querySelector('a.pc-open')?.getAttribute('href') || '#';
      body.innerHTML = '';
      const box = document.createElement('div');
      box.className = 'pd-loading';
      box.textContent = 'The quick view could not load. ';
      const link = document.createElement('a');
      link.href = href;
      link.textContent = 'Open the full profile →';
      box.appendChild(link);
      body.appendChild(box);
    }
  };

  // Mouse: anywhere on the card. Keyboard: the card's own "Quick view" button.
  document.addEventListener('click', (e) => {
    const card = e.target.closest('[data-card-url]');
    if (!card) return;
    const quick = e.target.closest('[data-open-card]');
    if (!quick && e.target.closest('a, button, form, input, select, textarea')) return;
    e.preventDefault();
    lastTrigger = quick || card.querySelector('[data-open-card]');
    openCard(card);
  });

  // ---------------- filters: submit on change ----------------
  document.addEventListener('change', (e) => {
    const form = e.target.closest('form[data-autosubmit]');
    if (form && e.target.matches('select')) form.requestSubmit();
  });

  // ---------------- profile tabs: mark the section in view ----------------
  document.addEventListener('DOMContentLoaded', () => {
    const tabs = document.querySelectorAll('.profile-tabs a[href^="#"]');
    if (!tabs.length || !('IntersectionObserver' in window)) return;
    const byId = new Map([...tabs].map((a) => [a.getAttribute('href').slice(1), a]));
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        tabs.forEach((a) => a.removeAttribute('aria-current'));
        byId.get(entry.target.id)?.setAttribute('aria-current', 'true');
      });
    }, { rootMargin: '-30% 0px -60% 0px' });
    byId.forEach((_, id) => { const el = document.getElementById(id); if (el) observer.observe(el); });
  });

  // ---------------- calendar agenda: minimise / expand ----------------
  // The panel and each day are native <details>, so this only adds "expand / collapse
  // all", remembers whether the panel was minimised, and opens a day the grid links to.
  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: not remembered */ } },
  };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('details[data-agenda-panel]').forEach((panel) => {
      const key = `t360.agenda:${panel.dataset.agendaPanel}`;
      if (store.get(key) === 'closed') panel.open = false;
      panel.addEventListener('toggle', () => store.set(key, panel.open ? 'open' : 'closed'));
      const tools = panel.querySelector('.agenda-tools');
      if (tools) tools.hidden = false;
      const today = new Date().toISOString().slice(0, 10);
      panel.addEventListener('click', (e) => {
        const button = e.target.closest('[data-agenda-all]');
        if (!button) return;
        const mode = button.dataset.agendaAll;
        panel.querySelectorAll('details.agenda-day').forEach((day) => {
          day.open = mode === 'open' || (mode === 'upcoming' && day.dataset.date >= today);
        });
      });
    });
  });
  const openAgendaDay = (hash) => {
    const day = hash && hash.startsWith('#agenda-') ? document.getElementById(hash.slice(1)) : null;
    if (!(day instanceof HTMLDetailsElement)) return;
    const panel = day.closest('details[data-agenda-panel]');
    if (panel) panel.open = true;
    day.open = true;
    day.scrollIntoView({ block: 'start' });
  };
  document.addEventListener('click', (e) => {
    const more = e.target.closest('a.cal-more');
    if (!more) return;
    e.preventDefault();
    window.history.replaceState(null, '', more.getAttribute('href'));
    openAgendaDay(more.getAttribute('href'));
  });
  window.addEventListener('hashchange', () => openAgendaDay(window.location.hash));
  document.addEventListener('DOMContentLoaded', () => openAgendaDay(window.location.hash));

  // ---------------- onboarding: show "new vertical / department" fields on demand ----------------
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select[data-reveal]').forEach((select) => {
      const boxes = document.querySelectorAll(`.reveal-box[data-reveal-for="${select.dataset.reveal}"]`);
      const sync = () => boxes.forEach((box) => {
        box.hidden = select.value !== box.dataset.revealValue;
        if (!box.hidden && document.activeElement === select) box.querySelector('input, select')?.focus();
      });
      select.addEventListener('change', sync);
      sync();
    });
  });

  // ---------------- code boxes: Tab indents; paste telemetry ----------------
  document.addEventListener('keydown', (e) => {
    const box = e.target;
    if (!(box instanceof HTMLTextAreaElement) || !box.classList.contains('code') || e.key !== 'Tab' || e.shiftKey) return;
    e.preventDefault();
    const { selectionStart: s, selectionEnd: end, value } = box;
    box.value = value.slice(0, s) + '    ' + value.slice(end);
    box.selectionStart = box.selectionEnd = s + 4;
  });
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form.code-form').forEach((form) => {
      const box = form.querySelector('textarea.code');
      if (!box || !form.elements.paste_chars) return;
      box.addEventListener('paste', (e) => {
        const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
        form.elements.paste_chars.value = String(Number(form.elements.paste_chars.value || 0) + text.length);
      });
    });
  });
})();
