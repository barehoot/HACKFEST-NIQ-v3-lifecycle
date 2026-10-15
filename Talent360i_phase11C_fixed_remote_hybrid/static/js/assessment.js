document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('assessment-app');
  if (!root) return;
  const total = Number(root.dataset.total);
  let current = 0;
  const panels = [...document.querySelectorAll('.question-panel')];
  const numbers = [...document.querySelectorAll('.question-number')];
  const next = document.getElementById('next-question');
  const previous = document.getElementById('previous-question');
  const submit = document.getElementById('submit-assessment');
  const modal = document.getElementById('submit-modal');
  const answeredNames = () => new Set([...document.querySelectorAll('.question-panel input:checked, .question-panel textarea')]
    .filter((el) => el.tagName === 'TEXTAREA' ? el.value.trim() : true)
    .map((input) => input.name));
  const count = () => answeredNames().size;

  function render(index) {
    current = Math.max(0, Math.min(total - 1, index));
    panels.forEach((panel, i) => panel.classList.toggle('visible', i === current));
    numbers.forEach((number, i) => {
      const panel = panels[i];
      number.classList.toggle('active', i === current);
      number.classList.toggle('answered', Boolean(panel.querySelector('input:checked')) || Boolean(panel.querySelector('textarea')?.value.trim()));
    });
    document.getElementById('answered-count').textContent = count();
    previous.disabled = current === 0;
    next.classList.toggle('hidden', current === total - 1);
    submit.classList.toggle('hidden', current !== total - 1);
  }

  numbers.forEach((button, index) => button.addEventListener('click', () => render(index)));
  document.querySelectorAll('.question-panel input').forEach((input) => input.addEventListener('change', () => render(current)));
  document.querySelectorAll('.question-panel textarea').forEach((textarea) => {
    textarea.addEventListener('input', () => {
      const target = document.getElementById(`chars_${textarea.name.replace('question_', '')}`);
      if (target) target.textContent = textarea.value.length;
      render(current);
    });
  });
  next.addEventListener('click', () => render(current + 1));
  previous.addEventListener('click', () => render(current - 1));
  submit.addEventListener('click', () => { document.getElementById('modal-answered').textContent = count(); modal.classList.remove('hidden'); });
  document.querySelector('[data-close-modal]')?.addEventListener('click', () => modal.classList.add('hidden'));
  document.getElementById('confirm-submit')?.addEventListener('click', () => {
    const answers = {};
    document.querySelectorAll('.question-panel input:checked, .question-panel textarea').forEach((input) => {
      if (input.value.trim()) answers[input.name] = input.value.trim();
    });
    document.getElementById('answers-field').value = JSON.stringify(answers);
    const form = document.getElementById('assessment-form');
    // requestSubmit fires the submit event, so the proctored page can carry on without a reload.
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });

  const timer = document.getElementById('timer');
  let seconds = Number(timer.dataset.seconds);
  const tick = () => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    timer.textContent = `${mins}:${String(secs).padStart(2, '0')}`;
    if (seconds > 0) { seconds -= 1; window.setTimeout(tick, 1000); }
    else { timer.textContent = 'Time elapsed'; timer.classList.add('elapsed'); }
  };
  tick();
  render(0);
});
