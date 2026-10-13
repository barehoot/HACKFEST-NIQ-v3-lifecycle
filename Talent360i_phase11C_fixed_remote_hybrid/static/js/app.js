document.addEventListener('DOMContentLoaded', () => {
  const menu = document.querySelector('.mobile-menu');
  const close = document.querySelector('.sidebar-close');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');

  const setMenu = (open) => {
    sidebar?.classList.toggle('open', open);
    overlay?.classList.toggle('open', open);
    menu?.setAttribute('aria-expanded', String(open));
  };

  menu?.addEventListener('click', () => setMenu(!sidebar?.classList.contains('open')));
  close?.addEventListener('click', () => setMenu(false));
  overlay?.addEventListener('click', () => setMenu(false));

  document.querySelectorAll('.nav a').forEach((link) => {
    link.addEventListener('click', () => setMenu(false));
  });

  document.querySelectorAll('.status-tabs button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.status-tabs button').forEach((item) => item.classList.remove('selected'));
      button.classList.add('selected');
      const status = button.dataset.status;
      document.querySelectorAll('[data-assessment-status]').forEach((row) => {
        row.hidden = status !== 'all' && row.dataset.assessmentStatus !== status;
      });
    });
  });
});

// Lightweight responsive top navigation for the reference-style shell.
document.addEventListener('DOMContentLoaded', () => {
  const menu = document.querySelector('.mobile-menu');
  const nav = document.querySelector('.top-nav');
  if (!menu || !nav) return;
  menu.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    menu.setAttribute('aria-expanded', String(open));
  });
});
