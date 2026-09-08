(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    devices: [],
    catalog: [],
    catalogBase: [],
    category: "luci-app",
    selected: new Set(),
    options: {},
    expanded: new Set(),
    activeJob: null,
    eventSource: null,
    cancelingJobs: new Set(),
    cancelPolls: new Map(),
    lastSeq: 0,
    sourcePoll: null,
    runtime: { logical_cpus: 1, max_parallel_jobs: 1, default_parallel_jobs: 1 },
  };
  const statusLabel = {
    queued: "排队中",
    running: "构建中",
    succeeded: "成功",
    failed: "失败",
    canceled: "已取消",
    interrupted: "中断",
    canceling: "取消中",
  };

  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)owrt_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function api(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const init = {
      credentials: "same-origin",
      ...options,
      headers: { ...(options.headers || {}) },
    };
    if (init.body && typeof init.body !== "string") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(init.body);
    }
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      init.headers["X-CSRF-Token"] = csrfToken();
    }
    const response = await fetch(url, init);
    let body = {};
    try {
      body = await response.json();
    } catch (_) {
      body = {};
    }
    if (!response.ok) {
      const detail = body.detail;
      const message = typeof detail === "string" ? detail : detail?.message;
      const error = new Error(message || `请求失败（HTTP ${response.status}）`);
      error.body = body;
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function show(node, text, className = "") {
    node.textContent = text || "";
    node.className = className;
  }

  function activeDevice() {
    return state.devices.find((item) => item.key === $("device-select").value);
  }

  function applyDeviceDefaults() {
    const device = activeDevice();
    const saved = device?.saved_defaults;
    const packages = saved?.packages || device?.default_plugins || [];
    const options = saved?.options || device?.default_options || {};
    state.selected = new Set(packages);
    state.options = { ...options };
    state.expanded.clear();
  }

  async function login(event) {
    event.preventDefault();
    show($("login-error"), "");
    try {
      await api("/api/auth/login", {
        method: "POST",
        body: { username: $("username").value.trim(), password: $("password").value },
      });
      $("password").value = "";
      $("login-panel").hidden = true;
      $("app-panel").hidden = false;
      await load();
    } catch (error) {
      show($("login-error"), error.message, "error");
    }
  }

  async function boot() {
    try {
      await api("/api/auth/me");
      $("login-panel").hidden = true;
      $("app-panel").hidden = false;
      await load();
    } catch (_) {
      $("login-panel").hidden = false;
      $("app-panel").hidden = true;
    }
  }

  async function load() {
    await loadDevices();
    await Promise.all([loadJobs(), loadSource()]);
    await loadCatalog();
  }

  async function loadDevices() {
    const result = await api("/api/devices");
    state.devices = result.items || [];
    state.runtime = result.runtime || state.runtime;
    configureBuildControls();
    const select = $("device-select");
    const previous = select.value;
    select.replaceChildren();
    state.devices.forEach((device) => {
      const option = document.createElement("option");
      option.value = device.key;
      option.textContent = device.description ? `${device.key} · ${device.description}` : device.key;
      select.appendChild(option);
    });
    if (state.devices.some((device) => device.key === previous)) select.value = previous;
    applyDeviceDefaults();
  }

  function configureBuildControls() {
    const input = $("parallel-jobs");
    const hint = $("parallel-jobs-hint");
    if (!input || !hint) return;
    const maximum = Math.max(1, Number(state.runtime.max_parallel_jobs || state.runtime.logical_cpus || navigator.hardwareConcurrency || 1));
    const defaultValue = Math.min(maximum, Math.max(1, Number(state.runtime.default_parallel_jobs || maximum)));
    input.min = "1";
    input.max = String(maximum);
    if (!input.value || Number(input.value) < 1 || Number(input.value) > maximum) input.value = String(defaultValue);
    hint.textContent = `范围 1–${maximum}（系统逻辑核心数上限，默认 ${defaultValue}）`;
  }

  async function loadSource() {
    try {
      const result = await api("/api/sources");
      const ready = Boolean(result.ready);
      const badge = $("source-badge");
      badge.textContent = ready
        ? result.status === "preparing"
          ? `源码更新中（沿用旧快照）· ${String(result.snapshot_id || "").slice(0, 12)}`
          : result.status === "failed"
            ? `源码更新失败（沿用旧快照）· ${String(result.snapshot_id || "").slice(0, 12)}`
            : `源码已就绪 · ${String(result.snapshot_id || "").slice(0, 12)}`
        : result.status === "failed" ? "源码更新失败" : "源码准备中";
      badge.className = `badge ${ready ? "ready" : ""}`;
      if (result.status === "preparing" && !state.sourcePoll) {
        state.sourcePoll = window.setInterval(async () => {
          try {
            const status = await api("/api/sources");
            if (status.status !== "preparing") {
              window.clearInterval(state.sourcePoll);
              state.sourcePoll = null;
              await loadSource();
              if (status.ready) {
                await loadDevices();
                await loadCatalog();
              }
            }
          } catch (_) {
            // A session expiry is handled by the next user action.
          }
        }, 5000);
      }
    } catch (_) {
      $("source-badge").textContent = "源码不可用";
    }
  }

  async function loadCatalog() {
    const query = encodeURIComponent($("catalog-search").value.trim());
    const device = encodeURIComponent($("device-select").value || "");
    $("catalog-state").textContent = "读取真实插件目录…";
    try {
      const result = await api(`/api/catalog?category=all&q=${query}&device=${device}`);
      state.catalogBase = (result.items || []).slice();
      orderCatalogSelectedFirst();
      renderCatalog();
      $("catalog-state").textContent = `${state.catalog.length} 个目录项 · ${result.source_snapshot_id ? `快照 ${result.source_snapshot_id.slice(0, 12)}` : "等待快照"}`;
    } catch (error) {
      $("catalog-state").textContent = error.message;
      $("catalog-list").replaceChildren();
    }
  }

  function catalogItemName(item) {
    return item.name || item.symbol || "";
  }

  function orderCatalogSelectedFirst() {
    const selected = [];
    const unselected = [];
    state.catalogBase.forEach((item) => {
      const bucket = state.selected.has(catalogItemName(item)) ? selected : unselected;
      bucket.push(item);
    });
    state.catalog = selected.concat(unselected);
  }

  function visibleCatalog() {
    return state.catalog.filter((item) => {
      if (state.category === "all") return true;
      const name = catalogItemName(item);
      const category = item.category || (
        name.startsWith("luci-app-") ? "luci-app" :
          name.startsWith("luci-theme-") ? "luci-theme" : "other"
      );
      return category === state.category;
    });
  }

  function renderCatalog() {
    const list = $("catalog-list");
    list.replaceChildren();
    visibleCatalog().forEach((item, index) => {
      const name = catalogItemName(item);
      const options = item.options || [];
      const selected = state.selected.has(name);
      const expanded = options.length > 0 && state.expanded.has(name);

      const row = document.createElement("div");
      row.className = "package-row" + (selected ? " selected" : "");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected;
      checkbox.setAttribute("aria-label", "选择 " + (item.title || name));
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          state.selected.add(name);
        } else {
          state.selected.delete(name);
          options.forEach((option) => delete state.options[option.symbol || option.name]);
          state.expanded.delete(name);
        }
        renderCatalog();
      });

      const body = document.createElement("div");
      body.className = "package-body";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "package-toggle";
      toggle.disabled = !options.length;
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.setAttribute("aria-label", options.length
        ? (item.title || name) + " 子选项"
        : (item.title || name));

      const titleLine = document.createElement("span");
      titleLine.className = "package-title-line";
      const title = document.createElement("span");
      title.className = "package-title";
      title.textContent = item.title || name;
      titleLine.appendChild(title);
      if (options.length) {
        const chevron = document.createElement("span");
        chevron.className = "package-chevron";
        chevron.textContent = expanded ? "⌃" : "⌄";
        titleLine.appendChild(chevron);
      }

      const symbol = document.createElement("span");
      symbol.className = "package-symbol";
      const origin = item.feed || item.repository || item.path || "core";
      symbol.textContent = `${name} · 来源 ${origin}`;
      const description = document.createElement("span");
      description.className = "package-desc";
      description.textContent = item.description || "暂无简介";
      toggle.append(titleLine, symbol, description);
      if (options.length) {
        toggle.addEventListener("click", () => {
          if (state.expanded.has(name)) state.expanded.delete(name);
          else state.expanded.add(name);
          renderCatalog();
        });
      }
      body.appendChild(toggle);

      if (expanded) {
        const optionsPanel = document.createElement("div");
        optionsPanel.className = "package-options" + (selected ? "" : " disabled");
        optionsPanel.id = "package-options-" + packageId(name) + "-" + index;
        if (selected) {
          renderPackageOptions(item, optionsPanel);
        } else {
          const hint = document.createElement("p");
          hint.className = "muted small";
          hint.textContent = "勾选插件后可编辑这些子选项。";
          optionsPanel.appendChild(hint);
        }
        body.appendChild(optionsPanel);
        toggle.setAttribute("aria-controls", optionsPanel.id);
      }
      row.append(checkbox, body);
      list.appendChild(row);
    });
  }

  function packageId(name) {
    return String(name).replace(/[^a-zA-Z0-9_-]+/g, "-");
  }

  function optionDefault(option) {
    const defaults = option.defaults || [];
    return defaults.length ? defaults[0].value : "";
  }

  function optionValue(option, key) {
    if (Object.prototype.hasOwnProperty.call(state.options, key)) return state.options[key];
    const value = optionDefault(option);
    if (option.kind === "bool" || option.kind === "boolean") return value === true || value === "y";
    return value;
  }

  function renderPackageOptions(item, list) {
    (item.options || []).forEach((option) => {
      const key = option.symbol || option.name;
      const row = document.createElement("label");
      row.className = "option-item";
      const title = document.createElement("span");
      title.textContent = option.prompt || option.title || key;
      const symbol = document.createElement("small");
      symbol.className = "option-symbol";
      symbol.textContent = key;
      const kind = String(option.kind || option.type || "string").toLowerCase();
      let input;
      if (kind === "tristate") {
        input = document.createElement("select");
        ["n", "m", "y"].forEach((value) => {
          const node = document.createElement("option");
          node.value = value;
          node.textContent = value;
          input.appendChild(node);
        });
        input.value = String(optionValue(option, key) || "n");
        input.addEventListener("change", () => { state.options[key] = input.value; });
      } else if (kind === "bool" || kind === "boolean") {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(optionValue(option, key));
        input.addEventListener("change", () => { state.options[key] = input.checked; });
      } else if (kind === "choice" || kind === "enum") {
        input = document.createElement("select");
        (option.choices || option.values || []).forEach((choice) => {
          const value = typeof choice === "object" ? choice.value : choice;
          const node = document.createElement("option");
          node.value = value;
          node.textContent = typeof choice === "object" ? (choice.label || value) : value;
          input.appendChild(node);
        });
        input.value = String(optionValue(option, key));
        input.addEventListener("change", () => { state.options[key] = input.value; });
      } else {
        input = document.createElement("input");
        input.type = kind === "int" || kind === "integer" ? "number" : "text";
        input.value = String(optionValue(option, key));
        input.addEventListener("input", () => {
          state.options[key] = kind === "int" || kind === "integer" ? Number(input.value) : input.value;
        });
      }
      row.append(title, symbol, input);
      if (option.help) {
        const help = document.createElement("small");
        help.textContent = option.help;
        row.appendChild(help);
      }
      list.appendChild(row);
    });
  }

  function renderIssues(issues) {
    const box = $("dependency-feedback");
    box.replaceChildren();
    (issues || []).forEach((issue) => {
      const node = document.createElement("div");
      node.className = `issue ${["dependency", "auto_dependency"].includes(issue.kind || issue.code) ? "info" : ""}`;
      node.textContent = issue.message || issue.details?.message || issue.code || issue.kind || "";
      box.appendChild(node);
    });
  }

  function configurationBody() {
    const rawParallelJobs = $("parallel-jobs").value.trim();
    const numericParallelJobs = Number(rawParallelJobs);
    const parallelJobs = rawParallelJobs === ""
      ? undefined
      : Number.isFinite(numericParallelJobs) ? numericParallelJobs : rawParallelJobs;
    return {
      device: $("device-select").value,
      packages: [...state.selected],
      options: state.options,
      parallel_jobs: parallelJobs,
      reuse_cache: $("reuse-cache").checked,
    };
  }

  async function validateConfiguration() {
    try {
      const result = await api("/api/configuration/validate", { method: "POST", body: configurationBody() });
      renderIssues(result.issues || []);
      show($("builder-message"), result.authoritative ? "原生 defconfig 校验完成。" : "已完成静态校验，原生 defconfig 结果不可用。", "message");
    } catch (error) {
      renderIssues(error.body?.detail?.issues || []);
      show($("builder-message"), error.message, "error");
    }
  }

  async function submitJob() {
    try {
      const result = await api("/api/jobs", { method: "POST", body: configurationBody() });
      renderIssues(result.issues || []);
      show($("builder-message"), `任务已提交：${result.job.id}`, "message");
      await loadJobs();
      await openJob(result.job.id);
    } catch (error) {
      renderIssues(error.body?.detail?.issues || []);
      show($("builder-message"), error.message, "error");
    }
  }

  function isTerminalJob(job) {
    return ["succeeded", "failed", "canceled", "interrupted"].includes(job.status);
  }

  function isCancelingJob(job) {
    if (isTerminalJob(job)) {
      state.cancelingJobs.delete(job.id);
      return false;
    }
    if (job.cancel_requested) state.cancelingJobs.add(job.id);
    return state.cancelingJobs.has(job.id);
  }

  function displayedJobStatus(job) {
    return isCancelingJob(job) ? "canceling" : job.status;
  }

  async function loadJobs() {
    try {
      const result = await api("/api/jobs?limit=80");
      const list = $("jobs-list");
      list.replaceChildren();
      if (!result.items?.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "暂无任务。";
        list.appendChild(empty);
        return;
      }
      result.items.forEach((job) => {
        const button = document.createElement("button");
        button.className = `job-card ${state.activeJob === job.id ? "selected" : ""}`;
        const top = document.createElement("span");
        top.className = "job-card-top";
        const title = document.createElement("strong");
        title.textContent = `${job.device} · ${job.id.slice(0, 10)}`;
        const badge = document.createElement("span");
        const displayStatus = displayedJobStatus(job);
        badge.className = `badge ${displayStatus}`;
        badge.textContent = statusLabel[displayStatus] || displayStatus;
        top.append(title, badge);
        const meta = document.createElement("span");
        meta.className = "job-meta";
        meta.textContent = `${job.created_at} · ${job.packages?.length || 0} 个插件`;
        button.append(top, meta);
        button.addEventListener("click", () => openJob(job.id));
        list.appendChild(button);
      });
    } catch (_) {
      // Session expiry is surfaced by the next explicit action.
    }
  }

  async function openJob(id) {
    state.activeJob = id;
    $("job-detail").hidden = false;
    stopEvents();
    stopCancelPolling(id);
    state.lastSeq = 0;
    try {
      const result = await api(`/api/jobs/${id}`);
      const job = result.job;
      updateJobHeader(job);
      $("job-log").textContent = "";
      renderArtifacts(job.artifacts || []);
      startEvents(id);
      if (isCancelingJob(job)) startCancelPolling(id);
    } catch (error) {
      show($("builder-message"), error.message, "error");
    }
  }

  function updateJobHeader(job) {
    const id = String(job.id || state.activeJob || "");
    const displayStatus = displayedJobStatus(job);
    $("job-title").textContent = `${job.device} · ${id.slice(0, 12)}`;
    $("job-status").textContent = statusLabel[displayStatus] || displayStatus;
    $("job-status").className = `badge ${displayStatus}`;
    const canceling = displayStatus === "canceling";
    $("cancel-job").hidden = canceling || !["queued", "running"].includes(job.status);
    $("cancel-job").disabled = canceling;
    $("save-default").hidden = job.status !== "succeeded";
  }

  function stopCancelPolling(id) {
    if (id !== undefined) {
      const timer = state.cancelPolls.get(id);
      if (timer !== undefined) window.clearTimeout(timer);
      state.cancelPolls.delete(id);
      return;
    }
    for (const timer of state.cancelPolls.values()) window.clearTimeout(timer);
    state.cancelPolls.clear();
  }

  function startCancelPolling(id) {
    stopCancelPolling(id);
    const poll = async () => {
      if (!state.cancelingJobs.has(id)) {
        stopCancelPolling(id);
        return;
      }
      try {
        const result = await api(`/api/jobs/${id}`);
        const job = result.job;
        if (isTerminalJob(job)) {
          state.cancelingJobs.delete(id);
          if (state.activeJob === id) {
            stopEvents();
            updateJobHeader(job);
            renderArtifacts(job.artifacts || []);
          }
          stopCancelPolling(id);
          await loadJobs();
          return;
        }
        state.cancelingJobs.add(id);
        if (state.activeJob === id) updateJobHeader(job);
        await loadJobs();
      } catch (_) {
        // Keep polling so a transient request failure cannot leave the UI
        // permanently stuck in the local "取消中" state.
      }
      if (state.cancelingJobs.has(id)) state.cancelPolls.set(id, window.setTimeout(poll, 1000));
    };
    poll();
  }

  function appendLog(row) {
    if (!row || Number(row.seq) <= state.lastSeq) return;
    state.lastSeq = Number(row.seq);
    const log = $("job-log");
    log.textContent += `[${row.created_at}] ${row.line}\n`;
    log.scrollTop = log.scrollHeight;
  }

  function startEvents(id) {
    const source = new EventSource(`/api/jobs/${id}/events?after=${encodeURIComponent(state.lastSeq)}`, { withCredentials: true });
    state.eventSource = source;
    source.addEventListener("log", (event) => {
      try { appendLog(JSON.parse(event.data)); } catch (_) { /* ignore malformed event */ }
    });
    source.addEventListener("status", async (event) => {
      await loadJobs();
      if (state.activeJob !== id) return;
      // A terminal stream sends one status event and closes.  Refresh the
      // header/artifact list in place; reopening the stream here would reset
      // the sequence and create a terminal-job reconnect loop.
      stopEvents();
      try {
        const result = await api(`/api/jobs/${id}`);
        updateJobHeader(result.job);
        renderArtifacts(result.job.artifacts || []);
        // The follow-up job response is authoritative for both the status
        // badge and the cancel button; the event only wakes the refresh.
      } catch (_) {
        // The history list has already been refreshed; a later click can
        // reopen the detail if the session expires during this refresh.
      }
    });
    source.onerror = () => {
      if (state.eventSource !== source) return;
      if (source.readyState === EventSource.CLOSED && state.activeJob === id) {
        window.setTimeout(() => {
          if (state.activeJob === id && state.eventSource === source) startEvents(id);
        }, 1500);
      }
    };
  }

  function stopEvents() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
  }

  function renderArtifacts(items) {
    const box = $("artifacts");
    box.replaceChildren();
    (items || []).forEach((item) => {
      const row = document.createElement("div");
      row.className = "artifact";
      const link = document.createElement("a");
      link.href = `/api/jobs/${state.activeJob}/artifacts/${item.id}/download`;
      link.textContent = item.name;
      link.target = "_blank";
      link.rel = "noopener";
      const size = document.createElement("span");
      size.className = "muted";
      size.textContent = item.size ? `${Math.round(item.size / 1024)} KB` : "";
      row.append(link, size);
      box.appendChild(row);
    });
  }

  async function cancelJob() {
    const id = state.activeJob;
    if (!id || state.cancelingJobs.has(id)) return;
    state.cancelingJobs.add(id);
    $("job-status").textContent = statusLabel.canceling;
    $("job-status").className = "badge canceling";
    $("cancel-job").hidden = true;
    $("cancel-job").disabled = true;
    show($("builder-message"), "取消请求已提交，正在停止构建容器…", "message");
    startCancelPolling(id);
    try {
      await api(`/api/jobs/${id}/cancel`, { method: "POST", body: {} });
      await loadJobs();
      const result = await api(`/api/jobs/${id}`);
      updateJobHeader(result.job);
      if (isTerminalJob(result.job)) {
        state.cancelingJobs.delete(id);
        stopCancelPolling(id);
        renderArtifacts(result.job.artifacts || []);
      } else {
        startCancelPolling(id);
      }
    } catch (error) {
      state.cancelingJobs.delete(id);
      stopCancelPolling(id);
      try {
        const result = await api(`/api/jobs/${id}`);
        updateJobHeader(result.job);
        if (isCancelingJob(result.job)) startCancelPolling(id);
      } catch (_) {
        // Keep the original cancellation error visible if the refresh also fails.
      }
      show($("builder-message"), error.message, "error");
    }
  }

  async function saveDefault() {
    try {
      await api(`/api/jobs/${state.activeJob}/save-default`, { method: "POST", body: {} });
      show($("builder-message"), "已保存为设备默认。", "message");
      await loadDevices();
    } catch (error) {
      show($("builder-message"), error.message, "error");
    }
  }

  async function refreshSources() {
    try {
      await api("/api/sources/refresh", { method: "POST", body: {} });
      show($("builder-message"), "源码/feeds 更新已在后台开始。", "message");
      await loadSource();
    } catch (error) {
      show($("builder-message"), error.message, "error");
    }
  }

  $("login-form").addEventListener("submit", login);
  $("logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } finally { window.location.reload(); }
  });
  $("device-select").addEventListener("change", async () => { applyDeviceDefaults(); await loadCatalog(); });
  $("catalog-search").addEventListener("input", loadCatalog);
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.category = tab.dataset.category;
    orderCatalogSelectedFirst();
    renderCatalog();
  }));
  $("validate").addEventListener("click", validateConfiguration);
  $("submit-job").addEventListener("click", submitJob);
  $("reload-jobs").addEventListener("click", loadJobs);
  $("refresh-sources").addEventListener("click", refreshSources);
  $("cancel-job").addEventListener("click", cancelJob);
  $("save-default").addEventListener("click", saveDefault);
  $("close-job").addEventListener("click", () => { stopEvents(); $("job-detail").hidden = true; });
  boot();
})();
