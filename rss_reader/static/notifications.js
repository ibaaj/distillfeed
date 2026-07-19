(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const toast = document.getElementById('toast');
  function notify(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('visible');
    setTimeout(() => toast.classList.remove('visible'), 4200);
  }
  document.querySelectorAll('.issue-dismiss').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch(`/api/issues/${button.dataset.issueId}/acknowledge`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
        body: '{}',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      button.closest('[data-issue-row]')?.remove();
      notify('Notice dismissed. It will return if the condition changes.');
    } catch (error) {
      button.disabled = false;
      notify(error.message);
    }
  }));
})();
