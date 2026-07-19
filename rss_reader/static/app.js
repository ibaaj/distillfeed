(() => {
  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const toast = document.getElementById('toast');
  let toastTimer;

  function notify(message) {
    if (!toast) return;
    clearTimeout(toastTimer); toast.textContent = message; toast.classList.add('visible');
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 4000);
  }
  const completedOperation = sessionStorage.getItem('distillfeedCompletedOperation');
  if (completedOperation) {
    sessionStorage.removeItem('distillfeedCompletedOperation');
    setTimeout(() => notify(completedOperation), 150);
  }

  async function request(path, options = {}, retryCsrf = true) {
    const headers = { ...(options.headers || {}) };
    if (csrf) headers['X-CSRF-Token'] = csrf;
    if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 403 && retryCsrf) {
      const tokenResponse = await fetch('/api/csrf', { cache: 'no-store' });
      if (tokenResponse.ok) {
        const tokenData = await tokenResponse.json();
        csrf = tokenData.csrf_token || '';
        return request(path, options, false);
      }
    }
    return response;
  }

  async function api(path, options = {}) {
    const response = await request(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || data.message || (response.status === 403 ? 'Your session changed. Reload the page and try again.' : `Request failed (${response.status})`));
    return data;
  }

  document.querySelector('.system-notice-dismiss')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api(`/api/issues/${button.dataset.issueId}/acknowledge`, {
        method: 'POST', body: JSON.stringify({}),
      });
      location.reload();
    } catch (error) {
      button.disabled = false; notify(error.message);
    }
  });

  let syncPopupBackdrop = () => {};
  function closePopupMenus(except = null) {
    document.querySelectorAll('details.action-menu[open], details.nav-menu[open], details.summary-ai-config[open]').forEach(menu => {
      if (menu !== except) menu.open = false;
    });
    syncPopupBackdrop();
  }

  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    closePopupMenus();
    document.getElementById('subscriptions')?.classList.remove('open');
    dialog.showModal();
  }

  const savedLeftWidth = localStorage.getItem('rssLeftWidth');
  const savedMiddleWidth = localStorage.getItem('rssMiddleWidth');
  if (savedLeftWidth) document.documentElement.style.setProperty('--left-width', `${savedLeftWidth}px`);
  if (savedMiddleWidth) document.documentElement.style.setProperty('--middle-width', `${savedMiddleWidth}px`);

  function refreshSavedListState() {
    const list = document.querySelector('.saved-list'); if (!list) return;
    const count = list.querySelectorAll('.saved-row').length;
    const label = document.querySelector('.saved-results > header span');
    if (label) label.textContent = `${count} item${count === 1 ? '' : 's'}`;
    if (!count && !list.querySelector('.empty')) {
      const empty = document.createElement('p'); empty.className = 'empty';
      empty.textContent = 'Nothing is saved in this view.'; list.appendChild(empty);
    }
  }

  function askConfirmation(title, message, confirmLabel = 'Confirm') {
    const dialog = document.getElementById('confirm-dialog');
    document.getElementById('confirm-heading').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    document.getElementById('confirm-action').textContent = confirmLabel;
    dialog.returnValue = 'cancel'; openDialog(dialog);
    return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
  }

  async function loadWeather() {
    const widget = document.getElementById('weather-widget');
    if (!widget) return;
    try {
      const weather = await fetch('/api/weather').then(response => response.json());
      if (!weather.enabled) { widget.hidden = true; return; }
      if (weather.error) { widget.textContent = `${weather.location} · ${weather.error}`; return; }
      const english = weather.language === 'English';
      const dayLabels = english ? ['Today', 'Tomorrow', 'Day +2'] : ['Auj.', 'Dem.', 'J+2'];
      const rainLabel = english ? 'rain' : 'pluie';
      const fragments = [];
      const location = document.createElement('span'); location.className = 'weather-location'; location.textContent = `${weather.location} ${weather.temperature}°C`;
      fragments.push(location);
      (weather.days || []).slice(0, 3).forEach((day, index) => {
        const forecast = document.createElement('span'); forecast.className = 'forecast-day';
        forecast.textContent = `${dayLabels[index]} ${day.minimum}–${day.maximum}° · ${rainLabel} ${day.rain_probability}%`;
        forecast.setAttribute('aria-label', english ? `${day.condition}; expected rain ${day.rain_sum} mm` : `${day.condition}; pluie prévue ${day.rain_sum} mm`);
        fragments.push(forecast);
      });
      widget.replaceChildren(...fragments);
    } catch (_) { widget.textContent = 'Paris · weather unavailable'; }
  }
  loadWeather();

  const jobProgress = document.getElementById('job-progress');
  const jobProgressLabel = document.getElementById('job-progress-label');
  const jobProgressTime = document.getElementById('job-progress-time');
  const jobProgressBar = document.getElementById('job-progress-bar');
  const jobCancel = document.getElementById('job-cancel');
  let notificationsEnabled = document.querySelector('meta[name="completion-notifications"]')?.content === 'true';
  let jobMonitorToken = 0;
  let jobCancelRequested = false;
  let jobStartPending = false;
  const jobControls = () => document.querySelectorAll('#refresh-button, #scope-refresh-button, #scope-update-button');
  function setJobControlsDisabled(disabled) {
    jobControls().forEach(control => { control.disabled = disabled; });
  }
  function showJobProgress(message) {
    if (!jobProgress) return;
    jobProgress.hidden = false; jobProgressLabel.textContent = message;
    jobProgressBar.removeAttribute('value');
    jobCancelRequested = false;
    if (jobCancel) { jobCancel.disabled = true; jobCancel.textContent = 'Stop'; }
  }
  function phaseMessage(status) {
    if (status.phase === 'arxiv-digest') return 'Ranking the new arXiv announcement and writing its daily digest…';
    if (status.phase === 'composing') return 'Writing updated summaries from completed evaluations…';
    if (status.phase === 'evaluating' && status.ai_job) return `Evaluating ${status.ai_job.completed_items} of ${status.ai_job.planned_items} selected feed entries…`;
    if (status.phase === 'summarizing') return 'Evaluating feed entries and updating summaries…';
    if (status.phase === 'refreshing') return 'Checking RSS and Atom feeds…';
    return 'Finishing the update…';
  }
  function quantity(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
  }
  function compactError(value, fallback) {
    const message = String(value || fallback).replace(/\s+/g, ' ').trim();
    return message.length > 180 ? `${message.slice(0, 177)}…` : message;
  }
  function completionMessage(status, operation, startedAt, stopping) {
    if (stopping) {
      const saved = Number(status.ai_job?.completed_items || 0);
      return saved ? `Stopped · ${quantity(saved, 'evaluation')} saved` : 'Stopped safely';
    }
    const operationTime = Date.parse(startedAt);
    const recent = value => value && Date.parse(value.started_at || '') >= operationTime - 1000;
    const refresh = recent(status.refresh) ? status.refresh : null;
    const aiJob = recent(status.ai_job) ? status.ai_job : null;
    const arxivRun = recent(status.arxiv_run) ? status.arxiv_run : null;
    if (operation === 'arxiv-check') {
      if (refresh?.status === 'failed') return `arXiv check failed · ${refresh.error || 'review Feed health'}`;
      const failed = refresh
        ? Math.max(0, Number(refresh.feeds_attempted || 0) - Number(refresh.feeds_succeeded || 0))
        : 0;
      const added = Number(refresh?.new_items || 0);
      const pending = Number(status.arxiv?.pending_items || 0);
      const parts = [`arXiv checked · ${quantity(added, 'new shortlisted paper')}`];
      if (pending) parts.push(`${quantity(pending, 'paper')} waiting for AI`);
      if (failed) parts.push(quantity(failed, 'feed failure', 'feed failures'));
      if (status.arxiv?.last_api_error) parts.push('API backfill will retry later');
      return parts.join(' · ');
    }
    if (operation === 'refresh') {
      if (!refresh) return 'Feed check finished';
      if (refresh.status === 'failed') return `Feed check failed · ${refresh.error || 'review Feed health'}`;
      const failed = Math.max(0, Number(refresh.feeds_attempted || 0) - Number(refresh.feeds_succeeded || 0));
      return `Checked ${quantity(Number(refresh.feeds_attempted || 0), 'feed')} · ${quantity(Number(refresh.new_items || 0), 'new entry', 'new entries')}${failed ? ` · ${quantity(failed, 'failure', 'failures')}` : ''}`;
    }
    if (operation === 'arxiv') {
      if (arxivRun?.status === 'failed') {
        return `arXiv AI update failed · ${compactError(arxivRun.error, 'the announcement remains ready to retry')}`;
      }
      if (arxivRun?.status === 'cancelled') return 'arXiv update stopped · the announcement remains ready';
      if (arxivRun?.status === 'success') {
        const evaluated = Number(arxivRun.submitted_items || 0);
        return `Daily arXiv digest updated · ${quantity(evaluated, 'paper')} evaluated`;
      }
      if (refresh?.status === 'failed') return `arXiv check failed · ${refresh.error || 'review Feed health'}`;
      const pending = Number(status.arxiv?.pending_items || 0);
      if (pending) return `arXiv checked · ${quantity(pending, 'paper')} waiting for AI`;
      return 'arXiv checked · no new announcement';
    }
    if (aiJob) {
      if (aiJob.status === 'failed') return `Summary update failed · ${compactError(aiJob.error, 'the entries remain available to retry')}`;
      const evaluated = Number(aiJob.completed_items || 0);
      const included = Number(status.ai_result?.included || 0);
      const summaries = Number(status.ai_result?.summaries || 0);
      const result = `${quantity(evaluated, 'entry', 'entries')} evaluated · ${quantity(included, 'entry', 'entries')} included`;
      return summaries ? `${result} in ${quantity(summaries, 'summary', 'summaries')}` : result;
    }
    if (refresh?.status === 'failed') return `Summary update failed during feed check · ${refresh.error || 'review Feed health'}`;
    return 'No entries needed evaluation';
  }
  function monitorJobs(reloadWhenDone = false, initialMessage = 'Updating your reader…', operation = 'summary', startedAt = '', operationId = '') {
    const token = ++jobMonitorToken; const monitorStartedAt = Date.now(); let observedBusy = false; let idleChecks = 0; let pollFailures = 0;
    showJobProgress(initialMessage);
    const poll = async () => {
      if (token !== jobMonitorToken) return;
      try {
        const statusPath = operationId ? `/api/status?operation_id=${encodeURIComponent(operationId)}` : '/api/status';
        const response = await fetch(statusPath, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Status request failed (${response.status})`);
        const status = await response.json();
        pollFailures = 0;
        const busy = status.operation ? Boolean(status.operation.active) : Boolean(status.locks?.length);
        const stopping = jobCancelRequested || Boolean(status.cancel_requested);
        jobProgressLabel.textContent = busy
          ? (stopping ? 'Stopping safely after the current request…' : phaseMessage(status))
          : (stopping ? 'Stopped safely' : 'Finishing the update…');
        jobProgressTime.textContent = `${Math.max(1, Math.round((Date.now() - monitorStartedAt) / 1000))}s`;
        if (busy) {
          observedBusy = true; idleChecks = 0;
          if (jobCancel) { jobCancel.disabled = stopping; jobCancel.textContent = stopping ? 'Stopping…' : 'Stop'; }
          setTimeout(poll, 1200); return;
        }
        idleChecks += 1;
        if (!observedBusy && idleChecks < 3) { setTimeout(poll, 350); return; }
        const resultMessage = status.operation?.message || completionMessage(status, operation, startedAt, stopping);
        jobProgressLabel.textContent = resultMessage; jobProgressTime.textContent = '';
        if (jobCancel) jobCancel.disabled = true;
        jobProgressBar.value = 100;
        const successful = !status.operation || ['success', 'partial', 'empty'].includes(status.operation.state);
        if (!stopping && successful && notificationsEnabled && 'Notification' in window && Notification.permission === 'granted' && document.hidden) {
          new Notification('DistillFeed is up to date', { body: 'New feeds and summaries are ready.' });
        }
        setTimeout(() => {
          setJobControlsDisabled(false);
          if (reloadWhenDone) {
            sessionStorage.setItem('distillfeedCompletedOperation', resultMessage);
            location.reload();
          } else {
            jobProgress.hidden = true; notify(resultMessage);
          }
        }, 850);
      } catch (_) {
        pollFailures += 1;
        if (pollFailures >= 5) {
          setJobControlsDisabled(false);
          if (jobCancel) jobCancel.disabled = true;
          jobProgressLabel.textContent = 'Status could not be checked. Controls are available again; review System notices before retrying.';
          jobProgressTime.textContent = '';
          return;
        }
        jobProgressLabel.textContent = `Status temporarily unavailable · retrying (${pollFailures}/5)…`;
        setTimeout(poll, 2500);
      }
    };
    setTimeout(poll, 250);
  }
  async function startJob(path, body, message, operationOverride = '') {
    if (jobStartPending) { notify('An update is already starting'); return; }
    jobStartPending = true;
    const startedAt = new Date().toISOString();
    const operation = operationOverride || (path === '/api/refresh' ? 'refresh' : path === '/api/arxiv/update' ? 'arxiv' : 'summary');
    try {
      closePopupMenus(); showJobProgress(message); setJobControlsDisabled(true);
      const started = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
      jobStartPending = false; monitorJobs(true, message, operation, startedAt, started.operation_id || '');
    } catch (error) {
      jobStartPending = false; setJobControlsDisabled(false);
      if (jobProgress) jobProgress.hidden = true;
      notify(error.message);
    }
  }
  jobCancel?.addEventListener('click', async () => {
    if (jobCancel.disabled) return;
    jobCancel.disabled = true; jobCancel.textContent = 'Stopping…';
    try {
      await api('/api/jobs/cancel', { method: 'POST', body: JSON.stringify({ jobs: ['feed-refresh', 'llm-summary', 'summary-update'] }) });
      jobCancelRequested = true;
      jobProgressLabel.textContent = 'Stopping safely after the current request…';
    } catch (error) {
      jobCancel.disabled = false; jobCancel.textContent = 'Stop'; notify(error.message);
    }
  });
  document.getElementById('refresh-button')?.addEventListener('click', () => startJob('/api/refresh', {}, 'Checking all feeds…'));
  function selectedScopePayload() {
    const feedId = document.querySelector('meta[name="selected-feed-id"]')?.content;
    const groupId = document.querySelector('meta[name="selected-group-id"]')?.content;
    const arxivDigest = document.querySelector('meta[name="arxiv-digest-scope"]')?.content === 'true';
    const payload = {};
    if (arxivDigest && groupId) payload.group_id = Number(groupId);
    else if (feedId) payload.feed_id = Number(feedId);
    else if (groupId) payload.group_id = Number(groupId);
    return payload;
  }
  function updateSelectedSummary() {
    const scopeName = document.getElementById('items-scope-title')?.textContent || 'this selection';
    const arxivDigest = document.querySelector('meta[name="arxiv-digest-scope"]')?.content === 'true';
    startJob(
      arxivDigest ? '/api/arxiv/update' : '/api/summarize',
      selectedScopePayload(),
      arxivDigest ? 'Checking for a new arXiv announcement and updating its daily digest…' : `Updating the summary for ${scopeName}…`,
    );
  }
  const summaryAIConfig = document.getElementById('summary-ai-config');
  const scopeUpdateButton = document.getElementById('scope-update-button');
  scopeUpdateButton?.addEventListener('click', () => {
    if (scopeUpdateButton.dataset.aiOff === 'true' && summaryAIConfig) {
      summaryAIConfig.open = true;
      summaryAIConfig.querySelector('select')?.focus();
      return;
    }
    updateSelectedSummary();
  });
  document.getElementById('scope-update-inline')?.addEventListener('click', updateSelectedSummary);
  document.getElementById('scope-enable-ai')?.addEventListener('click', () => {
    if (!summaryAIConfig) return;
    summaryAIConfig.open = true;
    const mode = summaryAIConfig.querySelector('[data-summary-source-field="ai_mode"]');
    if (mode && mode.value === 'off') mode.value = mode.querySelector('option[value="automatic"]') ? 'automatic' : mode.value;
    mode?.focus();
  });
  document.getElementById('summary-ai-config-save')?.addEventListener('click', async event => {
    if (!summaryAIConfig) return;
    const button = event.currentTarget; const payload = {};
    summaryAIConfig.querySelectorAll('[data-summary-source-field]').forEach(control => {
      payload[control.dataset.summarySourceField] = control.type === 'number' ? Number(control.value) : control.value;
    });
    button.disabled = true;
    try {
      await api(`/api/${summaryAIConfig.dataset.sourceKind}/${summaryAIConfig.dataset.sourceId}`, {
        method: 'PATCH', body: JSON.stringify(payload),
      });
      notify('AI configuration saved'); location.reload();
    } catch (error) { button.disabled = false; notify(error.message); }
  });
  document.getElementById('scope-refresh-button')?.addEventListener('click', () => {
    const scopeName = document.getElementById('items-scope-title')?.textContent || 'this selection';
    const arxivDigest = document.querySelector('meta[name="arxiv-digest-scope"]')?.content === 'true';
    startJob(
      '/api/refresh', { ...selectedScopePayload(), force: true },
      arxivDigest ? 'Checking configured arXiv categories…' : `Checking feeds in ${scopeName}…`,
      arxivDigest ? 'arxiv-check' : 'refresh',
    );
  });
  const summaryHelp = document.getElementById('summary-help-dialog');
  const summaryHelpButton = document.getElementById('summary-help-button');
  summaryHelpButton?.addEventListener('click', () => openDialog(summaryHelp));
  document.getElementById('summary-help-close')?.addEventListener('click', () => summaryHelp?.close());
  summaryHelp?.addEventListener('click', event => {
    if (event.target === summaryHelp) summaryHelp.close();
  });
  summaryHelp?.addEventListener('close', () => summaryHelpButton?.focus());
  let automaticRefreshTimer = null;
  function configureAutomaticRefresh(enabled, intervalMinutes, checkNow = false) {
    if (automaticRefreshTimer) clearInterval(automaticRefreshTimer);
    automaticRefreshTimer = null;
    if (!enabled) return;
    const interval = Math.max(1, Number(intervalMinutes) || 30) * 60000;
    const checkAutomatically = () => {
      const previousCheck = Number(sessionStorage.getItem('autoRefreshCheckedAt') || 0);
      if (Date.now() - previousCheck < interval) return;
      sessionStorage.setItem('autoRefreshCheckedAt', String(Date.now()));
      startJob('/api/refresh', { automatic: true }, 'Checking for new articles…');
    };
    if (checkNow) checkAutomatically();
    automaticRefreshTimer = setInterval(checkAutomatically, interval);
  }
  configureAutomaticRefresh(
    document.querySelector('meta[name="auto-refresh"]')?.content === 'true',
    document.querySelector('meta[name="refresh-interval-minutes"]')?.content || 30,
    true,
  );

  document.querySelectorAll('.item-title[href]').forEach(link => link.addEventListener('click', () => {
    const row = link.closest('.item-row'); const previous = row.dataset.read === '1';
    setRowsReadState([row], true);
    api(`/api/items/${row.dataset.itemId}/read`, { method: 'POST', keepalive: true, body: JSON.stringify({ read: true }) })
      .catch(error => { setRowsReadState([row], previous); notify(error.message); });
  }));
  document.querySelectorAll('.read-button').forEach(button => button.addEventListener('click', async () => {
    const row = button.closest('.item-row'); const read = button.dataset.read !== '1';
    try { await api(`/api/items/${row.dataset.itemId}/read`, { method: 'POST', body: JSON.stringify({ read }) }); setRowsReadState([row], read); }
    catch (error) { notify(error.message); }
  }));
  document.querySelectorAll('.star-button').forEach(button => button.addEventListener('click', async () => {
    const row = button.closest('.item-row'); const starred = button.dataset.starred !== '1';
    try {
      await api(`/api/items/${row.dataset.itemId}/star`, { method: 'POST', body: JSON.stringify({ starred }) });
      button.dataset.starred = starred ? '1' : '0'; row.dataset.starred = starred ? '1' : '0';
      button.classList.toggle('starred', starred);
      button.setAttribute('aria-label', starred ? 'Remove favorite' : 'Add favorite');
      if (!starred && new URLSearchParams(location.search).get('view') === 'favorites') { row.remove(); refreshSavedListState(); }
      applyItemFilters();
    }
    catch (error) { notify(error.message); }
  }));
  document.querySelectorAll('.read-later-button').forEach(button => button.addEventListener('click', async () => {
    const row = button.closest('.item-row'); const readLater = button.dataset.readLater !== '1';
    try {
      await api(`/api/items/${row.dataset.itemId}/read-later`, { method: 'POST', body: JSON.stringify({ read_later: readLater }) });
      button.dataset.readLater = readLater ? '1' : '0'; row.dataset.readLater = readLater ? '1' : '0';
      button.classList.toggle('active', readLater);
      button.setAttribute('aria-label', readLater ? 'Remove from Read later' : 'Add to Read later');
      if (!readLater && new URLSearchParams(location.search).get('view') === 'read-later') { row.remove(); refreshSavedListState(); }
      applyItemFilters();
    } catch (error) { notify(error.message); }
  }));

  document.querySelectorAll('.feed-retry').forEach(button => button.addEventListener('click', () => startJob('/api/refresh', { feed_id: Number(button.dataset.feedId), force: true }, 'Feed retry started')));
  const feedPropertiesDialog = document.getElementById('feed-properties-dialog');
  const feedPropertiesForm = document.getElementById('feed-properties-form');
  document.querySelectorAll('.feed-properties').forEach(button => button.addEventListener('click', () => {
    feedPropertiesForm.elements.id.value = button.dataset.id;
    feedPropertiesForm.elements.title.value = button.dataset.title;
    feedPropertiesForm.elements.xml_url.value = button.dataset.url;
    feedPropertiesForm.elements.group_id.value = button.dataset.groupId;
    const status = [];
    if (button.dataset.lastSuccess) status.push(`Last successful refresh: ${button.dataset.lastSuccess}`);
    else status.push('This feed has not completed a successful refresh yet.');
    if (button.dataset.httpStatus) status.push(`HTTP ${button.dataset.httpStatus}.`);
    if (button.dataset.lastError) status.push(button.dataset.lastError);
    document.getElementById('feed-properties-status').textContent = status.join(' ');
    const website = document.getElementById('feed-properties-website');
    if (button.dataset.htmlUrl) { website.href = button.dataset.htmlUrl; website.hidden = false; }
    else { website.removeAttribute('href'); website.hidden = true; }
    openDialog(feedPropertiesDialog); feedPropertiesForm.elements.title.select();
  }));
  feedPropertiesForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const id = Number(event.target.elements.id.value);
    const payload = {
      title: event.target.elements.title.value.trim(),
      xml_url: event.target.elements.xml_url.value.trim(),
      group_id: Number(event.target.elements.group_id.value),
    };
    try {
      await api(`/api/feeds/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      feedPropertiesDialog.close();
      startJob('/api/refresh', { feed_id: id, force: true }, `Saved ${payload.title}; refreshing this feed…`);
    } catch (error) { notify(error.message); }
  });
  document.getElementById('feed-properties-retry')?.addEventListener('click', () => {
    const id = Number(feedPropertiesForm.elements.id.value);
    feedPropertiesDialog.close(); startJob('/api/refresh', { feed_id: id, force: true }, 'Refreshing this feed…');
  });
  document.querySelectorAll('.feed-delete').forEach(button => button.addEventListener('click', async () => {
    if (!await askConfirmation('Delete feed?', `“${button.dataset.feedTitle}” and all of its stored items will be deleted.`, 'Delete feed')) return;
    try {
      await api(`/api/feeds/${button.dataset.feedId}`, { method: 'DELETE' });
      sessionStorage.setItem('subscriptionEditMode', '1'); location.reload();
    } catch (error) { notify(error.message); }
  }));
  document.querySelectorAll('.delete-group').forEach(button => button.addEventListener('click', async () => {
    if (!await askConfirmation('Delete group?', `“${button.dataset.title}”, its nested groups, feeds, items, and summaries will be deleted.`, 'Delete group')) return;
    try {
      await api(`/api/groups/${button.dataset.id}`, { method: 'DELETE' });
      sessionStorage.setItem('subscriptionEditMode', '1'); location.reload();
    } catch (error) { notify(error.message); }
  }));
  const renameDialog = document.getElementById('rename-dialog');
  const renameForm = document.getElementById('rename-form');
  document.querySelectorAll('.rename-feed, .rename-group').forEach(button => button.addEventListener('click', () => {
    const kind = button.classList.contains('rename-group') ? 'group' : 'feed';
    renameForm.elements.kind.value = kind; renameForm.elements.id.value = button.dataset.id;
    renameForm.elements.title.value = button.dataset.title;
    document.getElementById('rename-heading').textContent = kind === 'group' ? 'Rename group' : 'Rename feed';
    openDialog(renameDialog); renameForm.elements.title.select();
  }));
  renameForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const kind = event.target.elements.kind.value; const id = event.target.elements.id.value;
    const title = event.target.elements.title.value.trim(); if (!title) return;
    try {
      await api(`/api/${kind}s/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) });
      const button = document.querySelector(`.rename-${kind}[data-id="${id}"]`);
      const row = button?.closest(kind === 'group' ? '.group-summary-row' : '.feed-row');
      const label = row?.querySelector(kind === 'group' ? '.group-link > span:first-child' : '.feed-label');
      if (label) label.textContent = title;
      if (button) button.dataset.title = title;
      const deleteButton = row?.querySelector(kind === 'group' ? '.delete-group' : '.feed-delete');
      if (deleteButton) kind === 'group' ? deleteButton.dataset.title = title : deleteButton.dataset.feedTitle = title;
      if (kind === 'feed') {
        document.querySelectorAll(`.item-row[data-feed-id="${id}"] .item-feed`).forEach(element => { element.textContent = title; });
        document.querySelectorAll(`.summary-feed-name[data-feed-id="${id}"]`).forEach(element => { element.textContent = title; });
        if (document.querySelector('meta[name="selected-feed-id"]')?.content === id) {
          document.getElementById('items-scope-title').textContent = title;
          document.getElementById('summary-scope-title').textContent = ` · ${title}`;
        }
      } else {
        document.querySelectorAll(`.summary-group-name[data-group-id="${id}"]`).forEach(element => { element.textContent = title; });
        document.querySelectorAll(`#group-dialog option[value="${id}"], #feed-dialog option[value="${id}"]`).forEach(option => { option.textContent = title; });
        if (!document.querySelector('meta[name="selected-feed-id"]')?.content && document.querySelector('meta[name="selected-group-id"]')?.content === id) {
          document.getElementById('items-scope-title').textContent = title;
          document.getElementById('summary-scope-title').textContent = ` · ${title}`;
        }
      }
      renameDialog.close(); notify('Name saved');
    } catch (error) { notify(error.message); }
  });

  document.querySelectorAll('.subscription-edit-control').forEach(control => control.addEventListener('click', event => event.stopPropagation()));

  function bindDialog(buttonId, dialogId) { document.getElementById(buttonId)?.addEventListener('click', () => openDialog(document.getElementById(dialogId))); }
  bindDialog('add-group-button', 'group-dialog'); bindDialog('add-feed-button', 'feed-dialog');
  const settingsDialog = document.getElementById('settings-dialog');
  const settingsForm = document.getElementById('settings-form');
  const settingsStatus = document.getElementById('settings-status');
  const closeSettingsButton = document.getElementById('settings-close-button');
  const saveSettingsButton = document.getElementById('save-settings-button');
  const ntfyTestButton = document.getElementById('ntfy-test-button');
  const settingsControls = [...(settingsForm?.querySelectorAll('[data-config-path]') || [])];
  const settingsNavButtons = [...(settingsForm?.querySelectorAll('.settings-nav-button') || [])];
  const settingsPanels = [...(settingsForm?.querySelectorAll('[data-settings-panel]') || [])];
  const settingsContent = settingsForm?.querySelector('.settings-content');
  const settingsDesktop = window.matchMedia('(min-width: 761px)');
  let savedSettings = new Map();
  let settingsDirty = false;
  let settingsSaving = false;
  let settingsSavedSinceOpen = false;
  let selectedSettingsPanel = sessionStorage.getItem('distillfeedSettingsPanel') || settingsPanels[0]?.id || '';
  function selectSettingsPanel(panelId, { focus = false, remember = true } = {}) {
    let panel = settingsPanels.find(candidate => candidate.id === panelId);
    if (!panel) panel = settingsPanels[0];
    if (!panel) return;
    selectedSettingsPanel = panel.id;
    settingsPanels.forEach(candidate => candidate.classList.toggle('active', candidate === panel));
    settingsNavButtons.forEach(button => {
      const active = button.dataset.settingsTarget === panel.id;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      if (active && focus) button.focus();
    });
    panel.open = true;
    if (remember) sessionStorage.setItem('distillfeedSettingsPanel', panel.id);
    if (settingsDesktop.matches && settingsContent) settingsContent.scrollTop = 0;
  }
  function syncSettingsLayout() {
    if (settingsDesktop.matches) selectSettingsPanel(selectedSettingsPanel, { remember: false });
  }
  settingsNavButtons.forEach(button => button.addEventListener('click', () => {
    selectSettingsPanel(button.dataset.settingsTarget, { focus: true });
  }));
  settingsForm?.querySelector('.settings-sidebar')?.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const current = settingsNavButtons.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    let next = current + (event.key === 'ArrowDown' ? 1 : -1);
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = settingsNavButtons.length - 1;
    next = (next + settingsNavButtons.length) % settingsNavButtons.length;
    selectSettingsPanel(settingsNavButtons[next].dataset.settingsTarget, { focus: true });
  });
  settingsPanels.forEach(panel => panel.addEventListener('toggle', () => {
    if (!settingsDesktop.matches && panel.open) selectSettingsPanel(panel.id);
  }));
  if (settingsDesktop.addEventListener) settingsDesktop.addEventListener('change', syncSettingsLayout);
  else settingsDesktop.addListener(syncSettingsLayout);
  selectSettingsPanel(selectedSettingsPanel, { remember: false });
  function showSettings() { syncSettingsLayout(); openDialog(settingsDialog); }
  document.getElementById('settings-button')?.addEventListener('click', showSettings);
  document.getElementById('settings-menu-button')?.addEventListener('click', showSettings);
  const requestedSettingsPanel = new URLSearchParams(location.search).get('settings');
  if (requestedSettingsPanel) {
    const panelId = `settings-${requestedSettingsPanel}`;
    if (settingsPanels.some(panel => panel.id === panelId)) {
      selectSettingsPanel(panelId); showSettings();
    }
  }
  const controlValue = input => input.dataset.type === 'bool' ? input.checked : input.value;
  function snapshotSettings() {
    savedSettings = new Map(settingsControls.map(input => [input.dataset.configPath, controlValue(input)]));
  }
  function applyStoredFontPreview() {
    const paths = {
      'ui.subscription_font_size': '--subscription-size',
      'ui.item_font_size': '--item-size',
      'ui.summary_font_size': '--summary-size',
    };
    settingsControls.forEach(input => {
      if (paths[input.dataset.configPath]) document.documentElement.style.setProperty(paths[input.dataset.configPath], `${input.value}px`);
    });
  }
  function updateSettingsActions(message = null) {
    settingsDirty = settingsControls.some(input => controlValue(input) !== savedSettings.get(input.dataset.configPath));
    settingsDialog?.classList.toggle('has-unsaved-changes', settingsDirty);
    settingsPanels.forEach(panel => {
      const panelDirty = [...panel.querySelectorAll('[data-config-path]')]
        .some(input => controlValue(input) !== savedSettings.get(input.dataset.configPath));
      panel.classList.toggle('dirty', panelDirty);
      settingsNavButtons.find(button => button.dataset.settingsTarget === panel.id)?.classList.toggle('dirty', panelDirty);
    });
    if (saveSettingsButton) saveSettingsButton.disabled = settingsSaving || !settingsDirty;
    const ntfyEnabled = settingsControls.find(input => input.dataset.configPath === 'notifications.ntfy.enabled')?.checked;
    if (ntfyTestButton) ntfyTestButton.disabled = settingsSaving || settingsDirty || !ntfyEnabled;
    settingsForm?.querySelectorAll('.plugin-settings-action').forEach(button => {
      button.disabled = settingsSaving || settingsDirty;
    });
    if (settingsStatus && message !== null) {
      settingsStatus.classList.remove('error'); settingsStatus.textContent = message;
    } else if (settingsStatus && settingsDirty) {
      settingsStatus.classList.remove('error'); settingsStatus.textContent = 'Unsaved changes.';
    } else if (settingsStatus && !settingsDirty && settingsStatus.textContent === 'Unsaved changes.') {
      settingsStatus.textContent = '';
    }
  }
  function restoreSavedSettings() {
    settingsControls.forEach(input => {
      const value = savedSettings.get(input.dataset.configPath);
      if (input.dataset.type === 'bool') input.checked = Boolean(value); else input.value = value;
    });
    applyStoredFontPreview(); updateSettingsActions('');
  }
  function setSettingsSaving(saving) {
    settingsSaving = saving;
    settingsControls.forEach(input => { input.disabled = saving; });
    if (closeSettingsButton) closeSettingsButton.disabled = saving;
    updateSettingsActions(saving ? 'Saving…' : null);
  }
  function closeSettings({ reloadSaved = true } = {}) {
    if (settingsSaving) { updateSettingsActions('Saving…'); return false; }
    if (settingsDirty && !window.confirm('Discard unsaved settings changes?')) return false;
    if (settingsDirty) restoreSavedSettings();
    settingsDialog?.close();
    if (reloadSaved && settingsSavedSinceOpen) location.reload();
    return true;
  }
  snapshotSettings(); updateSettingsActions('');
  settingsControls.forEach(input => {
    input.addEventListener('input', () => updateSettingsActions());
    input.addEventListener('change', () => updateSettingsActions());
  });
  closeSettingsButton?.addEventListener('click', () => closeSettings());
  settingsDialog?.addEventListener('cancel', event => { event.preventDefault(); closeSettings(); });
  window.addEventListener('beforeunload', event => {
    if (!settingsDirty || settingsSaving) return;
    event.preventDefault(); event.returnValue = '';
  });
  document.getElementById('data-tools-button')?.addEventListener('click', () => {
    if (closeSettings({ reloadSaved: false })) openDialog(document.getElementById('data-dialog'));
  });
  document.querySelectorAll('.dialog-cancel').forEach(button => button.addEventListener('click', () => button.closest('dialog')?.close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    const outside = event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom;
    if (!outside) return;
    if (dialog === settingsDialog) closeSettings();
    else { dialog.returnValue = 'cancel'; dialog.close('cancel'); }
  }));
  document.getElementById('restore-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const formData = new FormData(event.target);
    try {
      const response = await request('/api/restore', { method: 'POST', body: formData });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `Restore failed (${response.status})`);
      notify('Backup restored; reloading DistillFeed'); setTimeout(() => location.reload(), 900);
    } catch (error) { notify(error.message); }
  });
  const subscriptionEditButton = document.getElementById('subscription-edit-toggle');
  const subscriptionPane = document.getElementById('subscriptions');
  const subscriptionTree = subscriptionPane?.querySelector('.tree');
  const moveSubscriptionDialog = document.getElementById('move-subscription-dialog');
  const moveSubscriptionForm = document.getElementById('move-subscription-form');
  const moveSubscriptionSelect = moveSubscriptionForm?.elements.parent_id;
  const moveSubscriptionSubmit = document.getElementById('move-subscription-submit');
  const moveSubscriptionStatus = document.getElementById('move-subscription-status');
  let subscriptionWidthBeforeEditing = null;
  let draggedSubscription = null;
  let subscriptionMovePending = false;
  let moveDialogRequestPending = false;
  function setSubscriptionEditing(editing) {
    const wasEditing = Boolean(subscriptionPane?.classList.contains('editing'));
    if (editing && !wasEditing && window.matchMedia('(min-width: 761px)').matches) {
      subscriptionWidthBeforeEditing = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--left-width'),
      );
      const expandedWidth = Math.max(
        subscriptionWidthBeforeEditing || 260,
        Math.min(460, Math.round(window.innerWidth * .38)),
      );
      document.documentElement.style.setProperty('--left-width', `${expandedWidth}px`);
    } else if (!editing && wasEditing && subscriptionWidthBeforeEditing !== null) {
      document.documentElement.style.setProperty('--left-width', `${subscriptionWidthBeforeEditing}px`);
      subscriptionWidthBeforeEditing = null;
    }
    subscriptionPane?.classList.toggle('editing', editing);
    subscriptionPane?.querySelectorAll('.drag-handle').forEach(handle => {
      handle.draggable = editing && !subscriptionMovePending;
    });
    if (subscriptionEditButton) {
      subscriptionEditButton.textContent = editing ? 'Done' : 'Manage';
      subscriptionEditButton.setAttribute('aria-pressed', editing ? 'true' : 'false');
    }
    syncSubscriptionMoveButtons();
    sessionStorage.setItem('subscriptionEditMode', editing ? '1' : '0');
  }
  if (subscriptionPane && sessionStorage.getItem('subscriptionEditMode') === '1') setSubscriptionEditing(true);
  subscriptionEditButton?.addEventListener('click', () => {
    const editing = subscriptionPane.classList.contains('editing');
    setSubscriptionEditing(!editing);
  });
  function directEntries(container) {
    return [...container.children].filter(child => child.classList.contains('subscription-entry'));
  }
  function subscriptionEntryTitle(entry) {
    const label = entry.dataset.kind === 'group'
      ? entry.querySelector(':scope > summary .group-link > span:first-child')
      : entry.querySelector(':scope > .feed-label');
    return label?.textContent.trim() || 'source';
  }
  function groupDestinationLabel(group, groupsById) {
    const names = [];
    const visited = new Set();
    let current = group;
    while (current && !visited.has(current.dataset.id)) {
      visited.add(current.dataset.id);
      names.unshift(subscriptionEntryTitle(current));
      current = current.dataset.parentId ? groupsById.get(current.dataset.parentId) : null;
    }
    return names.join(' / ');
  }
  function syncMoveSubscriptionDialog() {
    if (!moveSubscriptionForm || !moveSubscriptionSelect || !moveSubscriptionSubmit) return;
    const unchanged = moveSubscriptionSelect.value === (moveSubscriptionForm.dataset.currentParentId || '');
    moveSubscriptionSubmit.disabled = moveDialogRequestPending || unchanged;
    moveSubscriptionSelect.disabled = moveDialogRequestPending;
    moveSubscriptionForm.querySelector('.dialog-cancel').disabled = moveDialogRequestPending;
    moveSubscriptionForm.setAttribute('aria-busy', moveDialogRequestPending ? 'true' : 'false');
    if (moveSubscriptionStatus) {
      moveSubscriptionStatus.textContent = unchanged
        ? 'Choose a different destination, or cancel to keep this source here.'
        : 'The source will be placed last in this destination.';
    }
  }
  function openMoveSubscriptionDialog(entry) {
    if (!entry || subscriptionMovePending || !moveSubscriptionForm || !moveSubscriptionSelect) return;
    const groups = [...subscriptionTree.querySelectorAll('.group.subscription-entry')];
    const groupsById = new Map(groups.map(group => [group.dataset.id, group]));
    const currentParentId = entry.dataset.parentId || '';
    moveSubscriptionForm.elements.kind.value = entry.dataset.kind;
    moveSubscriptionForm.elements.id.value = entry.dataset.id;
    moveSubscriptionForm.dataset.currentParentId = currentParentId;
    moveSubscriptionSelect.replaceChildren();
    const rootOption = document.createElement('option');
    rootOption.value = '';
    rootOption.textContent = currentParentId ? 'Top level' : 'Top level (current)';
    rootOption.selected = !currentParentId;
    moveSubscriptionSelect.appendChild(rootOption);
    groups.forEach(group => {
      if (entry.dataset.kind === 'group' && entry.contains(group)) return;
      const option = document.createElement('option');
      option.value = group.dataset.id;
      const current = group.dataset.id === currentParentId;
      option.textContent = `${groupDestinationLabel(group, groupsById)}${current ? ' (current)' : ''}`;
      option.selected = current;
      moveSubscriptionSelect.appendChild(option);
    });
    document.getElementById('move-subscription-heading').textContent = `Move “${subscriptionEntryTitle(entry)}”`;
    moveDialogRequestPending = false;
    syncMoveSubscriptionDialog();
    openDialog(moveSubscriptionDialog);
    moveSubscriptionSelect.focus();
  }
  function entrySubscriptionActions(entry) {
    return entry.dataset.kind === 'group'
      ? entry.querySelector(':scope > summary .subscription-actions')
      : entry.querySelector(':scope > .subscription-actions');
  }
  function syncSubscriptionMoveButtons() {
    const containers = subscriptionTree
      ? [subscriptionTree, ...subscriptionTree.querySelectorAll('.subscription-container')]
      : [];
    containers.forEach(container => {
      const entries = directEntries(container);
      entries.forEach((entry, index) => {
        const actions = entrySubscriptionActions(entry);
        const up = actions?.querySelector('.move-subscription[data-direction="up"]');
        const down = actions?.querySelector('.move-subscription[data-direction="down"]');
        if (up) up.disabled = subscriptionMovePending || index === 0;
        if (down) down.disabled = subscriptionMovePending || index === entries.length - 1;
      });
    });
  }
  function setSubscriptionMovePending(pending, entry = null) {
    subscriptionMovePending = pending;
    subscriptionPane?.setAttribute('aria-busy', pending ? 'true' : 'false');
    subscriptionTree?.querySelectorAll('.subscription-moving').forEach(element => element.classList.remove('subscription-moving'));
    if (pending) entry?.classList.add('subscription-moving');
    subscriptionTree?.querySelectorAll('.subscription-actions button').forEach(button => { button.disabled = pending; });
    subscriptionTree?.querySelectorAll('.subscription-actions a').forEach(link => {
      if (pending) { link.setAttribute('aria-disabled', 'true'); link.setAttribute('tabindex', '-1'); }
      else { link.removeAttribute('aria-disabled'); link.removeAttribute('tabindex'); }
    });
    subscriptionPane?.querySelectorAll('.edit-actions button').forEach(button => { button.disabled = pending; });
    subscriptionPane?.querySelectorAll('.drag-handle').forEach(handle => {
      handle.draggable = subscriptionPane.classList.contains('editing') && !pending;
    });
    if (!pending) syncSubscriptionMoveButtons();
  }
  function clearSubscriptionDropState() {
    subscriptionTree?.querySelectorAll('.drop-before, .drop-after, .drop-inside, .dragging').forEach(entry => {
      entry.classList.remove('drop-before', 'drop-after', 'drop-inside', 'dragging');
    });
  }
  function subscriptionDropIntent(target, event) {
    if (target.dataset.kind === 'group') {
      const summary = target.querySelector(':scope > summary');
      const content = target.querySelector(':scope > .subscription-container');
      const bounds = summary?.getBoundingClientRect();
      const overEmptyContent = event.target === content;
      const overMiddle = bounds && event.clientY >= bounds.top + bounds.height * .24
        && event.clientY <= bounds.bottom - bounds.height * .24;
      if (overEmptyContent || overMiddle) return 'inside';
    }
    const row = target.dataset.kind === 'group'
      ? target.querySelector(':scope > summary') : target;
    const bounds = row.getBoundingClientRect();
    return event.clientY >= bounds.top + bounds.height / 2 ? 'after' : 'before';
  }
  subscriptionTree?.addEventListener('dragstart', event => {
    const handle = event.target.closest('.drag-handle');
    const entry = handle?.closest('.subscription-entry');
    if (!handle || !entry || subscriptionMovePending || !subscriptionPane.classList.contains('editing')) {
      event.preventDefault(); return;
    }
    draggedSubscription = entry; entry.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', `${entry.dataset.kind}:${entry.dataset.id}`);
  });
  subscriptionTree?.addEventListener('dragover', event => {
    if (!draggedSubscription) return;
    const target = event.target.closest('.subscription-entry');
    if (!target || target === draggedSubscription) return;
    event.preventDefault(); event.dataTransfer.dropEffect = 'move';
    subscriptionTree.querySelectorAll('.drop-before, .drop-after, .drop-inside').forEach(entry => {
      entry.classList.remove('drop-before', 'drop-after', 'drop-inside');
    });
    target.classList.add(`drop-${subscriptionDropIntent(target, event)}`);
  });
  subscriptionTree?.addEventListener('drop', async event => {
    const target = event.target.closest('.subscription-entry');
    if (!draggedSubscription || !target || target === draggedSubscription) {
      clearSubscriptionDropState(); draggedSubscription = null; return;
    }
    event.preventDefault();
    const inside = target.classList.contains('drop-inside');
    const container = inside
      ? target.querySelector(':scope > .subscription-container')
      : target.parentElement;
    if (!container?.classList.contains('subscription-container')) {
      clearSubscriptionDropState(); draggedSubscription = null; return;
    }
    const after = target.classList.contains('drop-after');
    const siblings = directEntries(container).filter(entry => entry !== draggedSubscription);
    const targetIndex = inside ? siblings.length : siblings.indexOf(target);
    const position = inside ? siblings.length : targetIndex + (after ? 1 : 0);
    const payload = {
      kind: draggedSubscription.dataset.kind,
      id: Number(draggedSubscription.dataset.id),
      parent_id: container.dataset.parentId ? Number(container.dataset.parentId) : null,
      position,
    };
    const movedEntry = draggedSubscription;
    clearSubscriptionDropState(); draggedSubscription = null;
    setSubscriptionMovePending(true, movedEntry);
    try {
      await api('/api/subscriptions/move', { method: 'POST', body: JSON.stringify(payload) });
      setSubscriptionMovePending(false);
      sessionStorage.setItem('subscriptionEditMode', subscriptionPane.classList.contains('editing') ? '1' : '0');
      location.reload();
    } catch (error) { setSubscriptionMovePending(false); notify(error.message); }
  });
  subscriptionTree?.addEventListener('dragend', () => {
    clearSubscriptionDropState(); draggedSubscription = null;
  });
  document.querySelectorAll('.move-subscription').forEach(button => button.addEventListener('click', async event => {
    event.preventDefault(); event.stopPropagation();
    if (subscriptionMovePending || button.disabled) return;
    const entry = button.closest('.subscription-entry');
    const container = entry?.parentElement;
    if (!entry || !container?.classList.contains('subscription-container')) return;
    const siblings = directEntries(container); const current = siblings.indexOf(entry);
    const position = current + (button.dataset.direction === 'up' ? -1 : 1);
    if (position < 0 || position >= siblings.length) return;
    setSubscriptionMovePending(true, entry);
    try {
      await api('/api/subscriptions/move', { method: 'POST', body: JSON.stringify({
        kind: entry.dataset.kind, id: Number(entry.dataset.id),
        parent_id: container.dataset.parentId ? Number(container.dataset.parentId) : null,
        position,
      }) });
      setSubscriptionMovePending(false);
      sessionStorage.setItem('subscriptionEditMode', subscriptionPane.classList.contains('editing') ? '1' : '0');
      location.reload();
    } catch (error) { setSubscriptionMovePending(false); button.focus(); notify(error.message); }
  }));
  document.querySelectorAll('.move-parent-subscription').forEach(button => button.addEventListener('click', event => {
    event.preventDefault(); event.stopPropagation();
    openMoveSubscriptionDialog(button.closest('.subscription-entry'));
  }));
  moveSubscriptionSelect?.addEventListener('change', syncMoveSubscriptionDialog);
  moveSubscriptionDialog?.addEventListener('cancel', event => {
    if (moveDialogRequestPending) event.preventDefault();
  });
  moveSubscriptionForm?.addEventListener('submit', async event => {
    event.preventDefault();
    if (subscriptionMovePending || moveDialogRequestPending) return;
    const kind = moveSubscriptionForm.elements.kind.value;
    const id = moveSubscriptionForm.elements.id.value;
    const entry = subscriptionTree.querySelector(`.subscription-entry[data-kind="${kind}"][data-id="${id}"]`);
    if (!entry) {
      moveSubscriptionDialog.close();
      notify('The subscription list changed. Reopen Manage and try again.');
      return;
    }
    const parentValue = moveSubscriptionSelect.value;
    if (parentValue === (entry.dataset.parentId || '')) {
      syncMoveSubscriptionDialog();
      return;
    }
    moveDialogRequestPending = true;
    setSubscriptionMovePending(true, entry);
    syncMoveSubscriptionDialog();
    try {
      await api('/api/subscriptions/move', { method: 'POST', body: JSON.stringify({
        kind, id: Number(id), parent_id: parentValue ? Number(parentValue) : null,
        position: 2147483647,
      }) });
      moveDialogRequestPending = false;
      setSubscriptionMovePending(false);
      moveSubscriptionDialog.close();
      sessionStorage.setItem('subscriptionEditMode', subscriptionPane.classList.contains('editing') ? '1' : '0');
      location.reload();
    } catch (error) {
      moveDialogRequestPending = false;
      setSubscriptionMovePending(false);
      syncMoveSubscriptionDialog();
      moveSubscriptionSelect.focus();
      notify(error.message);
    }
  });
  syncSubscriptionMoveButtons();
  function formObject(form) {
    const output = Object.fromEntries(new FormData(form).entries());
    form.querySelectorAll('input[type="checkbox"][name]').forEach(input => { output[input.name] = input.checked; });
    return output;
  }
  document.getElementById('group-form')?.addEventListener('submit', async event => {
    event.preventDefault(); try { await api('/api/groups', { method: 'POST', body: JSON.stringify(formObject(event.target)) }); sessionStorage.setItem('subscriptionEditMode', '1'); location.reload(); } catch (error) { notify(error.message); }
  });
  document.getElementById('feed-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const result = await api('/api/feeds', { method: 'POST', body: JSON.stringify(formObject(event.target)) });
      document.getElementById('feed-dialog')?.close();
      sessionStorage.setItem('subscriptionEditMode', '1');
      startJob('/api/refresh', { feed_id: result.feed_id, force: true }, 'Saving and checking the new feed…');
    } catch (error) { notify(error.message); }
  });
  function extractNtfyScopePolicy(values) {
    const modePath = 'notifications.ntfy.scope_mode';
    const mode = String(values[modePath] || 'all');
    delete values[modePath];
    const rulePattern = /^notifications\.ntfy\.scopes\.(group|feed)\.(\d+)\.(enabled|threshold)$/;
    const states = new Map();
    Object.keys(values).forEach(path => {
      const match = path.match(rulePattern);
      if (!match) return;
      const key = `${match[1]}:${match[2]}`;
      const state = states.get(key) || { scope_kind: match[1], scope_id: Number(match[2]) };
      state[match[3]] = values[path]; states.set(key, state); delete values[path];
    });
    const rules = [...states.values()].filter(rule => Boolean(rule.enabled)).map(rule => {
      const threshold = Number(rule.threshold);
      if (!Number.isInteger(threshold) || threshold < 0 || threshold > 100) {
        throw new Error('Every selected ntfy source threshold must be between 0 and 100.');
      }
      return {
        scope_kind: rule.scope_kind,
        scope_id: rule.scope_id,
        minimum_relevance: threshold,
      };
    }).sort((left, right) => left.scope_kind.localeCompare(right.scope_kind) || left.scope_id - right.scope_id);
    return { mode, rules };
  }
  settingsForm?.addEventListener('submit', async event => {
    event.preventDefault(); const values = {};
    event.target.querySelectorAll('[data-config-path]').forEach(input => {
      values[input.dataset.configPath] = input.dataset.type === 'bool' ? input.checked : input.value;
    });
    setSettingsSaving(true);
    try {
      const ntfyScopePolicy = extractNtfyScopePolicy(values);
      const result = await api('/api/config', { method: 'POST', body: JSON.stringify({ values, ntfy_scope_policy: ntfyScopePolicy }) });
      let message = result.restart_recommended ? 'Saved. Restart DistillFeed to apply server changes.' : 'Saved. You can continue editing or test device alerts.';
      if (result.plugin_state_changed) message = 'Saved. Close Settings, then reopen it to see the updated arXiv controls.';
      if (values['ui.completion_notifications'] && 'Notification' in window && Notification.permission === 'default') {
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') message += ' Browser completion alerts were not allowed.';
      }
      notificationsEnabled = Boolean(values['ui.completion_notifications']);
      configureAutomaticRefresh(
        Boolean(values['app.auto_refresh_on_load']),
        Number(values['app.refresh_interval_minutes']),
        Boolean(values['app.auto_refresh_on_load']),
      );
      snapshotSettings(); settingsSavedSinceOpen = true; setSettingsSaving(false); updateSettingsActions(message);
      notify(message);
    } catch (error) {
      setSettingsSaving(false);
      if (settingsStatus) { settingsStatus.classList.add('error'); settingsStatus.textContent = error.message; }
      notify(error.message);
    }
  });
  ntfyTestButton?.addEventListener('click', async () => {
    if (settingsDirty) { updateSettingsActions('Save device-alert changes before testing.'); return; }
    ntfyTestButton.disabled = true;
    try {
      await api('/api/notifications/ntfy/test', { method: 'POST', body: JSON.stringify({}) });
      updateSettingsActions('Test alert sent through ntfy.');
      notify('Test alert sent through ntfy');
    } catch (error) {
      if (settingsStatus) { settingsStatus.classList.add('error'); settingsStatus.textContent = error.message; }
      notify(error.message);
    } finally { updateSettingsActions(); }
  });
  document.querySelectorAll('.plugin-settings-action').forEach(button => button.addEventListener('click', async () => {
    if (settingsDirty) { updateSettingsActions('Save plugin changes before testing.'); return; }
    button.disabled = true;
    try {
      const result = await api(button.dataset.url, { method: 'POST', body: JSON.stringify({}) });
      const message = result.message || 'Plugin test completed successfully.';
      updateSettingsActions(message); notify(message);
    } catch (error) {
      if (settingsStatus) { settingsStatus.classList.add('error'); settingsStatus.textContent = error.message; }
      notify(error.message);
    } finally { button.disabled = false; updateSettingsActions(); }
  }));

  function installFontControl(id, cssVariable, configPath) {
    const input = document.getElementById(id); if (!input) return;
    document.documentElement.style.setProperty(cssVariable, `${input.value}px`);
    input.addEventListener('input', () => {
      document.documentElement.style.setProperty(cssVariable, `${input.value}px`);
    });
  }
  installFontControl('font-size', '--item-size', 'ui.item_font_size');
  installFontControl('subscription-font-size', '--subscription-size', 'ui.subscription_font_size');
  installFontControl('summary-font-size', '--summary-size', 'ui.summary_font_size');

  const itemSort = document.getElementById('item-sort');
  const itemDayFormatter = new Intl.DateTimeFormat(undefined, { day: '2-digit', month: '2-digit' });
  const itemDetailDateFormatter = new Intl.DateTimeFormat(undefined, {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  function itemDay(row) {
    const date = new Date(row.dataset.date || '');
    if (Number.isNaN(date.getTime())) return { key: '0000-00-00', label: 'Undated' };
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return { key: `${year}-${month}-${day}`, label: itemDayFormatter.format(date) };
  }
  function updateDateGroupVisibility() {
    document.querySelectorAll('.item-day-group').forEach(group => {
      const visible = [...group.querySelectorAll('.item-row')].filter(row => !row.hidden).length;
      group.hidden = visible === 0;
      const heading = group.querySelector('.item-day-heading');
      if (heading) heading.textContent = `${group.dataset.dayLabel} · ${visible} item${visible === 1 ? '' : 's'}`;
    });
  }
  function sortItems(mode) {
    const list = document.querySelector('.item-list'); if (!list) return;
    const rows = [...list.querySelectorAll('.item-row')];
    rows.sort((a, b) => {
      const dayOrder = itemDay(b).key.localeCompare(itemDay(a).key);
      if (dayOrder) return dayOrder;
      if (mode === 'relevance') {
        const relevanceOrder = Number(b.dataset.relevance) - Number(a.dataset.relevance);
        if (relevanceOrder) return relevanceOrder;
      }
      return (b.dataset.date || '').localeCompare(a.dataset.date || '');
    });
    const oldGroups = [...list.querySelectorAll('.item-day-group')];
    const fragment = document.createDocumentFragment();
    let group;
    let previousKey;
    rows.forEach(row => {
      const day = itemDay(row);
      if (day.key !== previousKey) {
        group = document.createElement('section');
        group.className = 'item-day-group'; group.dataset.dayKey = day.key; group.dataset.dayLabel = day.label;
        const heading = document.createElement('h2');
        heading.className = 'item-day-heading'; heading.id = `item-day-${day.key}`;
        group.setAttribute('aria-labelledby', heading.id); group.appendChild(heading); fragment.appendChild(group);
        previousKey = day.key;
      }
      group.appendChild(row);
    });
    oldGroups.forEach(existing => existing.remove());
    list.appendChild(fragment);
    updateDateGroupVisibility();
  }
  if (itemSort) {
    const profile = document.querySelector('.item-list')?.dataset.sortProfile || 'date';
    const storageKey = `rssItemSort:${profile}`;
    itemSort.value = localStorage.getItem(storageKey) || profile;
    sortItems(itemSort.value);
    itemSort.addEventListener('change', () => { localStorage.setItem(storageKey, itemSort.value); sortItems(itemSort.value); });
  }

  document.querySelectorAll('.item-detail-date').forEach(time => {
    const date = new Date(time.dateTime || '');
    if (!Number.isNaN(date.getTime())) time.textContent = itemDetailDateFormatter.format(date);
  });
  document.querySelectorAll('.item-details-toggle').forEach(button => button.addEventListener('click', () => {
    const details = document.getElementById(button.getAttribute('aria-controls'));
    if (!details) return;
    const expanded = button.getAttribute('aria-expanded') !== 'true';
    const title = button.closest('.item-row')?.querySelector('.item-title')?.textContent?.trim() || 'item';
    button.setAttribute('aria-expanded', String(expanded));
    button.setAttribute('aria-label', `${expanded ? 'Hide' : 'Show'} details for ${title}`);
    details.hidden = !expanded;
  }));

  const itemSearch = document.getElementById('item-search');
  const itemFilter = document.getElementById('item-filter');
  const visibleCount = document.getElementById('visible-count');
  const selectVisible = document.getElementById('select-visible-items');
  const markSelected = document.getElementById('mark-selected-read');
  const markSelectedUnread = document.getElementById('mark-selected-unread');
  const saveSelectedLater = document.getElementById('save-selected-later');
  const favoriteSelected = document.getElementById('favorite-selected');
  const tagSelected = document.getElementById('tag-selected');
  const selectedActions = document.getElementById('selected-actions');
  const selectedCount = document.getElementById('selected-count');
  function updateSelectionState() {
    const visible = [...document.querySelectorAll('.item-row:not([hidden])')];
    const selectedVisible = visible.filter(row => row.querySelector('.item-select')?.checked);
    const selected = selectedRows();
    if (selectVisible) { selectVisible.checked = visible.length > 0 && selectedVisible.length === visible.length; selectVisible.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visible.length; }
    if (markSelected) markSelected.disabled = selected.length === 0;
    if (markSelectedUnread) markSelectedUnread.disabled = selected.length === 0;
    if (saveSelectedLater) saveSelectedLater.disabled = selected.length === 0;
    if (favoriteSelected) favoriteSelected.disabled = selected.length === 0;
    if (tagSelected) tagSelected.disabled = selected.length === 0;
    if (selectedActions) selectedActions.classList.toggle('disabled', selected.length === 0);
    if (selectedCount) selectedCount.textContent = selected.length ? `(${selected.length})` : '';
  }
  function applyItemFilters() {
    const query = (itemSearch?.value || '').trim().toLocaleLowerCase();
    const mode = itemFilter?.value || 'all'; let visible = 0;
    document.querySelectorAll('.item-row').forEach(row => {
      const searchable = `${row.textContent} ${row.dataset.tags || ''}`.toLocaleLowerCase();
      const matchesText = !query || searchable.includes(query);
      const matchesState = mode === 'all' || (mode === 'unread' && row.dataset.read === '0') || (mode === 'starred' && row.dataset.starred === '1') || (mode === 'read-later' && row.dataset.readLater === '1');
      row.hidden = !(matchesText && matchesState);
      if (!row.hidden) visible += 1;
    });
    updateDateGroupVisibility();
    if (visibleCount) {
      visibleCount.textContent = `${visible.toLocaleString()} shown`;
      visibleCount.setAttribute('aria-label', `${visible.toLocaleString()} visible items`);
    }
    if (saveSelectedLater) saveSelectedLater.textContent = mode === 'read-later' ? 'Remove from Read later' : 'Add to Read later';
    updateSelectionState();
  }
  itemSearch?.addEventListener('input', applyItemFilters);
  itemFilter?.addEventListener('change', applyItemFilters);
  applyItemFilters();
  document.querySelectorAll('.item-select').forEach(checkbox => checkbox.addEventListener('change', updateSelectionState));
  selectVisible?.addEventListener('change', () => {
    document.querySelectorAll('.item-row:not([hidden]) .item-select').forEach(checkbox => { checkbox.checked = selectVisible.checked; });
    updateSelectionState();
  });
  function setRowsReadState(rows, read) {
    rows.forEach(row => {
      row.dataset.read = read ? '1' : '0'; row.classList.toggle('read', read);
      const button = row.querySelector('.read-button');
      if (button) {
        button.dataset.read = read ? '1' : '0'; button.textContent = read ? '○' : '●';
        button.setAttribute('aria-label', read ? 'Mark as unread' : 'Mark as read');
      }
    });
    applyItemFilters();
  }
  function selectedRows() {
    return [...document.querySelectorAll('.item-row')].filter(
      row => row.querySelector('.item-select')?.checked
    );
  }
  async function runSelectedReadTransition(read) {
    const rows = selectedRows();
    try {
      const result = await api('/api/items/bulk-read', {
        method: 'POST', body: JSON.stringify({ mode: 'selected', item_ids: rows.map(row => Number(row.dataset.itemId)), read }),
      });
      const confirmed = new Set(result.item_ids || []);
      const confirmedRows = rows.filter(row => confirmed.has(Number(row.dataset.itemId)));
      confirmedRows.forEach(row => { row.querySelector('.item-select').checked = false; });
      setRowsReadState(confirmedRows, read);
      if (selectedActions) selectedActions.open = false;
      notify(`${result.changed} item${result.changed === 1 ? '' : 's'} changed; ${result.matched} selected item${result.matched === 1 ? '' : 's'} confirmed ${read ? 'read' : 'unread'}`);
    } catch (error) { notify(error.message); }
  }
  markSelected?.addEventListener('click', () => runSelectedReadTransition(true));
  markSelectedUnread?.addEventListener('click', () => runSelectedReadTransition(false));
  saveSelectedLater?.addEventListener('click', async () => {
    const rows = selectedRows();
    const add = itemFilter?.value !== 'read-later';
    try {
      const result = await api('/api/items/bulk-read-later', { method: 'POST', body: JSON.stringify({ item_ids: rows.map(row => Number(row.dataset.itemId)), read_later: add }) });
      const confirmed = new Set(result.item_ids || []);
      rows.filter(row => confirmed.has(Number(row.dataset.itemId))).forEach(row => { row.dataset.readLater = add ? '1' : '0'; row.querySelector('.item-select').checked = false; });
      if (selectedActions) selectedActions.open = false;
      applyItemFilters(); notify(add ? `${result.changed} items added to Read later` : `${result.changed} items removed from Read later`);
    } catch (error) { notify(error.message); }
  });
  favoriteSelected?.addEventListener('click', async () => {
    const rows = selectedRows();
    try {
      const result = await api('/api/items/bulk-star', { method: 'POST', body: JSON.stringify({ item_ids: rows.map(row => Number(row.dataset.itemId)), starred: true }) });
      const confirmed = new Set(result.item_ids || []);
      rows.filter(row => confirmed.has(Number(row.dataset.itemId))).forEach(row => {
        row.dataset.starred = '1'; row.querySelector('.item-select').checked = false;
        const button = row.querySelector('.star-button');
        if (button) { button.dataset.starred = '1'; button.classList.add('starred'); button.setAttribute('aria-label', 'Remove favorite'); }
      });
      if (selectedActions) selectedActions.open = false;
      applyItemFilters(); notify(`${result.changed} items added to Favorites`);
    } catch (error) { notify(error.message); }
  });
  const tagDialog = document.getElementById('tag-dialog');
  tagSelected?.addEventListener('click', () => openDialog(tagDialog));
  document.getElementById('tag-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const rows = selectedRows();
    const tags = event.target.elements.tags.value.split(',').map(value => value.trim()).filter(Boolean);
    try {
      const result = await api('/api/items/bulk-tags', { method: 'POST', body: JSON.stringify({ item_ids: rows.map(row => Number(row.dataset.itemId)), tags }) });
      const confirmed = new Set(result.item_ids || []);
      rows.filter(row => confirmed.has(Number(row.dataset.itemId))).forEach(row => { row.dataset.tags = result.tags.join(' · '); row.querySelector('.item-select').checked = false; });
      tagDialog.close(); if (selectedActions) selectedActions.open = false; event.target.reset(); updateSelectionState(); notify('Tags saved');
    } catch (error) { notify(error.message); }
  });
  async function runScopeReadTransition(read) {
    const feedId = document.querySelector('meta[name="selected-feed-id"]')?.content;
    const groupId = document.querySelector('meta[name="selected-group-id"]')?.content;
    const label = document.getElementById('items-scope-title')?.textContent || 'this selection';
    if (!feedId && !groupId) { notify('Choose a feed or group before using a scope action'); return; }
    if (!await askConfirmation(
      `Mark every item as ${read ? 'read' : 'unread'}?`,
      `This changes every stored item in “${label}”, including items hidden by the current search or filter.`,
      `Mark all as ${read ? 'read' : 'unread'}`,
    )) return;
    const payload = { mode: 'scope', read };
    if (feedId) payload.feed_id = Number(feedId); else if (groupId) payload.group_id = Number(groupId);
    try {
      const result = await api('/api/items/bulk-read', { method: 'POST', body: JSON.stringify(payload) });
      setRowsReadState([...document.querySelectorAll('.item-row')], read);
      notify(`${result.changed} of ${result.matched} items changed to ${read ? 'read' : 'unread'}`);
    } catch (error) { notify(error.message); }
  }
  document.getElementById('mark-scope-read')?.addEventListener('click', () => runScopeReadTransition(true));
  document.getElementById('mark-scope-unread')?.addEventListener('click', () => runScopeReadTransition(false));

  const popupMenus = [...document.querySelectorAll('details.action-menu, details.nav-menu, details.summary-ai-config')];
  const popupBackdrop = document.getElementById('popup-backdrop');
  syncPopupBackdrop = () => {
    const actionSheetOpen = !settingsDesktop.matches
      && popupMenus.some(menu => menu.classList.contains('action-menu') && menu.open);
    if (popupBackdrop) popupBackdrop.hidden = !actionSheetOpen;
  };
  popupMenus.forEach(menu => menu.addEventListener('toggle', () => {
    menu.querySelector(':scope > summary')?.setAttribute('aria-expanded', menu.open ? 'true' : 'false');
    if (menu.open) { closePopupMenus(menu); setSubscriptionsOpen(false); }
    syncPopupBackdrop();
  }));
  popupMenus.forEach(menu => menu.querySelector(':scope > summary')?.setAttribute('aria-expanded', 'false'));
  popupMenus.forEach(menu => menu.querySelectorAll('a, button').forEach(control => {
    control.addEventListener('click', () => { menu.open = false; });
  }));
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('details.action-menu, details.nav-menu, details.summary-ai-config')) closePopupMenus();
  });
  popupBackdrop?.addEventListener('click', () => closePopupMenus());
  document.addEventListener('focusin', event => {
    if (!event.target.closest('details.action-menu, details.nav-menu, details.summary-ai-config')) closePopupMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closePopupMenus();
  });
  window.addEventListener('resize', () => closePopupMenus());
  document.addEventListener('scroll', () => closePopupMenus(), true);

  document.querySelectorAll('.pane-resizer').forEach(handle => handle.addEventListener('pointerdown', event => {
    if (handle.dataset.resizer === 'left' && subscriptionPane?.classList.contains('editing')) return;
    event.preventDefault(); handle.setPointerCapture(event.pointerId); handle.classList.add('dragging');
    const startX = event.clientX; const styles = getComputedStyle(document.documentElement);
    const startLeft = parseFloat(styles.getPropertyValue('--left-width')); const startMiddle = parseFloat(styles.getPropertyValue('--middle-width'));
    const move = moveEvent => {
      const delta = moveEvent.clientX - startX;
      if (handle.dataset.resizer === 'left') { const width = Math.max(190, Math.min(window.innerWidth * .45, startLeft + delta)); document.documentElement.style.setProperty('--left-width', `${width}px`); localStorage.setItem('rssLeftWidth', Math.round(width)); }
      else { const width = Math.max(300, Math.min(window.innerWidth * .65, startMiddle + delta)); document.documentElement.style.setProperty('--middle-width', `${width}px`); localStorage.setItem('rssMiddleWidth', Math.round(width)); }
    };
    const up = () => { handle.classList.remove('dragging'); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
  }));

  const subscriptions = document.getElementById('subscriptions');
  const subscriptionToggle = document.getElementById('menu-toggle');
  const subscriptionBackdrop = document.getElementById('subscription-backdrop');
  function setSubscriptionsOpen(open, { returnFocus = false } = {}) {
    const mobileOpen = Boolean(open && !settingsDesktop.matches);
    if (mobileOpen) closePopupMenus();
    subscriptions?.classList.toggle('open', mobileOpen);
    subscriptionToggle?.setAttribute('aria-expanded', mobileOpen ? 'true' : 'false');
    if (subscriptionBackdrop) subscriptionBackdrop.hidden = !mobileOpen;
    if (returnFocus && !mobileOpen) subscriptionToggle?.focus();
  }
  const selectedSubscription = subscriptions?.querySelector('.feed-label.selected, .group-link.selected');
  if (selectedSubscription) {
    let group = selectedSubscription.closest('details.group');
    if (selectedSubscription.classList.contains('group-link')) group = group?.parentElement?.closest('details.group');
    while (group) { group.open = true; group = group.parentElement?.closest('details.group'); }
  }
  subscriptionToggle?.setAttribute('aria-expanded', 'false');
  subscriptionToggle?.addEventListener('click', () => setSubscriptionsOpen(!subscriptions.classList.contains('open')));
  subscriptionBackdrop?.addEventListener('click', () => setSubscriptionsOpen(false, { returnFocus: true }));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && subscriptions?.classList.contains('open')) {
      setSubscriptionsOpen(false, { returnFocus: true });
    }
  });
  if (settingsDesktop.addEventListener) settingsDesktop.addEventListener('change', () => setSubscriptionsOpen(false));
  else settingsDesktop.addListener(() => setSubscriptionsOpen(false));
  document.querySelectorAll('.mobile-tabs button').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.mobile-tabs button').forEach(item => item.classList.toggle('active', item === button));
    document.querySelectorAll('[data-pane]').forEach(pane => pane.classList.toggle('view-active', pane.dataset.pane === button.dataset.view)); setSubscriptionsOpen(false);
  }));
  const itemRows = [...document.querySelectorAll('.item-row')]; let keyboardIndex = -1;
  function focusItem(index) {
    const visible = itemRows.filter(row => !row.hidden); if (!visible.length) return;
    keyboardIndex = Math.max(0, Math.min(index, visible.length - 1));
    itemRows.forEach(row => row.classList.remove('keyboard-current'));
    visible[keyboardIndex].classList.add('keyboard-current'); visible[keyboardIndex].focus({ preventScroll: true }); visible[keyboardIndex].scrollIntoView({ block: 'nearest' });
  }
  document.addEventListener('keydown', event => {
    if (event.target instanceof Element && (
      event.target.matches('input, textarea, select') || event.target.closest('dialog[open]')
    )) return;
    const visible = itemRows.filter(row => !row.hidden); const current = visible[keyboardIndex];
    if (event.key === 'j') { event.preventDefault(); focusItem(keyboardIndex + 1); }
    else if (event.key === 'k') { event.preventDefault(); focusItem(keyboardIndex < 0 ? 0 : keyboardIndex - 1); }
    else if (current && (event.key === 'o' || event.key === 'Enter')) current.querySelector('.item-title[href]')?.click();
    else if (current && event.key === 'm') current.querySelector('.read-button')?.click();
    else if (current && event.key === 's') current.querySelector('.star-button')?.click();
    else if (current && event.key === 'l') {
      const selector = current.querySelector('.item-select');
      if (selector) { selector.checked = true; updateSelectionState(); saveSelectedLater?.click(); }
      else current.querySelector('.read-later-button')?.click();
    }
  });
  itemRows.forEach(row => {
    let startX = 0; let startY = 0;
    row.addEventListener('pointerdown', event => { if (event.pointerType === 'touch') { startX = event.clientX; startY = event.clientY; } });
    row.addEventListener('pointerup', event => {
      if (event.pointerType !== 'touch' || Math.abs(event.clientY - startY) > 35) return;
      const distance = event.clientX - startX;
      if (distance < -70) row.querySelector('.read-button')?.click();
      if (distance > 70) row.querySelector('.star-button')?.click();
    });
  });
  if ('serviceWorker' in navigator) {
    const offline = document.querySelector('meta[name="offline-cache-enabled"]')?.content === 'true';
    if (offline) navigator.serviceWorker.register('/static/service-worker.js').catch(() => {});
    else navigator.serviceWorker.getRegistrations().then(registrations => registrations.forEach(registration => registration.unregister()));
  }
  if (document.querySelectorAll('.top-actions button:disabled').length) monitorJobs(true);
})();
