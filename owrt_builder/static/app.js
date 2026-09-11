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
    clearingCache: false,
    clearingHistory: false,
    currentView: "overview",
    lastSeq: 0,
    sourcePoll: null,
    systemPoll: null,
    jobsPage: 1,
    jobsPerPage: 5,
    jobsPages: 1,
    jobsTotal: 0,
    buildDialogReturnFocus: null,
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
  const beijingTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  function formatBeijingTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间无效（北京时间）";
    return `${beijingTimeFormatter.format(date)} 北京时间`;
  }

  function formatSourceUpdateTime(source) {
    const value = source?.last_success_at || source?.snapshot?.created_at;
    if (!value) return "源: --";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "源: --";
    return `源: ${beijingTimeFormatter.format(date)}`;
  }

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
    if (!node) return;
    node.textContent = text || "";
    node.className = className;
  }

  function bindEvent(id, eventName, listener) {
    const node = $(id);
    if (node) node.addEventListener(eventName, listener);
  }

  const validViews = new Set(["overview", "jobs", "settings"]);

  function routeFromHash() {
    const value = window.location.hash.replace(/^#/, "");
    return validViews.has(value) ? value : "overview";
  }

  async function activateView(view = routeFromHash()) {
    const selected = validViews.has(view) ? view : "overview";
    state.currentView = selected;
    document.querySelectorAll("[data-view]").forEach((node) => {
      node.hidden = node.dataset.view !== selected;
    });
    document.querySelectorAll("[data-route]").forEach((link) => {
      if (link.dataset.route === selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (selected === "settings") await loadSettings();
    if (selected === "jobs") await loadJobs();
    if (selected === "overview") await loadSystem();
    syncSystemPolling();
    window.scrollTo({ top: 0, behavior: "auto" });
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
        body: { username: $("username").value, password: $("password").value },
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

  async function loadSettings() {
    const result = await api("/api/settings");
    const settings = result.settings || {};
    const username = $("settings-username");
    if (username) username.value = settings.username || "";
    const pushplus = settings.pushplus || {};
    show(
      $("pushplus-status"),
      pushplus.configured ? `已配置（${pushplus.masked || "已隐藏"}）` : "未配置",
      pushplus.configured ? "message" : "muted small",
    );
  }

  async function openSettings() {
    window.location.hash = "settings";
    await activateView("settings");
  }

  function closeSettings() {
    window.location.hash = "overview";
  }

  async function saveAccount(event) {
    event.preventDefault();
    try {
      const username = $("settings-username").value;
      const currentPassword = $("current-password").value;
      const newPassword = $("new-password").value;
      const body = { username, current_password: currentPassword };
      if (newPassword) body.new_password = newPassword;
      const result = await api("/api/settings", { method: "PUT", body });
      if (result.requires_login) {
        window.location.reload();
        return;
      }
      $("current-password").value = "";
      $("new-password").value = "";
      show($("settings-message"), "账户设置已保存。", "message");
      await loadSettings();
    } catch (error) {
      show($("settings-message"), error.message, "error");
    }
  }

  async function savePushplus(event) {
    event.preventDefault();
    try {
      const token = $("pushplus-token").value.trim();
      await api("/api/settings", { method: "PUT", body: { pushplus_token: token } });
      $("pushplus-token").value = "";
      show($("settings-message"), "PushPlus 设置已保存。", "message");
      await loadSettings();
    } catch (error) {
      show($("settings-message"), error.message, "error");
    }
  }

  async function clearPushplus() {
    try {
      await api("/api/settings", { method: "PUT", body: { clear_pushplus: true } });
      $("pushplus-token").value = "";
      show($("settings-message"), "PushPlus token 已清空。", "message");
      await loadSettings();
    } catch (error) {
      show($("settings-message"), error.message, "error");
    }
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let amount = bytes;
    let unit = "B";
    for (const candidate of units) {
      amount /= 1024;
      unit = candidate;
      if (amount < 1024 || candidate === units[units.length - 1]) break;
    }
    return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
  }

  function renderCacheCleanup(result) {
    const box = $("cache-cleanup-result");
    if (!box) return;
    box.replaceChildren();
    const labels = {
      compile_cache: "编译缓存",
      download_cache: "下载缓存",
      source_cache: "源码快照",
      artifact_dir: "固件产物",
      repo_owrt_cache: "仓库 .owrt 缓存",
      other_cache: "其他缓存",
    };
    const categories = result?.categories || result?.breakdown || {};
    Object.entries(labels).forEach(([key, label]) => {
      const row = document.createElement("div");
      row.className = "cache-cleanup-row";
      const name = document.createElement("span");
      name.textContent = label;
      const amount = document.createElement("span");
      amount.className = "muted";
      amount.textContent = formatBytes(categories[key]);
      row.append(name, amount);
      box.appendChild(row);
    });
    const total = document.createElement("div");
    total.className = "cache-cleanup-total";
    total.textContent = `总释放：${formatBytes(result?.total_bytes ?? result?.released_bytes)}`;
    box.appendChild(total);
  }

  async function clearCache() {
    if (state.clearingCache) return;
    if (!window.confirm("确定清理所有缓存吗？编译缓存、下载缓存、源码快照和固件产物都会被删除。")) return;
    state.clearingCache = true;
    const button = $("clear-cache");
    if (button) button.disabled = true;
    show($("cache-cleanup-message"), "正在清理缓存，请稍候…", "message");
    try {
      const result = await api("/api/cache/clear", { method: "POST", body: {} });
      renderCacheCleanup(result);
      const total = result?.total_bytes ?? result?.released_bytes ?? 0;
      show(
        $("cache-cleanup-message"),
        `缓存清理完成，共释放 ${formatBytes(total)}。请重新更新源。`,
        "message",
      );
      state.catalogBase = [];
      state.catalog = [];
      $("catalog-list")?.replaceChildren();
      $("artifacts")?.replaceChildren();
      await loadSource();
    } catch (error) {
      show($("cache-cleanup-message"), error.message, "error");
    } finally {
      state.clearingCache = false;
      if (button) button.disabled = false;
    }
  }

  async function clearHistory() {
    if (state.clearingHistory) return;
    if (!window.confirm("确定清理所有已结束的构建记录吗？对应日志、任务证据和固件也会删除。")) return;
    state.clearingHistory = true;
    const button = $("clear-history");
    if (button) button.disabled = true;
    show($("history-cleanup-message"), "正在清理构建记录，请稍候…", "message");
    try {
      const result = await api("/api/jobs/history/clear", { method: "POST", body: {} });
      const box = $("history-cleanup-result");
      if (box) {
        box.textContent = `已删除 ${result.jobs || 0} 个任务、${result.logs || 0} 个日志、${result.artifact_records || 0} 条产物记录，释放 ${formatBytes(result.released_bytes)}。`;
      }
      show($("history-cleanup-message"), "构建记录清理完成。", "message");
      state.activeJob = null;
      stopEvents();
      if ($("job-detail")) $("job-detail").hidden = true;
      state.jobsPage = 1;
      await loadJobs(1);
    } catch (error) {
      show($("history-cleanup-message"), error.message, "error");
    } finally {
      state.clearingHistory = false;
      if (button) button.disabled = false;
    }
  }

  function openBuildDialog() {
    const dialog = $("build-dialog");
    if (!dialog) return;
    state.buildDialogReturnFocus = document.activeElement;
    renderIssues([]);
    show($("builder-message"), "");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    // The device list and its catalog are loaded while the dialog is open so
    // opening it remains instant even when the source refresh is still
    // running.  Focus the first control for keyboard and screen-reader users.
    window.setTimeout(() => $("device-select")?.focus(), 0);
  }

  function closeBuildDialog() {
    const dialog = $("build-dialog");
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
    const returnFocus = state.buildDialogReturnFocus;
    state.buildDialogReturnFocus = null;
    if (returnFocus && typeof returnFocus.focus === "function") returnFocus.focus();
  }

  function handleBuildDialogCancel(event) {
    // Native <dialog> dispatches ``cancel`` for Escape.  Explicitly closing
    // it also covers browsers that expose dialog markup without the method.
    event?.preventDefault?.();
    closeBuildDialog();
  }

  function handleBuildDialogBackdrop(event) {
    if (event.target === $("build-dialog")) closeBuildDialog();
  }

  async function load() {
    await loadDevices();
    await Promise.all([loadJobs(), loadSource()]);
    await loadCatalog();
    await activateView();
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
      badge.textContent = formatSourceUpdateTime(result);
      badge.className = `badge ${ready ? "ready" : ""}`;
      show($("source-time"), ready ? "源码可用于构建" : (result.status === "preparing" ? "准备中 · 目录暂不可用" : "等待源码快照"), "muted small");
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
      $("source-badge").textContent = "源: --";
    }
  }

  function formatPercent(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(1)}%` : "—";
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    if (value < 60) return `${value}s`;
    const minutes = Math.floor(value / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ${minutes % 60}m`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  }

  function renderSystemStatus(result) {
    const disk = result?.disk || {};
    const memory = result?.memory || {};
    const cpu = result?.cpu || {};
    const diskUsage = formatPercent(disk.usage_percent);
    const memoryUsage = formatPercent(memory.usage_percent);
    const cpuUsage = formatPercent(cpu.usage_percent);
    show($("system-disk-value"), `${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}`);
    show($("system-disk-percent"), diskUsage, "muted small");
    show($("system-memory-value"), `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`);
    show($("system-memory-percent"), memoryUsage, "muted small");
    show($("system-cpu-value"), `${cpu.logical_cpus || 0} 核 · ${cpuUsage}`);
    show($("system-cpu-load"), `负载 ${(Number(cpu.load_1m) || 0).toFixed(2)} / ${(Number(cpu.load_5m) || 0).toFixed(2)}`, "muted small");
    show($("overview-disk"), diskUsage);
    show($("overview-disk-note"), `${formatBytes(disk.available_bytes)} 可用`, "muted small");
    show($("overview-memory"), memoryUsage);
    show($("overview-cpu"), `CPU ${cpuUsage} · ${cpu.logical_cpus || 0} 核`, "muted small");
    show($("system-collected"), result?.collected_at ? `更新于 ${formatBeijingTime(result.collected_at)}` : "每 4 秒刷新", "muted small");
  }

  function renderProcesses(result) {
    const list = $("process-list");
    if (!list) return;
    list.replaceChildren();
    const items = result?.items || [];
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "muted";
      cell.textContent = "暂无可见进程";
      row.appendChild(cell);
      list.appendChild(row);
      return;
    }
    const stateLabels = { R: "运行", S: "休眠", D: "等待", Z: "僵尸", T: "停止", I: "空闲" };
    items.forEach((item) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const name = document.createElement("span");
      name.className = "process-name";
      const title = document.createElement("strong");
      title.textContent = item.name || "未知进程";
      const pid = document.createElement("small");
      pid.textContent = `PID ${item.pid}`;
      name.append(title, pid);
      nameCell.appendChild(name);
      const state = document.createElement("td");
      state.textContent = stateLabels[item.state] || item.state || "—";
      const cpu = document.createElement("td");
      cpu.textContent = formatPercent(item.cpu_percent);
      const memory = document.createElement("td");
      memory.textContent = `${formatBytes(item.memory_bytes)} (${formatPercent(item.memory_percent)})`;
      const elapsed = document.createElement("td");
      elapsed.textContent = formatDuration(item.elapsed_seconds ?? item.runtime_seconds);
      row.append(nameCell, state, cpu, memory, elapsed);
      list.appendChild(row);
    });
  }

  async function loadSystem() {
    try {
      const sort = $("process-sort")?.value || "cpu";
      const [status, processes] = await Promise.all([
        api("/api/system/status"),
        api(`/api/system/processes?limit=20&sort=${encodeURIComponent(sort)}`),
      ]);
      renderSystemStatus(status);
      renderProcesses(processes);
    } catch (_) {
      // Keep the last good telemetry visible during a transient probe or
      // session failure.  Individual cells already use an em dash fallback.
    }
  }

  function systemPollingAllowed() {
    return state.currentView === "overview" && !$("app-panel")?.hidden && document.visibilityState === "visible";
  }

  function stopSystemPolling() {
    if (state.systemPoll === null) return;
    window.clearInterval(state.systemPoll);
    state.systemPoll = null;
  }

  function syncSystemPolling() {
    if (!systemPollingAllowed()) {
      stopSystemPolling();
      return;
    }
    if (state.systemPoll !== null) return;
    state.systemPoll = window.setInterval(() => {
      if (!systemPollingAllowed()) {
        stopSystemPolling();
        return;
      }
      loadSystem();
    }, 4000);
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

  async function submitJob() {
    try {
      const result = await api("/api/jobs", { method: "POST", body: configurationBody() });
      renderIssues(result.issues || []);
      show($("builder-message"), `任务已提交：${result.job.id}`, "message");
      closeBuildDialog();
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

  function updateOverviewJobs(result) {
    const total = Number(result?.total);
    const items = result?.items || [];
    const active = Number.isFinite(Number(result?.active_total))
      ? Number(result.active_total)
      : items.filter((job) => ["queued", "running"].includes(job.status)).length;
    show($("overview-total"), Number.isFinite(total) ? String(total) : "—");
    // The API page only contains five rows.  Keep the card useful even when
    // an active task sits on a different page by using the persisted page
    // metadata when available; a later refresh updates it again.
    show($("overview-active"), Number.isFinite(active) ? String(active) : "—");
    const counts = result?.status_counts || {};
    show(
      $("overview-total-note"),
      Number.isFinite(total) ? `成功 ${Number(counts.succeeded) || 0} · 失败 ${Number(counts.failed) || 0}` : "成功 — · 失败 —",
      "muted small",
    );
  }

  function updateJobPagination(result) {
    const pagination = $("job-pagination");
    if (!pagination) return;
    state.jobsPage = Math.max(1, Number(result?.page) || 1);
    state.jobsPages = Math.max(1, Number(result?.pages) || 1);
    state.jobsTotal = Math.max(0, Number(result?.total) || 0);
    state.jobsPerPage = Math.max(1, Number(result?.per_page) || 5);
    pagination.hidden = state.jobsTotal === 0;
    show($("jobs-page-label"), `第 ${state.jobsPage} / ${state.jobsPages} 页 · 共 ${state.jobsTotal} 条`, "muted small");
    const previous = $("jobs-prev");
    const next = $("jobs-next");
    if (previous) previous.disabled = state.jobsPage <= 1;
    if (next) next.disabled = state.jobsPage >= state.jobsPages;
  }

  async function loadJobs(requestedPage = state.jobsPage) {
    const page = Math.max(1, Number(requestedPage) || 1);
    try {
      const result = await api(`/api/jobs?page=${page}&per_page=5`);
      updateJobPagination(result);
      updateOverviewJobs(result);
      const list = $("jobs-list");
      list.replaceChildren();
      if (!result.items?.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = state.jobsTotal ? "这一页暂无任务。" : "暂无任务。";
        list.appendChild(empty);
        return;
      }
      result.items.forEach((job) => {
        const button = document.createElement("button");
        button.type = "button";
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
        meta.textContent = `${formatBeijingTime(job.created_at)} · ${job.packages?.length || 0} 个插件 · ${job.parallel_jobs || 1} 核`;
        button.append(top, meta);
        button.setAttribute("aria-label", `查看任务 ${job.device} ${job.id.slice(0, 10)}`);
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
    const summary = $("job-summary");
    if (summary) {
      summary.textContent = `${job.device || "—"} · ${job.parallel_jobs || 1} 核 · 缓存${job.reuse_cache ? "复用" : "不复用"} · ${job.packages?.length || 0} 个插件`;
    }
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
    log.textContent += `[${formatBeijingTime(row.created_at)}] ${row.line}\n`;
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
      show($("source-maintenance-status"), "源码/feeds 更新已在后台开始。", "message");
      await loadSource();
    } catch (error) {
      show($("source-maintenance-status"), error.message, "error");
    }
  }

  bindEvent("login-form", "submit", login);
  bindEvent("close-settings", "click", closeSettings);
  bindEvent("account-form", "submit", saveAccount);
  bindEvent("pushplus-form", "submit", savePushplus);
  bindEvent("clear-pushplus", "click", clearPushplus);
  bindEvent("clear-cache", "click", clearCache);
  bindEvent("clear-history", "click", clearHistory);
  bindEvent("new-build", "click", openBuildDialog);
  bindEvent("close-build", "click", closeBuildDialog);
  bindEvent("cancel-build", "click", closeBuildDialog);
  bindEvent("build-dialog", "cancel", handleBuildDialogCancel);
  bindEvent("build-dialog", "click", handleBuildDialogBackdrop);
  bindEvent("logout", "click", async () => {
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } finally { window.location.reload(); }
  });
  bindEvent("device-select", "change", async () => { applyDeviceDefaults(); await loadCatalog(); });
  bindEvent("catalog-search", "input", loadCatalog);
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.category = tab.dataset.category;
    orderCatalogSelectedFirst();
    renderCatalog();
  }));
  bindEvent("submit-job", "click", submitJob);
  bindEvent("reload-jobs", "click", loadJobs);
  bindEvent("jobs-prev", "click", () => loadJobs(state.jobsPage - 1));
  bindEvent("jobs-next", "click", () => loadJobs(state.jobsPage + 1));
  bindEvent("reload-system", "click", loadSystem);
  bindEvent("process-sort", "change", loadSystem);
  bindEvent("refresh-sources", "click", refreshSources);
  bindEvent("cancel-job", "click", cancelJob);
  bindEvent("save-default", "click", saveDefault);
  bindEvent("close-job", "click", () => { stopEvents(); $("job-detail").hidden = true; });
  window.addEventListener("hashchange", () => {
    activateView().catch((error) => show($("builder-message"), error.message, "error"));
  });
  document.addEventListener("visibilitychange", () => {
    if (!systemPollingAllowed()) {
      stopSystemPolling();
      return;
    }
    loadSystem().finally(syncSystemPolling);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("build-dialog")?.open) closeBuildDialog();
  });
  boot();
})();
