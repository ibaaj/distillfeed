(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.DistillFeedReaderCore = api; api.install(root); }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function videoId(value) {
    try {
      const url = new URL(String(value));
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return '';
      const hosts = ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'];
      const parts = url.pathname.replace(/^\/+|\/+$/g, '').split('/');
      let id = '';
      if (['youtu.be', 'www.youtu.be'].includes(url.hostname) && parts.length === 1) id = parts[0];
      else if (hosts.includes(url.hostname)) {
        if (url.pathname === '/watch') id = url.searchParams.get('v') || '';
        else if (parts.length === 2 && ['shorts', 'live', 'embed'].includes(parts[0])) id = parts[1];
      }
      return /^[A-Za-z0-9_-]{11}$/.test(id) ? id : '';
    } catch (_) { return ''; }
  }
  function articleURL(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
    } catch (_) { return ''; }
  }
  function numberOption(value, fallback, low, high) {
    const parsed = Number(value ?? fallback);
    return Number.isFinite(parsed) ? Math.max(low, Math.min(high, parsed)) : fallback;
  }
  function chunksFor(text, limit = 600) {
    const maximum = Math.trunc(numberOption(limit, 600, 180, 2400));
    let remaining = String(text || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 250000);
    const chunks = [];
    while (remaining.length > maximum) {
      const head = remaining.slice(0, maximum + 1);
      const boundaries = [...head.matchAll(/[.!?…;:]\s/g)].map(match => match.index + 1);
      let end = boundaries.filter(index => index >= maximum / 3 && index <= maximum).at(-1);
      if (!end) end = head.lastIndexOf(' ', maximum);
      if (!end || end < maximum / 3) end = maximum;
      // Do not split a UTF-16 surrogate pair in a long word.
      if (/[\uD800-\uDBFF]/.test(remaining[end - 1])) end -= 1;
      chunks.push(remaining.slice(0, end).trim()); remaining = remaining.slice(end).trim();
    }
    if (remaining) chunks.push(remaining);
    return chunks;
  }
  function createSerialSaver(save, onState = () => {}) {
    let next = null; let running = false; let revision = 0;
    async function pump() {
      if (running) return;
      running = true;
      while (next) {
        const request = next; next = null;
        try { await save(request.value); onState({ status: 'saved', value: request.value, current: request.revision === revision }); }
        catch (error) { onState({ status: 'error', value: request.value, current: request.revision === revision, error }); }
      }
      running = false;
    }
    return {
      save(value) { revision += 1; next = { value, revision }; onState({ status: 'saving', value, current: true }); return pump(); },
      state() { return { running, queued: Boolean(next), revision }; },
    };
  }
  function createSpeechController(synth, Utterance, onState = () => {}, options = {}) {
    const volume = numberOption(options.volume, 0.65, 0, 1);
    const rate = numberOption(options.rate, 1, 0.5, 2);
    const chunkSize = numberOption(options.chunkChars, 600, 180, 2400);
    const gap = numberOption(options.pauseMs, 120, 0, 1000);
    const schedule = options.setTimeout || setTimeout;
    const unschedule = options.clearTimeout || clearTimeout;
    let generation = 0; let chunks = []; let voice = null; let status = 'idle'; let current = null; let timer = null;
    function emit(next, message = '') { status = next; onState({ status, message, remaining: chunks.length }); }
    function clearTimer() { if (timer !== null) { unschedule(timer); timer = null; } }
    function stop() {
      const active = Boolean(current || synth?.speaking || synth?.pending);
      generation += 1; clearTimer(); chunks = []; current = null;
      if (active) { try { synth?.cancel(); } catch (_) { /* shutdown remains authoritative */ } }
      emit('idle'); return active;
    }
    function queueNext(token, delay) {
      clearTimer(); emit('waiting');
      timer = schedule(() => { timer = null; if (token === generation && status !== 'paused') next(token); }, delay);
    }
    function next(token) {
      if (token !== generation || status === 'paused') return;
      if (!chunks.length) { current = null; emit('idle', 'Finished'); return; }
      const utterance = new Utterance(chunks.shift()); current = utterance;
      utterance.voice = voice; utterance.volume = volume; utterance.rate = rate;
      if (voice?.lang) utterance.lang = voice.lang;
      let settled = false;
      utterance.onend = () => {
        if (settled || token !== generation || current !== utterance) return;
        settled = true; current = null;
        if (!chunks.length) { emit('idle', 'Finished'); return; }
        if (status !== 'paused') queueNext(token, gap);
      };
      utterance.onerror = event => {
        if (settled || token !== generation || current !== utterance) return;
        settled = true; stop(); emit('error', `Read aloud stopped: ${event?.error || 'voice unavailable'}. Try another local voice or browser.`);
      };
      emit('speaking');
      try { synth.speak(utterance); } catch (error) { utterance.onerror({ error: error.message || 'synthesis-failed' }); }
    }
    return {
      speak(text, selectedVoice) {
        const interrupted = stop();
        if (!synth || !Utterance || !selectedVoice?.localService) { emit('unavailable', 'No local system voice is exposed by this browser.'); return false; }
        voice = selectedVoice; chunks = chunksFor(text, chunkSize);
        if (!chunks.length) { emit('idle', 'No text is available here.'); return false; }
        if (volume === 0) { chunks = []; emit('idle', 'Read-aloud volume is zero. Change it in Reader & audio settings.'); return false; }
        try { if (synth.paused) synth.resume(); } catch (_) { /* speak will report failure */ }
        // A new user request may interrupt old speech; never overlap cancel/start.
        if (interrupted) queueNext(generation, Math.max(150, gap)); else next(generation);
        return true;
      },
      pauseResume() {
        if (status === 'waiting') { clearTimer(); emit('paused'); }
        else if (status === 'speaking') { try { synth.pause(); emit('paused'); } catch (_) { stop(); emit('error', 'Pause is unavailable in this browser. Reading stopped.'); } }
        else if (status === 'paused') {
          if (current) { try { synth.resume(); emit('speaking'); } catch (_) { stop(); emit('error', 'Resume is unavailable in this browser.'); } }
          else queueNext(generation, gap);
        }
      },
      stop, state: () => ({ status, generation, remaining: chunks.length }),
    };
  }
  function install(win) {
    const doc = win.document; if (!doc) return;
    let preferences = {};
    try { preferences = JSON.parse(doc.getElementById('reader-preferences')?.textContent || '{}'); } catch (_) { /* defaults */ }
    const audioEnabled = preferences.audio_reading_enabled !== false;
    const audioDisclosure = doc.getElementById('review-audio-disclosure');
    const dialog = doc.getElementById('reader-theatre');
    const frameContainer = doc.getElementById('reader-frame-container');
    const speechStatus = doc.getElementById('reader-speech-status');
    const voiceSelect = doc.getElementById('reader-voice');
    const pauseButton = doc.getElementById('reader-pause'); const stopButton = doc.getElementById('reader-stop');
    let voices = []; let returnFocus = null;
    // Hardened browsers may block the Web Speech properties themselves. Audio
    // availability must not prevent filters, settings or item content working.
    let nativeSpeech = null; let NativeUtterance = null;
    if (audioEnabled) {
      try { nativeSpeech = win.speechSynthesis; NativeUtterance = win.SpeechSynthesisUtterance; } catch (_) { /* unavailable */ }
    }
    const speech = createSpeechController(nativeSpeech, NativeUtterance, state => {
      if (speechStatus) speechStatus.textContent = state.message || ({ speaking: 'Reading aloud', paused: 'Paused', waiting: 'Reading…' }[state.status] || '');
      if (pauseButton) { pauseButton.disabled = !['speaking', 'paused', 'waiting'].includes(state.status); pauseButton.textContent = state.status === 'paused' ? 'Resume' : 'Pause'; }
      if (stopButton) stopButton.disabled = !['speaking', 'paused', 'waiting'].includes(state.status);
    }, { volume: preferences.tts_volume, rate: preferences.tts_rate, chunkChars: preferences.tts_chunk_chars, pauseMs: preferences.tts_pause_ms });
    audioDisclosure?.addEventListener('toggle', () => {
      audioDisclosure.querySelector('summary')?.setAttribute('aria-expanded', String(audioDisclosure.open));
      if (!audioDisclosure.open) speech.stop();
      else doc.querySelectorAll('details.action-menu[open], details.nav-menu[open]').forEach(menu => { menu.open = false; });
    });
    doc.getElementById('reader-hide-audio')?.addEventListener('click', () => { audioDisclosure.open = false; audioDisclosure.querySelector('summary')?.focus(); });
    doc.addEventListener('keydown', event => { if (event.key === 'Escape' && audioDisclosure?.open) audioDisclosure.open = false; });
    doc.querySelectorAll('#summary-ai-config, #arxiv-scope-config').forEach(menu => menu.addEventListener('toggle', () => { if (menu.open && audioDisclosure) audioDisclosure.open = false; }));
    function loadVoices() {
      const previous = voiceSelect?.value;
      try { voices = audioEnabled ? (nativeSpeech?.getVoices() || []).filter(voice => voice.localService) : []; } catch (_) { voices = []; }
      if (!voiceSelect) return;
      voiceSelect.replaceChildren();
      voices.forEach(voice => voiceSelect.add(new Option(`${voice.name} (${voice.lang})`, voice.voiceURI)));
      let saved = ''; try { saved = win.localStorage.getItem('distillfeedLocalVoice') || ''; } catch (_) { /* private mode */ }
      const chosen = [previous, saved].find(value => voices.some(voice => voice.voiceURI === value));
      if (chosen) voiceSelect.value = chosen;
      if (!voices.length) { voiceSelect.add(new Option('No local voice available', '')); voiceSelect.disabled = true; }
      else voiceSelect.disabled = false;
    }
    loadVoices();
    try { nativeSpeech?.addEventListener('voiceschanged', loadVoices); } catch (_) { /* load again on a user request */ }
    voiceSelect?.addEventListener('change', () => { try { win.localStorage.setItem('distillfeedLocalVoice', voiceSelect.value); } catch (_) { /* private mode */ } speech.stop(); });
    pauseButton?.addEventListener('click', () => speech.pauseResume()); stopButton?.addEventListener('click', () => speech.stop());
    function speakText(text) {
      if (!audioEnabled) return false;
      if (audioDisclosure) audioDisclosure.open = true;
      loadVoices();
      return speech.speak(text, voices.find(voice => voice.voiceURI === voiceSelect?.value) || voices[0]);
    }
    function speakHTML(html) {
      const fragment = doc.createElement('div'); fragment.innerHTML = String(html || '');
      fragment.querySelectorAll('script,style,iframe').forEach(node => node.remove());
      fragment.querySelectorAll('p,li,h1,h2,h3,h4,div,br').forEach(node => node.appendChild(doc.createTextNode(' ')));
      speakText(fragment.textContent);
    }
    function close() { if (dialog?.open) dialog.close(); else frameContainer?.replaceChildren(); }
    dialog?.addEventListener('close', () => { frameContainer?.replaceChildren(); dialog.classList.remove('wide'); speech.stop(); returnFocus?.focus?.(); });
    doc.getElementById('reader-close')?.addEventListener('click', close);
    doc.getElementById('reader-wide')?.addEventListener('click', event => {
      const wide = dialog?.classList.toggle('wide'); event.currentTarget.textContent = wide ? 'Normal size' : 'Theatre mode';
    });
    function open(item, video = false) {
      if (!dialog || !frameContainer || (video ? preferences.youtube_embed_enabled === false : preferences.article_theatre_enabled === false)) return;
      const id = video ? videoId(item.url) : '';
      const source = articleURL(item.url);
      if ((!id && video) || (!source && !video)) { if (speechStatus) speechStatus.textContent = 'This resource cannot be embedded. Use its title to open it in a new tab.'; return; }
      speech.stop(); returnFocus = doc.activeElement;
      doc.getElementById('reader-theatre-title').textContent = item.title || 'Reader';
      doc.getElementById('reader-external').href = source || `https://www.youtube.com/watch?v=${id}`;
      doc.getElementById('reader-frame-notice').textContent = video
        ? 'The player connects to YouTube only after Play is clicked. Embedding may be disabled by the video owner; use Open original when necessary.'
        : 'Some publishers block embedding, sign-in, or playback in this view. A blank or refused frame is not a loading task: open the original instead. DistillFeed does not bypass publisher restrictions.';
      const frame = doc.createElement('iframe'); frame.title = item.title || 'Embedded resource';
      frame.referrerPolicy = video ? 'strict-origin-when-cross-origin' : 'no-referrer';
      frame.setAttribute('sandbox', video ? 'allow-scripts allow-same-origin allow-presentation allow-popups' : 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox');
      frame.allow = video ? 'fullscreen; encrypted-media; picture-in-picture' : 'fullscreen'; frame.allowFullscreen = true;
      frame.src = video ? `https://www.youtube-nocookie.com/embed/${id}?autoplay=0&rel=0` : source;
      frameContainer.replaceChildren(frame); dialog.classList.toggle('wide', Boolean(preferences.reader_theatre_wide)); doc.getElementById('reader-wide').textContent = preferences.reader_theatre_wide ? 'Normal size' : 'Theatre mode';
      if (!dialog.open) dialog.showModal();
    }
    function stopAll() { speech.stop(); close(); }
    win.addEventListener('pagehide', stopAll);
    win.DistillFeedReader = { open, close, speakText, speakHTML, stopAll };
  }
  return { videoId, articleURL, chunksFor, createSerialSaver, createSpeechController, install };
}));
