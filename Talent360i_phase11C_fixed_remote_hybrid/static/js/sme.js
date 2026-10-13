document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('show-reject')?.addEventListener('click', () => document.getElementById('rejection-form')?.classList.remove('hidden'));
  document.getElementById('edit-question')?.addEventListener('click', () => alert('Editing will be connected to the backend question authoring workflow.'));
});
