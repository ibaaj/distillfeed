(() => {
  "use strict";

  const form = document.getElementById("setup-form");
  const screens = new Map(
    Array.from(document.querySelectorAll("[data-screen]")).map((element) => [element.dataset.screen, element])
  );
  const stepSections = Array.from(document.querySelectorAll(".form-step"));
  const stepButtons = Array.from(document.querySelectorAll("[data-go-step]"));
  const cancelButton = document.getElementById("cancel-setup");
  const globalStatus = document.getElementById("global-status");
  const formError = document.getElementById("form-error");
  const nextButton = document.getElementById("wizard-next");
  const backButton = document.getElementById("wizard-back");
  const createButton = document.getElementById("create-reader");
  const defaultProfile = document.body.dataset.defaultProfile || "recommended";

  const fieldNames = [
    "port", "subscriptions", "language", "interest_profile", "weather_enabled",
    "weather_location", "weather_latitude", "weather_longitude", "weather_timezone",
    "ai_provider", "model", "ollama_url", "summary_threshold", "summary_window_days",
    "candidate_age_days", "review_workload", "monthly_budget_usd", "store_openai_key",
    "openai_key", "use_environment_openai_key", "arxiv_enabled", "arxiv_categories",
    "arxiv_lookback_days", "arxiv_final_threshold", "refresh_on_open", "background_updates",
    "refresh_interval_minutes", "auto_summarize", "ntfy_enabled", "ntfy_server", "ntfy_topic",
    "ntfy_threshold", "store_ntfy_token", "ntfy_token"
  ];
  const booleanFields = new Set([
    "weather_enabled", "store_openai_key", "use_environment_openai_key", "arxiv_enabled",
    "refresh_on_open", "background_updates", "auto_summarize", "ntfy_enabled",
    "store_ntfy_token"
  ]);

  const reviewLabels = new Map([
    ["instance_path", "Local instance folder"],
    ["reader_url", "Reader address"],
    ["access", "Network access"],
    ["subscriptions", "Starting subscriptions"],
    ["language", "Writing language"],
    ["weather", "Weather"],
    ["ai", "AI provider"],
    ["api_key", "API key handling"],
    ["ordinary_threshold", "Ordinary summary threshold"],
    ["ordinary_evidence_window", "Ordinary summary evidence"],
    ["candidate_age_limit", "Unscored candidate age limit"],
    ["monthly_budget", "Monthly local AI guard"],
    ["arxiv", "arXiv daily digest"],
    ["updates", "Updates"],
    ["device_alerts", "Alerts on other devices (ntfy)"]
  ]);

  let csrfToken = "";
  let profile = defaultProfile;
  let currentStep = 0;
  let reviewToken = "";
  let environmentKeyAvailable = false;
  let requestInProgress = false;
  let scheduledScreenFocus = 0;

  class ApiError extends Error {
    constructor(status, payload) {
      super(payload && payload.message ? payload.message : "The setup request failed.");
      this.status = status;
      this.payload = payload || {};
    }
  }

  async function api(path, {method = "GET", body = undefined, csrf = true} = {}) {
    const headers = {"Accept": "application/json"};
    const options = {method, headers, credentials: "same-origin", cache: "no-store"};
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    if (csrf && method !== "GET" && csrfToken) {
      headers["X-DistillFeed-Setup-CSRF"] = csrfToken;
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (_error) {
      throw new ApiError(0, {message: "The private setup server is no longer reachable. Relaunch ./launch.sh to try again."});
    }
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {message: "The setup server returned an unreadable response."};
    }
    if (!response.ok || payload.ok === false) {
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  function showScreen(name, {focusHeading = true} = {}) {
    if (scheduledScreenFocus) {
      window.cancelAnimationFrame(scheduledScreenFocus);
      scheduledScreenFocus = 0;
    }
    screens.forEach((element, key) => {
      element.hidden = key !== name;
    });
    const terminal = ["complete", "cancelled", "recovery", "error"].includes(name);
    cancelButton.hidden = terminal || name === "starting";
    const target = screens.get(name);
    const heading = target ? target.querySelector("h1") : null;
    if (heading && focusHeading) {
      heading.setAttribute("tabindex", "-1");
      scheduledScreenFocus = window.requestAnimationFrame(() => {
        scheduledScreenFocus = 0;
        if (!target.hidden) heading.focus({preventScroll: true});
      });
    }
    window.scrollTo({top: 0, behavior: "smooth"});
  }

  function setStatus(message = "", kind = "") {
    globalStatus.textContent = message;
    globalStatus.className = `status-banner${kind ? ` ${kind}` : ""}`;
    globalStatus.hidden = !message;
  }

  function clearSecretInputs() {
    document.getElementById("openai_key").value = "";
    document.getElementById("ntfy_token").value = "";
  }

  function showRecovery(state = {}, message = "") {
    clearSecretInputs();
    document.getElementById("recovery-message").textContent = message || state.error ||
      "DistillFeed could not confirm that its rollback completed. It stopped instead of applying the settings a second time.";
    document.getElementById("recovery-path").textContent = state.recovery_path || ".distillfeed";
    reviewToken = "";
    setStatus("");
    showScreen("recovery");
  }

  function setBusy(busy, label = "Working…") {
    requestInProgress = busy;
    document.querySelector(".setup-main").setAttribute("aria-busy", String(busy));
    form.toggleAttribute("inert", busy);
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = busy;
    });
    if (!busy) updateConditionalFields();
    if (busy) {
      setStatus(label);
    }
  }

  function fieldControl(name) {
    if (!/^[a-z_]+$/.test(name)) return null;
    return form.querySelector(`[name="${name}"]`);
  }

  function clearFieldErrors() {
    form.querySelectorAll(".field-error").forEach((element) => element.remove());
    form.querySelectorAll("[aria-invalid='true']").forEach((element) => {
      element.removeAttribute("aria-invalid");
      const describedBy = (element.getAttribute("aria-describedby") || "")
        .split(/\s+/)
        .filter((value) => value && !value.startsWith("setup-error-"));
      if (describedBy.length) element.setAttribute("aria-describedby", describedBy.join(" "));
      else element.removeAttribute("aria-describedby");
    });
    formError.hidden = true;
    formError.textContent = "";
  }

  function showFieldErrors(errors = {}) {
    clearFieldErrors();
    let firstControl = null;
    let firstStep = null;
    Object.entries(errors).forEach(([name, message]) => {
      if (name === "form") {
        formError.textContent = String(message);
        formError.hidden = false;
        return;
      }
      const control = fieldControl(name);
      if (!control) return;
      const id = `setup-error-${name}`;
      const error = document.createElement("small");
      error.id = id;
      error.className = "field-error";
      error.textContent = String(message);
      const container = control.closest(".field, .choice-fieldset, .switch-field") || control.parentElement;
      container.appendChild(error);
      const controls = name === "subscriptions" || name === "ai_provider"
        ? form.querySelectorAll(`[name="${name}"]`)
        : [control];
      controls.forEach((item) => {
        item.setAttribute("aria-invalid", "true");
        const previous = item.getAttribute("aria-describedby") || "";
        item.setAttribute("aria-describedby", `${previous} ${id}`.trim());
      });
      if (!firstControl) {
        firstControl = control;
        const step = control.closest("[data-step]");
        firstStep = step ? Number(step.dataset.step) : 0;
      }
    });
    showScreen("wizard", {focusHeading: false});
    if (firstStep !== null) setStep(firstStep, false);
    if (formError.hidden === false && !firstControl) formError.focus();
    else if (firstControl) firstControl.focus();
  }

  function applyPreset(preset) {
    profile = String(preset.profile || "guided");
    fieldNames.forEach((name) => {
      const controls = form.querySelectorAll(`[name="${name}"]`);
      if (!controls.length || !(name in preset)) return;
      if (controls[0].type === "radio") {
        controls.forEach((control) => { control.checked = control.value === String(preset[name]); });
      } else if (booleanFields.has(name)) {
        controls[0].checked = preset[name] === true;
      } else {
        controls[0].value = preset[name] === null || preset[name] === undefined ? "" : String(preset[name]);
      }
    });
    updateConditionalFields();
    clearFieldErrors();
  }

  function collectSettings() {
    const settings = {profile};
    fieldNames.forEach((name) => {
      const controls = form.querySelectorAll(`[name="${name}"]`);
      if (!controls.length) return;
      if (controls[0].type === "radio") {
        const selected = Array.from(controls).find((control) => control.checked);
        settings[name] = selected ? selected.value : "";
      } else if (booleanFields.has(name)) {
        settings[name] = controls[0].checked;
      } else {
        settings[name] = controls[0].value;
      }
    });
    return settings;
  }

  function selectedProvider() {
    const selected = form.querySelector("[name='ai_provider']:checked");
    return selected ? selected.value : "disabled";
  }

  function updateConditionalFields() {
    const provider = selectedProvider();
    document.querySelectorAll("[data-provider-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.providerPanel !== provider;
    });
    document.querySelectorAll("[data-ai-settings]").forEach((element) => {
      element.hidden = provider === "disabled";
    });
    const environmentCheckbox = document.getElementById("use_environment_openai_key");
    environmentCheckbox.disabled = !environmentKeyAvailable;
    if (!environmentKeyAvailable) environmentCheckbox.checked = false;
    document.getElementById("environment-key-status").textContent = environmentKeyAvailable
      ? "OPENAI_API_KEY is available in the launch environment; its value is not shown here."
      : "OPENAI_API_KEY is not available in the launch environment. Enter a key above or configure AI later.";
    document.getElementById("openai_key").disabled = provider === "openai" && environmentCheckbox.checked;
    const arxivEnabled = document.getElementById("arxiv_enabled").checked;
    document.getElementById("arxiv_enabled").setAttribute("aria-expanded", String(arxivEnabled));
    document.querySelectorAll("[data-arxiv-settings]").forEach((element) => {
      element.hidden = !arxivEnabled;
    });
    const backgroundEnabled = document.getElementById("background_updates").checked;
    document.getElementById("background_updates").setAttribute("aria-expanded", String(backgroundEnabled));
    document.querySelectorAll("[data-background-settings]").forEach((element) => {
      element.hidden = !backgroundEnabled;
    });
    const weatherEnabled = document.getElementById("weather_enabled").checked;
    document.getElementById("weather_enabled").setAttribute("aria-expanded", String(weatherEnabled));
    document.querySelectorAll("[data-weather-settings]").forEach((element) => {
      element.hidden = !weatherEnabled;
    });
    const ntfyEnabled = document.getElementById("ntfy_enabled").checked;
    document.getElementById("ntfy_enabled").setAttribute("aria-expanded", String(ntfyEnabled));
    document.querySelectorAll("[data-ntfy-settings]").forEach((element) => {
      element.hidden = !ntfyEnabled;
    });
  }

  function setStep(step, focus = true) {
    const safeStep = Math.max(0, Math.min(stepSections.length - 1, Number(step) || 0));
    currentStep = safeStep;
    stepSections.forEach((section, index) => { section.hidden = index !== safeStep; });
    stepButtons.forEach((button) => {
      if (Number(button.dataset.goStep) === safeStep) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    });
    document.getElementById("step-counter").textContent = `Step ${safeStep + 1} of ${stepSections.length}`;
    backButton.textContent = safeStep === 0 ? "Setup paths" : "Back";
    nextButton.textContent = safeStep === stepSections.length - 1 ? "Review settings" : "Continue";
    updateConditionalFields();
    if (focus) {
      const heading = stepSections[safeStep].querySelector("h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({preventScroll: false});
      }
    }
  }

  function currentStepIsValid() {
    const controls = Array.from(stepSections[currentStep].querySelectorAll("input, select, textarea"))
      .filter((control) => !control.disabled && !control.closest("[hidden]"));
    const invalid = controls.find((control) => !control.checkValidity());
    if (invalid) {
      invalid.reportValidity();
      invalid.focus();
      return false;
    }
    return true;
  }

  function renderReview(review) {
    const list = document.getElementById("review-list");
    const guarantees = document.getElementById("review-guarantees");
    list.replaceChildren();
    reviewLabels.forEach((label, key) => {
      if (!(key in review)) return;
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = String(review[key]);
      list.append(term, description);
    });
    guarantees.replaceChildren();
    (Array.isArray(review.guarantees) ? review.guarantees : []).forEach((message) => {
      const item = document.createElement("li");
      item.textContent = String(message);
      guarantees.appendChild(item);
    });
  }

  async function getPreset(selectedProfile) {
    const payload = await api(`/api/preset/${encodeURIComponent(selectedProfile)}`);
    environmentKeyAvailable = payload.environment_openai_key_available === true;
    applyPreset(payload.preset);
  }

  async function reviewSettings() {
    if (requestInProgress) return;
    clearFieldErrors();
    setBusy(true, "Checking these settings locally…");
    try {
      const payload = await api("/api/validate", {method: "POST", body: {settings: collectSettings()}});
      reviewToken = payload.review_token;
      renderReview(payload.review);
      setStatus("");
      showScreen("review");
    } catch (error) {
      setStatus("");
      if (error instanceof ApiError && error.status === 422) {
        showFieldErrors(error.payload.errors || {form: error.message});
      } else {
        showScreen("wizard", {focusHeading: false});
        formError.textContent = error.message || "The settings could not be reviewed.";
        formError.hidden = false;
        formError.focus();
      }
    } finally {
      setBusy(false);
    }
  }

  async function choosePath(selectedProfile, quickReview = false) {
    if (requestInProgress) return;
    setBusy(true, "Loading the selected setup path…");
    try {
      await getPreset(selectedProfile);
      setStatus("");
      if (quickReview) {
        setBusy(false);
        await reviewSettings();
      } else {
        showScreen("wizard", {focusHeading: false});
        setStep(0);
      }
    } catch (error) {
      setStatus(error.message || "This setup path could not be loaded.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function editReviewedSettings() {
    if (requestInProgress) return;
    setBusy(true, "Returning to your settings…");
    try {
      await api("/api/edit", {method: "POST", body: {}});
      reviewToken = "";
      setStatus("");
      showScreen("wizard", {focusHeading: false});
      setStep(currentStep, true);
    } catch (error) {
      setStatus(error.message || "The settings cannot be edited now.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function createReader() {
    if (requestInProgress || !reviewToken) return;
    createButton.textContent = "Creating and verifying…";
    setBusy(true, "Creating and verifying the local reader… Keep this page open.");
    try {
      const payload = await api("/api/complete", {
        method: "POST",
        body: {review_token: reviewToken}
      });
      const result = payload.result || {};
      clearSecretInputs();
      document.getElementById("reader-address").textContent = result.reader_url || "its local address";
      const link = document.getElementById("open-reader");
      link.href = result.reader_url || "#";
      setStatus("");
      showScreen("complete");
    } catch (error) {
      if (error instanceof ApiError && error.payload.state && error.payload.state.phase === "recovery_required") {
        showRecovery(error.payload.state, error.message);
        return;
      }
      // A commit failure moves the server to FAILED. Revalidation is the explicit retry path.
      showScreen("wizard", {focusHeading: false});
      setStep(currentStep, false);
      formError.textContent = `${error.message || "Setup could not finish."} Review the settings, then review and create the reader again.`;
      formError.hidden = false;
      formError.focus();
      setStatus("No active reader was changed.", "error");
    } finally {
      setBusy(false);
      createButton.textContent = "Create this reader";
    }
  }

  async function cancelSetup() {
    if (requestInProgress) return;
    const confirmed = window.confirm("Cancel setup? No reader will be created, and you can run ./launch.sh to start again.");
    if (!confirmed) return;
    setBusy(true, "Cancelling setup…");
    try {
      await api("/api/cancel", {method: "POST", body: {}});
      clearSecretInputs();
      setStatus("");
      showScreen("cancelled");
    } catch (error) {
      setStatus(error.message || "Setup could not be cancelled in its current state.", "error");
    } finally {
      setBusy(false);
    }
  }

  function restoreState(state) {
    if (!state || !state.phase) throw new Error("The setup state is missing.");
    if (state.settings) applyPreset(state.settings);
    if (state.phase === "complete" && state.result) {
      document.getElementById("reader-address").textContent = state.result.reader_url || "its local address";
      document.getElementById("open-reader").href = state.result.reader_url || "#";
      showScreen("complete");
      return;
    }
    if (state.phase === "cancelled" || state.phase === "timed_out") {
      showScreen("cancelled");
      return;
    }
    if (state.phase === "recovery_required") {
      showRecovery(state);
      return;
    }
    if (state.phase === "failed") {
      setStatus("The previous creation attempt was rolled back. Secret fields were cleared; review the settings and enter any needed token again.", "error");
      showScreen("wizard");
      setStep(0, false);
      return;
    }
    if (state.phase === "reviewed" && state.review && state.review_token) {
      reviewToken = state.review_token;
      renderReview(state.review);
      showScreen("review");
      return;
    }
    showScreen("welcome");
  }

  async function initialize() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const capability = fragment.get("capability");
    try {
      let payload;
      if (capability) {
        payload = await api("/api/bootstrap", {
          method: "POST",
          body: {capability},
          csrf: false
        });
        csrfToken = payload.csrf_token || "";
        environmentKeyAvailable = payload.environment_openai_key_available === true;
        applyPreset(payload.preset);
        // Remove the one-time capability from the address bar only after a successful exchange.
        window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      } else {
        payload = await api("/api/state");
        csrfToken = payload.csrf_token || "";
        environmentKeyAvailable = payload.environment_openai_key_available === true;
      }
      if (defaultProfile === "demo" && payload.state.phase === "editing") {
        document.getElementById("demo-introduction").hidden = false;
      }
      restoreState(payload.state);
    } catch (error) {
      document.getElementById("fatal-error-message").textContent = error.message || "Relaunch DistillFeed to open a fresh private setup session.";
      showScreen("error");
    }
  }

  document.getElementById("use-recommended").addEventListener("click", () => choosePath("recommended", true));
  document.getElementById("use-guided").addEventListener("click", () => choosePath("guided", false));
  document.getElementById("start-demo").addEventListener("click", () => choosePath("demo", false));
  cancelButton.addEventListener("click", cancelSetup);
  document.getElementById("edit-settings").addEventListener("click", editReviewedSettings);
  createButton.addEventListener("click", createReader);

  stepButtons.forEach((button) => {
    button.addEventListener("click", () => setStep(Number(button.dataset.goStep)));
  });
  backButton.addEventListener("click", () => {
    clearFieldErrors();
    if (currentStep === 0) showScreen("welcome");
    else setStep(currentStep - 1);
  });
  nextButton.addEventListener("click", () => {
    clearFieldErrors();
    if (!currentStepIsValid()) return;
    if (currentStep < stepSections.length - 1) setStep(currentStep + 1);
    else reviewSettings();
  });

  form.addEventListener("change", (event) => {
    updateConditionalFields();
    if (event.target.name === "arxiv_enabled" && event.target.checked && selectedProvider() !== "openai") {
      setStatus("The arXiv daily digest requires OpenAI. Choose OpenAI in the AI summaries section before review.");
    } else if (event.target.name === "auto_summarize" && event.target.checked && selectedProvider() === "disabled") {
      setStatus("Automatic summaries require an AI provider. Choose one in the AI summaries section before review.");
    } else if (event.target.name === "ntfy_enabled" && event.target.checked && !document.getElementById("auto_summarize").checked) {
      setStatus("Article alerts on other devices are sent after automatic summaries. Enable automatic summaries or configure ntfy later.");
    } else {
      setStatus("");
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (requestInProgress) return;
    clearFieldErrors();
    if (!currentStepIsValid()) return;
    if (currentStep === stepSections.length - 1) reviewSettings();
    else setStep(currentStep + 1);
  });

  initialize();
})();
