(() => {
  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const status = document.getElementById('ai-status');
  const toast = document.getElementById('toast');
  let toastTimer;
  function notify(message) {
    if (!toast) return;
    clearTimeout(toastTimer); toast.textContent = message; toast.classList.add('visible');
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 4200);
  }
  const completedOperation = sessionStorage.getItem('distillfeedCompletedOperation');
  if (completedOperation) {
    sessionStorage.removeItem('distillfeedCompletedOperation');
    setTimeout(() => notify(completedOperation), 150);
  }
  async function request(path, options = {}, retry = true) {
    const headers = { ...(options.headers || {}), 'X-CSRF-Token': csrf };
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    if (response.status === 403 && retry) {
      const token = await fetch('/api/csrf', { cache: 'no-store' }).then(value => value.json());
      csrf = token.csrf_token || ''; return request(path, options, false);
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status})`);
    return data;
  }
  function compactError(value, fallback) {
    const message = String(value || fallback).replace(/\s+/g, ' ').trim();
    return message.length > 180 ? `${message.slice(0, 177)}…` : message;
  }

  const nav = [...document.querySelectorAll('[data-ai-panel]')];
  const panels = [...document.querySelectorAll('[data-ai-content]')];
  function selectPanel(name, remember = true) {
    if (!panels.some(panel => panel.dataset.aiContent === name)) name = 'overview';
    nav.forEach(button => {
      const active = button.dataset.aiPanel === name;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    panels.forEach(panel => panel.classList.toggle('active', panel.dataset.aiContent === name));
    if (remember) history.replaceState(null, '', `${location.pathname}${location.search}#${name}`);
    document.querySelector('.ai-policy-content')?.scrollTo({ top: 0, behavior: 'instant' });
  }
  nav.forEach(button => button.addEventListener('click', () => selectPanel(button.dataset.aiPanel)));
  selectPanel(location.hash.slice(1) || 'overview', false);

  const form = document.getElementById('ai-config-form');
  const save = document.getElementById('ai-save');
  const controls = [...form.querySelectorAll('[data-config-path]')];
  const value = control => control.dataset.type === 'bool' ? control.checked : control.value;
  let snapshot = new Map(controls.map(control => [control.dataset.configPath, value(control)]));
  let formDirty = false;
  let formSaving = false;
  function updateDirty(message = '') {
    formDirty = controls.some(control => value(control) !== snapshot.get(control.dataset.configPath));
    save.disabled = formSaving || !formDirty;
    if (status) status.textContent = message || (formDirty ? 'Unsaved changes. Save them before running an update or changing stored entries.' : '');
  }
  controls.forEach(control => {
    control.addEventListener('input', () => updateDirty());
    control.addEventListener('change', () => updateDirty());
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (formSaving || !formDirty) return;
    formSaving = true; save.disabled = true; controls.forEach(control => { control.disabled = true; });
    if (status) status.textContent = 'Saving…';
    const changedControls = controls.filter(
      control => value(control) !== snapshot.get(control.dataset.configPath),
    );
    const values = Object.fromEntries(
      changedControls.map(control => [control.dataset.configPath, value(control)]),
    );
    try {
      await request('/api/config', { method: 'POST', body: JSON.stringify({ values }) });
      snapshot = new Map(controls.map(control => [control.dataset.configPath, value(control)]));
      formDirty = false;
      updateDirty('Saved. Rebuilding the visible next-update plan…'); notify('AI summary settings saved');
      setTimeout(() => location.reload(), 300);
    } catch (error) {
      formSaving = false; controls.forEach(control => { control.disabled = false; });
      updateDirty(error.message); notify(error.message);
    }
  });
  window.addEventListener('beforeunload', event => {
    if (!formDirty || formSaving) return;
    event.preventDefault(); event.returnValue = '';
  });

  const provider = document.getElementById('ai-provider');
  function showProviderFields() {
    document.querySelectorAll('.ollama-only').forEach(field => { field.hidden = provider?.value !== 'ollama'; });
  }
  provider?.addEventListener('change', showProviderFields); showProviderFields();
  document.getElementById('arxiv-ntfy-test')?.addEventListener('click', async event => {
    if (formDirty || formSaving) { notify('Save your changes before testing the stored arXiv device-alert settings'); return; }
    event.currentTarget.disabled = true;
    try {
      await request('/api/plugins/arxiv_digest/actions/test-ntfy', { method: 'POST', body: '{}' });
      notify('Test arXiv device alert sent');
    } catch (error) { notify(error.message); }
    finally { event.currentTarget.disabled = false; }
  });

  document.querySelectorAll('.ai-source-row').forEach(row => {
    const sourceControls = [...row.querySelectorAll('[data-source-field]')];
    const sourceSave = row.querySelector('.ai-source-save');
    const sourceSnapshot = () => JSON.stringify(sourceControls.map(control => control.value));
    let stored = sourceSnapshot();
    const changed = () => { sourceSave.disabled = sourceSnapshot() === stored; };
    sourceControls.forEach(control => { control.addEventListener('change', changed); control.addEventListener('input', changed); });
    sourceSave.addEventListener('click', async () => {
      if (formDirty || formSaving) { notify('Save the page settings before changing a source'); return; }
      sourceSave.disabled = true; const payload = {};
      sourceControls.forEach(control => { payload[control.dataset.sourceField] = control.type === 'number' ? Number(control.value) : control.value; });
      try {
        await request(`/api/${row.dataset.sourceKind}/${row.dataset.sourceId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        stored = sourceSnapshot(); notify('Source policy saved. Rebuilding the next-update plan…');
        setTimeout(() => location.reload(), 300);
      } catch (error) { sourceSave.disabled = false; notify(error.message); }
    });
  });

  function applyQueueFilter() {
    const parameters = new URLSearchParams(location.search);
    const search = document.getElementById('ai-queue-search')?.value.trim() || '';
    const state = document.getElementById('ai-queue-state')?.value || 'ready';
    const inactive = document.querySelector('#ai-queue-filter [name="include_inactive"]')?.checked;
    search ? parameters.set('q', search) : parameters.delete('q');
    parameters.set('queue_view', state); parameters.delete('queue_page');
    inactive ? parameters.set('include_inactive', '1') : parameters.delete('include_inactive');
    location.assign(`/ai?${parameters.toString()}#queue`);
  }
  document.getElementById('ai-queue-apply')?.addEventListener('click', applyQueueFilter);
  document.getElementById('ai-queue-search')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); applyQueueFilter(); }
  });
  document.querySelectorAll('.ai-entry-disposition').forEach(button => button.addEventListener('click', async () => {
    if (formDirty || formSaving) { notify('Save the page settings before changing a waiting entry'); return; }
    const article = button.closest('[data-item-id]'); button.disabled = true;
    try {
      await request('/api/items/ai-disposition', { method: 'POST', body: JSON.stringify({
        item_ids: [Number(article.dataset.itemId)], disposition: button.dataset.disposition,
      }) });
      article.remove(); notify(button.dataset.disposition === 'excluded' ? 'Entry excluded from future AI updates' : 'Entry restored to the waiting queue');
      setTimeout(() => location.reload(), 300);
    } catch (error) { button.disabled = false; notify(error.message); }
  }));

  const runButton = document.getElementById('ai-run-now');
  const arxivRunButton = document.getElementById('arxiv-run-now');
  let running = false;
  let stopRequested = false;
  let jobButton = null;
  let jobStartedAt = '';
  let jobKind = 'summary';
  let jobOperationId = '';
  let pollFailures = 0;
  const staticallyDisabled = button => button?.dataset.staticDisabled === 'true';
  const jobButtons = [runButton, arxivRunButton].filter(Boolean);
  const setJobButtons = callback => jobButtons.forEach(button => {
    button.disabled = staticallyDisabled(button) || callback(button);
  });
  function scopedPayload() {
    const parameters = new URLSearchParams(location.search); const payload = {};
    if (parameters.has('feed_id')) payload.feed_id = Number(parameters.get('feed_id'));
    else if (parameters.has('group_id')) payload.group_id = Number(parameters.get('group_id'));
    return payload;
  }
  async function pollJob() {
    let result;
    try {
      const path = jobOperationId ? `/api/status?operation_id=${encodeURIComponent(jobOperationId)}` : '/api/status';
      result = await request(path);
      pollFailures = 0;
    } catch (error) {
      pollFailures += 1;
      if (pollFailures < 5) {
        if (status) status.textContent = `Status temporarily unavailable · retrying (${pollFailures}/5)…`;
        setTimeout(pollJob, 2500);
        return;
      }
      running = false;
      setJobButtons(() => false);
      if (status) status.textContent = 'Status could not be checked. Controls are available again; review System notices before retrying.';
      notify(error.message);
      return;
    }
    const active = result.operation
      ? Boolean(result.operation.active)
      : (result.locks || []).some(name => ['feed-refresh', 'llm-summary', 'summary-update'].includes(name));
    if (active) {
      running = true;
      setJobButtons(button => button !== jobButton || stopRequested || Boolean(result.cancel_requested));
      if (jobButton) jobButton.textContent = stopRequested || result.cancel_requested ? 'Stopping safely…' : 'Stop update';
      if (status && !stopRequested && !result.cancel_requested) {
        if (result.phase === 'arxiv-digest') status.textContent = 'Ranking the new arXiv announcement and writing one daily digest…';
        else if (result.phase === 'composing') status.textContent = 'Writing the affected summaries from completed evaluations…';
        else if (result.phase === 'evaluating' && result.ai_job) status.textContent = `Evaluated ${result.ai_job.completed_items} of ${result.ai_job.planned_items} selected entries…`;
        else status.textContent = 'Checking feeds before the AI stage…';
      }
      setTimeout(pollJob, 1000); return;
    }
    if (running) {
      let message = result.operation?.message || 'AI summaries are up to date';
      if (!result.operation && (stopRequested || result.cancel_requested)) {
        const saved = Number(result.ai_job?.completed_items || 0);
        message = saved ? `Stopped · ${saved} evaluation${saved === 1 ? '' : 's'} saved` : 'Stopped safely';
      } else if (!result.operation && jobKind === 'arxiv') {
        const recentArxiv = result.arxiv_run && (
          !jobStartedAt || Date.parse(result.arxiv_run.started_at || '') >= Date.parse(jobStartedAt) - 1000
        ) ? result.arxiv_run : null;
        if (recentArxiv?.status === 'failed') {
          message = `arXiv AI update failed · ${compactError(recentArxiv.error, 'the announcement remains ready to retry')}`;
        } else if (recentArxiv?.status === 'cancelled') {
          message = 'arXiv update stopped · the announcement remains ready';
        } else if (recentArxiv?.status === 'success') {
          const evaluated = Number(recentArxiv.submitted_items || 0);
          message = `Daily arXiv digest updated · ${evaluated} paper${evaluated === 1 ? '' : 's'} evaluated`;
        } else {
          const pending = Number(result.arxiv?.pending_items || 0);
          message = pending
            ? `arXiv checked · ${pending} paper${pending === 1 ? '' : 's'} waiting for AI`
            : 'arXiv checked · no new announcement';
        }
      } else if (!result.operation && result.ai_job?.status === 'failed') {
        message = `Summary update failed · ${compactError(result.ai_job.error, 'the waiting entries remain available to retry')}`;
      } else if (!result.operation && result.ai_job && (!jobStartedAt || Date.parse(result.ai_job.started_at || '') >= Date.parse(jobStartedAt) - 1000)) {
        const evaluated = Number(result.ai_job.completed_items || 0);
        const included = Number(result.ai_result?.included || 0);
        const summaries = Number(result.ai_result?.summaries || 0);
        message = `${evaluated} entr${evaluated === 1 ? 'y' : 'ies'} evaluated · ${included} entr${included === 1 ? 'y' : 'ies'} included${summaries ? ` in ${summaries} summar${summaries === 1 ? 'y' : 'ies'}` : ''}`;
      }
      sessionStorage.setItem('distillfeedCompletedOperation', message);
      location.reload();
      return;
    }
    setJobButtons(() => false);
  }
  async function runOrStop(button, path, payload) {
    if (!running && (formDirty || formSaving)) { notify('Save your AI summary settings before starting an update'); return; }
    if (running) {
      button.disabled = true; button.textContent = 'Stopping safely…';
      try {
        await request('/api/jobs/cancel', { method: 'POST', body: JSON.stringify({ jobs: ['feed-refresh', 'llm-summary', 'summary-update'] }) });
        stopRequested = true;
      } catch (error) { button.disabled = false; button.textContent = 'Stop update'; notify(error.message); }
      return;
    }
    jobButton = button;
    jobStartedAt = new Date().toISOString();
    jobKind = path === '/api/arxiv/update' ? 'arxiv' : 'summary';
    setJobButtons(() => true);
    button.textContent = 'Starting update…';
    try {
      const started = await request(path, { method: 'POST', body: JSON.stringify(payload) });
      jobOperationId = started.operation_id || '';
      running = true; setTimeout(pollJob, 350);
    } catch (error) {
      setJobButtons(() => false);
      notify(error.message);
    }
  }
  runButton?.addEventListener('click', () => runOrStop(runButton, '/api/summarize', scopedPayload()));
  arxivRunButton?.addEventListener('click', () => runOrStop(arxivRunButton, '/api/arxiv/update', {}));
  if (document.body.dataset.jobRunning === 'true') {
    running = true;
    jobButton = location.hash === '#arxiv' && arxivRunButton ? arxivRunButton : runButton;
    pollJob();
  }
})();
