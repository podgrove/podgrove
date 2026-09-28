"use strict";

// All data is inserted as text. The browser can only select and read.
(() => {
  const $ = (id) => document.getElementById(id);
  const sidebarKey = "podgrove.web.sidebar-collapsed";
  let sidebarCollapsed = false;
  try { sidebarCollapsed = localStorage.getItem(sidebarKey) === "true"; } catch (_) { /* Start expanded. */ }
  function applySidebar() {
    document.querySelector(".workspace").classList.toggle("sidebar-collapsed", sidebarCollapsed);
    $("sidebar-toggle").setAttribute("aria-expanded", String(!sidebarCollapsed));
    const label = sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar";
    $("sidebar-toggle").setAttribute("aria-label", label);
    $("sidebar-toggle").title = label;
  }
  applySidebar();
  $("sidebar-toggle").addEventListener("click", () => {
    sidebarCollapsed = !sidebarCollapsed;
    applySidebar();
    try { localStorage.setItem(sidebarKey, String(sidebarCollapsed)); } catch (_) { /* Keep this document's choice. */ }
  });
  const themeKey = "podgrove.web.theme";
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  let chosenTheme = null;
  try { chosenTheme = localStorage.getItem(themeKey); } catch (_) { /* Use the system preference. */ }
  if (!["light", "dark"].includes(chosenTheme)) chosenTheme = null;
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("theme-toggle").setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
    $("theme-toggle").textContent = theme === "dark" ? "Light" : "Dark";
  }
  applyTheme(chosenTheme || (systemTheme.matches ? "dark" : "light"));
  $("theme-toggle").addEventListener("click", () => {
    chosenTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(chosenTheme);
    try { localStorage.setItem(themeKey, chosenTheme); } catch (_) { /* Keep this document's choice. */ }
  });
  systemTheme.addEventListener("change", () => { if (!chosenTheme) applyTheme(systemTheme.matches ? "dark" : "light"); });
  const tokenKey = "podgrove.web.token";
  let token = "";
  function consumeToken() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (!fragment.has("token")) return false;
    token = fragment.get("token") || "";
    try { sessionStorage.setItem(tokenKey, token); } catch (_) { /* Token still works in this document. */ }
    history.replaceState(null, "", location.pathname + location.search);
    return true;
  }
  if (!consumeToken()) {
    try { token = sessionStorage.getItem(tokenKey) || ""; } catch (_) { /* Show the printed-link guidance. */ }
  }
  const state = { environments: [], selected: null, detail: null, context: "", generation: 0, logGeneration: 0, settingsGeneration: 0, screen: "worktree", view: "services", configView: "cluster", logText: "", logLines: [], logBytes: 0, logDropped: 0, logAbort: null, live: false, liveStarted: false, fullscreen: false, loadingList: false };
  const logEncoder = new TextEncoder();
  const MAX_LOG_LINES = 2000, MAX_LOG_BYTES = 256 * 1024;
  for (const select of document.querySelectorAll("select")) {
    const wrapper = document.createElement("span"); wrapper.className = "select-wrap";
    select.replaceWith(wrapper); wrapper.append(select);
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 20 20"); icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m5 8 5 5 5-5"); icon.append(path); wrapper.append(icon);
  }

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function message(id, text) {
    $(id).textContent = text || "";
    $(id).hidden = !text;
  }
  function errorText(error) { return error instanceof Error ? error.message : String(error); }
  function friendly(value, fallback = "Unknown") { return value === undefined || value === null || value === "" ? fallback : String(value); }
  function time(value) {
    if (!value) return "";
    const parsed = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function statusClass(value) {
    const status = String(value || "").toLowerCase();
    if (["running", "ready", "healthy", "bound", "active"].includes(status)) return "good";
    if (["failed", "error", "unhealthy", "dead"].includes(status)) return "bad";
    if (["starting", "pending", "restarting", "initializing", "cleanup_pending", "disconnected", "reconnecting", "degraded"].includes(status)) return "warn";
    return "";
  }
  function badge(value) { return node("span", friendly(value), `badge ${statusClass(value)}`); }
  function busy(id, value) { $(id).disabled = value; $(id).setAttribute("aria-busy", String(value)); }
  async function api(path, signal) {
    if (!token) throw new Error("Open the browser link printed by podgrove web to connect this tab.");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) controller.abort();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(path, { headers: { "X-Podgrove-Token": token }, credentials: "omit", cache: "no-store", signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The read timed out. Check your cluster connection, then refresh.");
      if (error instanceof TypeError) throw new Error("Cannot reach Podgrove. Keep podgrove web running in your terminal, then refresh.");
      throw error;
    } finally { clearTimeout(timeout); signal?.removeEventListener("abort", abort); }
  }
  function renderNavigation() {
    const search = $("search").value.trim().toLowerCase();
    const filtered = state.environments.filter((env) => `${env.name} ${env.root} ${env.namespace} ${env.branch || ""} ${env.worktree || ""}`.toLowerCase().includes(search));
    $("worktree-count").textContent = String(state.environments.length);
    const items = filtered.map((env) => {
      const button = node("button", null, "worktree");
      button.type = "button";
      button.dataset.identity = env.identity;
      if (env.identity === state.selected && state.screen === "worktree") button.setAttribute("aria-current", "page");
      button.append(node("span", null, `state-dot ${statusClass(env.status)}`));
      button.firstChild.setAttribute("aria-hidden", "true");
      const text = node("span", null, "worktree-text");
      const title = node("span", env.name || env.identity, "worktree-name");
      title.title = env.name || env.identity;
      text.append(title, node("span", env.branch ? `${env.branch} · ${friendly(env.status)}` : `${friendly(env.status)} · ${friendly(env.namespace)}`, "worktree-description"));
      button.append(text);
      button.addEventListener("click", () => selectEnvironment(env.identity));
      return button;
    });
    if (!items.length) items.push(node("p", search ? "No matching worktrees." : "No saved worktrees.", "muted nav-note"));
    $("worktrees").replaceChildren(...items);
  }
  function resetLogs() {
    stopLogs();
    state.logText = "";
    state.logLines = []; state.logBytes = 0; state.logDropped = 0; state.liveStarted = false;
    $("log-output").textContent = "Select a service or the engine pod to read its logs.";
    logStatus("Logs refresh on request.");
    $("copy-logs").disabled = true;
    $("copy-logs").textContent = "Copy logs";
    busy("refresh-logs", false);
    $("refresh-logs").disabled = true;
    $("live-logs").disabled = true;
    $("live-logs").textContent = "Go live";
    $("log-buffer").textContent = "";
    $("jump-logs").hidden = true;
    message("log-notice", "");
    message("log-error", "");
  }
  function renderSources(env, reset) {
    const previous = reset ? "" : $("log-source").value;
    const options = [new Option("Choose a source", ""), new Option("Engine pod", "engine")];
    for (const service of env.services || []) {
      options.push(new Option(`Service · ${service.name} (all containers)`, `service:${service.name}`));
      for (const container of service.containers || []) {
        if (/^[a-f0-9]{64}$/.test(container.id)) options.push(new Option(`Container · ${service.name} / ${container.name || container.id.slice(0, 12)}`, `container:${encodeURIComponent(service.name)}:${container.id}`));
      }
    }
    $("log-source").replaceChildren(...options);
    $("log-source").value = options.some((option) => option.value === previous) ? previous : "";
    $("refresh-logs").disabled = !$("log-source").value;
    $("live-logs").disabled = !$("log-source").value;
    if (previous && !$("log-source").value) resetLogs();
  }
  function serviceStats(services) {
    const running = services.filter((service) => service.state === "running").length;
    return `${running} / ${services.length}`;
  }
  function renderServices(env) {
    const services = env.services || [];
    $("service-total").textContent = `${services.length} ${services.length === 1 ? "service" : "services"}`;
    if (env.services_truncated) $("service-total").textContent += ` shown · ${env.service_observations_omitted || "more"} container observations omitted`;
    if (!services.length) $("services").replaceChildren(node("p", "No service observations yet. Refresh once the environment is running.", "panel-empty"));
    else {
      const table = node("table", null, "service-table");
      const thead = node("thead");
      const tr = node("tr");
      for (const label of ["Service", "State", "Health", "Logs"]) {
        const th = node("th", label); th.scope = "col"; tr.append(th);
      }
      thead.append(tr);
      const tbody = node("tbody");
      for (const service of services) {
        const row = node("tr");
        const name = node("td");
        name.append(node("div", service.name, "service-name"));
        if (service.image) name.append(node("div", service.image, "cell-subtitle"));
        const status = node("td"); status.append(badge(service.state));
        const health = node("td", service.health || "No healthcheck"); health.dataset.column = "health";
        if (service.replicas > 1) health.append(node("div", `${service.replicas} containers`, "cell-subtitle"));
        const logs = node("td"); logs.dataset.column = "logs";
        const button = node("button", "View logs", "text-button");
        button.type = "button"; button.setAttribute("aria-label", `View ${service.name} logs`);
        button.addEventListener("click", () => { setView("logs"); $("log-source").value = `service:${service.name}`; readLogs(); });
        logs.append(button); row.append(name, status, health, logs); tbody.append(row);
      }
      table.append(thead, tbody);
      const wrap = node("div", null, "table-wrap"); wrap.append(table); $("services").replaceChildren(wrap);
    }
  }
  function renderEndpoints(env) {
    const ports = env?.ports || [];
    const search = $("endpoint-search").value.trim().toLowerCase();
    const matches = ports.filter((port) => `${port.service} ${port.target} 127.0.0.1:${port.local}`.toLowerCase().includes(search));
    $("endpoint-total").textContent = search ? `${matches.length} of ${ports.length} endpoints` : `${ports.length} ${ports.length === 1 ? "endpoint" : "endpoints"}`;
    if (env?.ports_truncated) $("endpoint-total").textContent += ` shown · ${env.ports_omitted || "more"} omitted`;
    $("endpoint-search").disabled = !ports.length;
    if (!matches.length) {
      $("endpoint-list").replaceChildren(node("p", ports.length ? "No endpoints match this filter." : "No local port forwards are configured for this worktree.", "panel-empty"));
      return;
    }
    const table = node("table", null, "endpoint-table");
    const head = node("thead"), labels = node("tr"), body = node("tbody");
    for (const label of ["Service", "Local address", "Container port", "Forward"]) {
      const th = node("th", label); th.scope = "col"; labels.append(th);
    }
    head.append(labels);
    for (const port of matches) {
      const row = node("tr");
      const service = node("td", port.service, "service-name");
      const address = node("td"); address.dataset.column = "address";
      // Plain text keeps navigation from making requests to the application.
      address.append(node("code", `127.0.0.1:${port.local}`));
      const target = node("td", port.target); target.dataset.column = "target";
      const forward = node("td"); forward.append(badge(port.status || "unknown"));
      row.append(service, address, target, forward); body.append(row);
    }
    table.append(head, body);
    const wrap = node("div", null, "table-wrap"); wrap.append(table);
    $("endpoint-list").replaceChildren(wrap);
  }
  function detailsList(pairs) {
    const list = node("dl", null, "detail-list");
    for (const [label, value] of pairs) {
      const row = node("div"); row.append(node("dt", label), node("dd", friendly(value, "Not observed"))); list.append(row);
    }
    return list;
  }
  function duration(seconds) {
    for (const [unit, scale] of [["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]]) {
      if (Number.isSafeInteger(seconds) && seconds > 0 && seconds % scale === 0) {
        const value = seconds / scale;
        return `${value.toLocaleString()} ${unit}${value === 1 ? "" : "s"}`;
      }
    }
    return typeof seconds === "number" && Number.isFinite(seconds) ? `${seconds.toLocaleString()} seconds` : "Not configured";
  }
  function renderConfiguration(config, savedNetwork) {
    if (config?.status !== "available" || !config.settings) {
      $("configuration").replaceChildren(node("p", config?.warning || (config?.status === "missing" ? "No Podgrove configuration file was found for this worktree." : "Configuration has not been read. Refresh to read this worktree’s file."), "panel-empty"));
      return;
    }
    const settings = config.settings, compose = settings.compose || {};
    const forwards = settings.forward;
    const list = detailsList([
      ["File", config.file], ["Format version", settings.version], ["Engine budget", settings.resources_mode === "custom" ? "Custom resources" : `Preset · ${friendly(settings.size)}`],
      ["Configured context", settings.cluster?.context || "Not configured in this file"],
      ["Configured namespace", settings.cluster?.namespace || "Not configured in this file"],
      ["Configured namespace mode", settings.cluster?.namespace_mode || "shared"],
      ["Configured storage class", settings.cluster?.storage_class || "Required for a new claim"],
      ["Configured extra blocked CIDRs", settings.network?.blocked_cidrs?.join(", ") || "None configured"],
      ["Saved extra blocked CIDRs", Array.isArray(savedNetwork?.blocked_cidrs) ? savedNetwork.blocked_cidrs.join(", ") || "None configured" : "Not recorded"],
      ["Node placement", settings.node_mode], ["Idle TTL", duration(settings.ttl_seconds)],
      ["Additional placement", Object.keys(settings.placement || {}).length ? JSON.stringify(settings.placement) : "None configured"],
      ["Reverse forwarding", settings.reverse?.length ? settings.reverse.map(rule => `host.docker.internal:${rule.remote_port} → ${rule.local_host}:${rule.local_port}`).join("; ") : "None configured"],
      ["Environment links", settings.connect?.length ? settings.connect.map(rule => `${rule.name}.podgrove:${rule.port} → ${rule.environment}/${rule.service}`).join("; ") : "None configured"],
      ...(settings.node_mode === "tainted" && settings.tainted_nodes ? [
        ["Node selector", Object.entries(settings.tainted_nodes.selector || {}).map(([key, value]) => `${key}=${value}`).join(", ")],
        ["Taint tolerated", `${settings.tainted_nodes.taint?.key}=${settings.tainted_nodes.taint?.value || ""} · ${settings.tainted_nodes.taint?.effect}`],
      ] : []),
      ["Compose files", compose.files === null ? "Automatic discovery" : (compose.files || []).join(", ")],
      ["Compose profiles", (compose.profiles || []).join(", ") || "None configured"],
      ["Project directory", compose.project_directory || "."],
      ["Sync exclusions", settings.sync?.exclude?.join(", ") || "None configured"],
      ["Port forwarding", forwards === null ? "Automatic from published Compose ports" : forwards?.length ? `${forwards.length} configured` : "Disabled by an empty forward list"],
    ]);
    $("configuration").replaceChildren(list);
    $("configuration").append(node("p", "These CIDRs add to built-in network restrictions. Saved settings are from startup; this view does not inspect policy enforcement.", "footnote"));
    if (forwards?.length) {
      const section = node("details", null, "configured-forwards");
      section.append(node("summary", "Configured port forwards"));
      const items = forwards.map((port) => {
        const item = node("div");
        item.append(node("dt", port.service), node("dd", `Container ${port.port} → ${port.local ? `local ${port.local}` : "available local port"}`));
        return item;
      });
      const rows = node("dl", null, "detail-list"); rows.append(...items);
      section.append(rows, node("p", "Assigned local addresses are listed in Endpoints.", "footnote"));
      $("configuration").append(section);
    }
  }
  function resourceTable(resources) {
    if (!resources || typeof resources !== "object") return node("p", "Allocation was not reported.", "panel-empty");
    const table = node("table"), head = node("thead"), labels = node("tr"), body = node("tbody");
    for (const title of ["Resource", "Request", "Limit"]) {
      const cell = node("th", title); cell.scope = "col"; labels.append(cell);
    }
    head.append(labels);
    for (const [key, title] of [["cpu", "CPU"], ["memory", "Memory"], ["ephemeral-storage", "Ephemeral storage"]]) {
      const row = node("tr");
      row.append(node("td", title), node("td", friendly(resources.requests?.[key], "Not set")), node("td", friendly(resources.limits?.[key], "Not set")));
      body.append(row);
    }
    table.append(head, body);
    const wrap = node("div", null, "table-wrap"); wrap.append(table); return wrap;
  }
  function renderResources(env) {
    const items = [], config = env.configuration, settings = config?.status === "available" ? config.settings : null;
    function group(title, content, note) {
      const section = node("section", null, "resource-group"); section.append(node("h3", title));
      if (note) section.append(node("p", note, "section-description"));
      section.append(content); items.push(section);
    }
    if (settings) {
      group("Current file · engine", resourceTable(settings.resources), settings.resources_mode === "custom" ? "Custom budget from podgrove.yml. This replaces the preset budget." : `Effective ${friendly(settings.size, "configured")} preset from podgrove.yml.`);
      group("Current file · storage initializer", resourceTable(settings.init_resources));
      group("Current file · persistent storage", detailsList([["Requested capacity", settings.storage?.size], ["Storage class", settings.cluster?.storage_class || "Required for a new claim"]]));
    } else {
      group("Current file", node("p", config?.warning || "Current configuration is unavailable for this worktree.", "panel-empty"));
    }
    group("Observed engine pod", resourceTable(env.engine?.pod?.resources), "The current Kubernetes allocation can include admission defaults or earlier configuration. It does not measure usage.");
    for (const container of env.engine?.init_containers || []) group(`Observed initializer · ${friendly(container.name)}`, resourceTable(container.resources));
    group("Observed persistent volume claim", env.storage ? detailsList([["Claim", env.storage.name], ["Requested capacity", env.storage.requested], ["Provisioned capacity", env.storage.capacity], ["Storage class", env.storage.storage_class]]) : node("p", "No claim allocation has been observed.", "panel-empty"));
    items.push(node("p", "An unset field means no value was reported at that level. Kubernetes may apply defaults or reject a budget under namespace policy. Changing this file does not resize a running environment.", "footnote"));
    $("resource-configuration").replaceChildren(...items);
  }
  function renderDetail(env, reset = false) {
    $("welcome").hidden = true;
    $("environment").hidden = state.screen !== "worktree";
    $("environment-name").textContent = state.screen === "configuration" ? "Configuration" : env.name || env.identity;
    $("environment-name").title = $("environment-name").textContent;
    $("environment-state").hidden = state.screen !== "worktree";
    $("environment-state").textContent = friendly(env.status);
    $("environment-state").className = `badge ${statusClass(env.status)}`;
    $("environment-root").textContent = env.root || "";
    $("environment-repository").textContent = env.repository ? `Repository · ${env.repository}` : "Repository information is unavailable.";
    $("environment-branch").textContent = friendly(env.branch, "Not available");
    $("environment-worktree").textContent = friendly(env.worktree, "Not available");
    $("environment-namespace").textContent = friendly(env.namespace);
    $("namespace-mode").textContent = friendly(env.namespace_mode, "Not recorded");
    $("environment-mode").textContent = friendly(env.node_mode, "Not recorded");
    $("context").textContent = friendly(env.context || state.context);
    $("context").title = $("context").textContent;
    $("fullscreen-worktree").textContent = env.name || env.identity;
    $("running-count").textContent = env.services ? serviceStats(env.services) + (env.services_truncated ? " shown" : "") : "—";
    const storage = env.storage;
    const engine = env.engine || {};
    const pod = engine.pod || {};
    const controller = engine.statefulset || {};
    $("storage-summary").textContent = storage ? `${friendly(storage.capacity || storage.requested, "—")} · 1 PVC` : "Not observed";
    $("engine-summary").textContent = pod.phase || "Not observed";
    const observed = time(env.health_observed_at || env.observed_at);
    $("observed").textContent = env.health_fresh ? `Live observation${observed ? ` · ${observed}` : ""}` : "Saved service snapshot";
    message("detail-warnings", (env.warnings || []).map((warning) => typeof warning === "string" ? warning : warning.message || JSON.stringify(warning)).join(" "));
    renderServices(env);
    if (reset) $("endpoint-search").value = "";
    renderEndpoints(env);
    renderConfiguration(env.configuration, env.network);
    renderResources(env);
    renderSources(env, reset);
    if (storage) $("storage").replaceChildren(detailsList([
      ["Claim", storage.name], ["Status", storage.phase], ["Requested", storage.requested], ["Provisioned", storage.capacity], ["Storage class", storage.storage_class], ["Persistent volume", storage.volume],
    ]));
    else $("storage").replaceChildren(node("p", "Storage has not been observed. Refresh to read the worktree’s claim.", "panel-empty"));
    if (engine.pod || engine.statefulset) {
      const requests = pod.resources?.requests || {};
      const limits = pod.resources?.limits || {};
      $("engine").replaceChildren(detailsList([
        ["StatefulSet", controller.name], ["Ready replicas", controller.ready_replicas === undefined ? null : `${controller.ready_replicas} / ${controller.replicas}`], ["Pod", pod.name], ["Phase", pod.phase], ["Ready", pod.ready === undefined ? null : pod.ready ? "Yes" : "No"], ["Restarts", pod.restarts], ["Node", pod.node], ...[["cpu", "CPU"], ["memory", "Memory"], ["ephemeral-storage", "Ephemeral storage"]].map(([key, label]) => [`${label} request / limit`, pod.resources ? `${friendly(requests[key], "Not set")} / ${friendly(limits[key], "Not set")}` : null]),
      ]));
    } else $("engine").replaceChildren(node("p", "Engine details have not been observed. Refresh to read the worktree’s pod.", "panel-empty"));
    const sync = env.sync_status || {};
    const syncLabels = { ready: "Ready", retrying: "Retrying local snapshot", reconnecting: "Reconnecting", disconnected: "Paused — inspect the mirror, then run podgrove up --refresh", disabled: "Disabled" };
    $("engine").append(detailsList([
      ["File sync", syncLabels[sync.state] || "Not observed"],
      ["Sync recovery attempts", sync.attempts],
      ["Next sync retry", sync.next_retry_at ? time(sync.next_retry_at) : null],
      ["Sync diagnostic", sync.error || null],
    ]));
  }
  function configurationWorktrees() {
    $("config-worktree").replaceChildren(...state.environments.map((env) => new Option(`${env.name || env.identity} · ${env.namespace}`, env.identity)));
    $("config-worktree").value = state.selected || "";
    $("config-worktree").disabled = !state.environments.length;
    if (!state.environments.length) {
      for (const id of ["configuration", "resource-configuration"]) $(id).replaceChildren(node("p", "No saved worktrees. Start one from your terminal to inspect its configuration here.", "panel-empty"));
    }
  }
  function renderSettings(data) {
    $("cluster-settings").replaceChildren(detailsList([["Observed context", data.context], ["Observed namespace", data.selected_namespace || "No namespace selected"], ["Dashboard", "Local browser · read-only"], ["Scope", "This machine’s saved worktrees and the selected namespace filter"]]));
    const choices = data.namespace_options || [];
    $("settings-namespace").replaceChildren(...choices.map((name) => new Option(name, name)));
    $("settings-namespace").value = data.selected_namespace || "";
    $("settings-namespace").disabled = !choices.length;
    const provisioning = data.provisioning;
    if (provisioning?.status === "present") $("namespace-details").replaceChildren(detailsList([
      ["Namespace", data.selected_namespace], ["Bootstrap", provisioning.name], ["Status", provisioning.status],
      ["Mode", provisioning.namespace_mode], ["Version", provisioning.version], ["Environment", provisioning.environment || "Shared namespace"],
    ]));
    else $("namespace-details").replaceChildren(node("p", provisioning?.warning || (data.selected_namespace ? `Bootstrap ${provisioning?.status || "not observed"}.` : "Set cluster.namespace in podgrove.yml to inspect its setup."), "panel-empty"));
    const objects = data.access?.objects || [];
    const items = objects.map((resource) => {
      const item = node("details", null, "access-object");
      const title = node("summary");
      title.append(node("span", `${resource.kind} · ${resource.name}`, "access-name"), badge(resource.status.replaceAll("_", " ")));
      item.append(title);
      if (resource.warning) item.append(node("p", resource.warning, "footnote"));
      if (resource.truncated) item.append(node("p", "Some fields were omitted from this bounded observation.", "footnote"));
      if (resource.terminating) item.append(node("p", "This resource is being deleted.", "footnote"));
      if (resource.optional) item.append(node("p", "Optional access for tainted node placement.", "footnote"));
      if (resource.status === "present") {
        const pairs = [["Namespace", resource.namespace || "Cluster scope"], ["UID", resource.uid]];
        if (resource.kind === "ServiceAccount") pairs.push(["Automount token", resource.automount_service_account_token === null ? "Not set on this account" : String(resource.automount_service_account_token)]);
        if (resource.role_ref) pairs.push(["Role", `${resource.role_ref.kind} · ${resource.role_ref.name}`]);
        item.append(detailsList(pairs));
        if (resource.subjects?.length) {
          item.append(node("h3", "Bound subjects", "access-subheading"));
          const subjects = node("ul", null, "access-list");
          for (const subject of resource.subjects) subjects.append(node("li", `${subject.kind} · ${subject.namespace ? subject.namespace + "/" : ""}${subject.name}`));
          item.append(subjects);
        }
        if (resource.rules) {
          item.append(node("h3", "Role rules", "access-subheading"));
          for (const rule of resource.rules) item.append(detailsList([
            ["API groups", (rule.api_groups || []).map((group) => group || "core").join(", ") || "None"],
            ["Resources", (rule.resources || []).join(", ") || "None"],
            ["Verbs", (rule.verbs || []).join(", ") || "None"],
            ["Resource names", (rule.resource_names || []).join(", ") || "Not restricted by name"],
            ...(rule.non_resource_urls?.length ? [["Non-resource URLs", rule.non_resource_urls.join(", ")]] : []),
          ]));
        }
      }
      return item;
    });
    if (!items.length) items.push(node("p", "Select a namespace to read Podgrove’s access setup.", "panel-empty"));
    if (data.access?.note) items.push(node("p", data.access.note, "footnote"));
    if (data.bootstrap?.note) items.push(node("p", data.bootstrap.note, "footnote"));
    $("access-details").replaceChildren(...items);
    message("settings-warnings", (data.warnings || []).join(" "));
  }
  async function loadSettings(namespace) {
    const generation = ++state.settingsGeneration;
    $("settings-page").setAttribute("aria-busy", "true");
    message("settings-error", "");
    message("settings-warnings", "");
    $("namespace-details").replaceChildren(node("p", "Reading namespace details…", "panel-empty"));
    $("access-details").replaceChildren(node("p", "Reading Podgrove’s access setup…", "panel-empty"));
    try {
      const query = namespace ? `?${new URLSearchParams({ namespace })}` : "";
      const result = await api(`/api/settings${query}`);
      if (generation !== state.settingsGeneration) return;
      renderSettings(result);
    } catch (error) {
      if (generation !== state.settingsGeneration) return;
      message("settings-error", errorText(error));
      $("namespace-details").replaceChildren(node("p", "Namespace details are unavailable. Refresh to try again.", "panel-empty"));
      $("access-details").replaceChildren(node("p", "Access setup could not be read.", "panel-empty"));
    } finally {
      if (generation === state.settingsGeneration) $("settings-page").setAttribute("aria-busy", "false");
    }
  }
  function openConfiguration() {
    stopLogs("Live connection paused while viewing configuration.");
    leaveFullscreen();
    state.screen = "configuration";
    $("environment-name").textContent = "Configuration"; $("environment-name").title = "Configuration";
    $("environment-state").hidden = true;
    $("environment").hidden = true; $("welcome").hidden = true; $("settings-page").hidden = false;
    $("settings-button").setAttribute("aria-pressed", "true");
    $("worktree-nav").classList.remove("expanded"); $("nav-toggle").setAttribute("aria-expanded", "false");
    renderNavigation(); configurationWorktrees();
    loadSettings($("settings-namespace").value || state.environments.find((env) => env.identity === state.selected)?.namespace);
  }
  async function selectEnvironment(identity, { preserve = false, keepSettings = false } = {}) {
    const item = state.environments.find((env) => env.identity === identity);
    if (!item) return;
    const changed = state.selected !== identity;
    if (!keepSettings) {
      state.screen = "worktree";
      $("settings-page").hidden = true; $("settings-button").setAttribute("aria-pressed", "false");
    }
    state.selected = identity;
    const generation = ++state.generation;
    if (changed) { resetLogs(); state.detail = null; setView("services"); }
    renderNavigation();
    configurationWorktrees();
    message("detail-error", "");
    if (!preserve || !state.detail || changed) renderDetail(item, changed);
    $("observed").textContent = "Reading current state…";
    $("environment").setAttribute("aria-busy", "true");
    $("worktree-nav").classList.remove("expanded");
    $("nav-toggle").setAttribute("aria-expanded", "false");
    try {
      const detail = await api(`/api/environments/${encodeURIComponent(identity)}`);
      if (generation !== state.generation) return;
      state.detail = detail;
      renderDetail(detail);
    } catch (error) {
      if (generation !== state.generation) return;
      message("detail-error", `${errorText(error)} Showing the last available snapshot.`);
      $("observed").textContent = "Snapshot · refresh unavailable";
    } finally {
      if (generation === state.generation) $("environment").setAttribute("aria-busy", "false");
    }
  }
  async function refresh() {
    if (state.loadingList) return;
    state.loadingList = true; busy("refresh", true);
    message("global-error", "");
    $("connection-state").textContent = "Reading local inventory…";
    try {
      const result = await api("/api/environments");
      state.environments = result.environments || [];
      state.context = result.context || "";
      message("inventory-warnings", (result.errors || []).map((error) => typeof error === "string" ? error : error.error || error.message || "A local record could not be read.").join(" "));
      renderNavigation();
      $("connection-state").textContent = `Inventory read at ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
      if (!state.environments.length) {
        state.selected = null; state.detail = null; state.generation++; resetLogs();
        leaveFullscreen();
        $("environment-name").textContent = state.screen === "configuration" ? "Configuration" : "Podgrove";
        $("environment-state").hidden = true;
        $("environment").hidden = true; $("welcome").hidden = state.screen !== "worktree";
        configurationWorktrees();
        $("welcome-message").textContent = "Start a worktree from its directory, then refresh here to see services, storage, and logs. Only environments saved on this machine are listed.";
        $("start-hint").hidden = false;
      } else {
        const selected = state.environments.some((env) => env.identity === state.selected) ? state.selected : state.environments[0].identity;
        await selectEnvironment(selected, { preserve: true, keepSettings: state.screen === "configuration" });
      }
      if (state.screen === "configuration") await loadSettings($("settings-namespace").value);
    } catch (error) {
      stopLogs("Live connection stopped. Check the dashboard connection before resuming.");
      message("global-error", errorText(error));
      $("connection-state").textContent = "Connection unavailable";
      if (!state.environments.length) $("welcome-message").textContent = "The local inventory could not be read. Check the message above, then refresh.";
    } finally { state.loadingList = false; busy("refresh", false); }
  }
  function setView(view) {
    if (view !== "logs") { stopLogs("Live connection paused while viewing another tab."); leaveFullscreen(); }
    state.view = view;
    for (const name of ["services", "endpoints", "storage", "logs", "engine"]) {
      const selected = name === view;
      $(`tab-${name}`).setAttribute("aria-selected", String(selected));
      $(`tab-${name}`).tabIndex = selected ? 0 : -1;
      $(`panel-${name}`).hidden = !selected;
    }
    revealTab($(`tab-${view}`));
  }
  function revealTab(tab) {
    const strip = tab.parentElement;
    const left = tab.offsetLeft - strip.offsetLeft, right = left + tab.offsetWidth;
    if (left < strip.scrollLeft) strip.scrollLeft = left;
    else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
  }
  function setConfigView(view) {
    state.configView = view;
    for (const name of ["cluster", "access", "worktree", "resources"]) {
      const selected = name === view;
      $(`config-tab-${name}`).setAttribute("aria-selected", String(selected));
      $(`config-tab-${name}`).tabIndex = selected ? 0 : -1;
      $(`config-panel-${name}`).hidden = !selected;
    }
    revealTab($(`config-tab-${view}`));
  }
  function logStatus(text, live = false) {
    $("log-status").textContent = text;
    $("log-status").classList.toggle("live", live);
  }
  function updateLiveButton() {
    $("live-logs").textContent = state.live ? "Pause live" : state.liveStarted ? "Resume live" : "Go live";
    $("live-logs").setAttribute("aria-pressed", String(state.live));
    $("live-logs").classList.toggle("live", state.live);
    $("live-logs").disabled = !$("log-source").value;
  }
  function stopLogs(reason) {
    const wasLive = state.live;
    state.logGeneration++;
    state.logAbort?.abort(); state.logAbort = null; state.live = false;
    updateLiveButton();
    busy("refresh-logs", false); $("refresh-logs").disabled = !$("log-source").value;
    if (wasLive && reason) logStatus(reason);
  }
  function clearLogBuffer() { state.logLines = []; state.logBytes = 0; state.logDropped = 0; state.logText = ""; }
  function appendLogText(text) {
    for (let line of String(text).match(/[^\n]*\n|[^\n]+$/g) || []) {
      let bytes = logEncoder.encode(line);
      if (bytes.length > MAX_LOG_BYTES) {
        line = new TextDecoder().decode(bytes.slice(0, MAX_LOG_BYTES - 4));
        bytes = logEncoder.encode(line); state.logDropped++;
      }
      state.logLines.push({ text: line, bytes: bytes.length }); state.logBytes += bytes.length;
      while (state.logLines.length > MAX_LOG_LINES || state.logBytes > MAX_LOG_BYTES) {
        state.logBytes -= state.logLines.shift().bytes; state.logDropped++;
      }
    }
  }
  function nearLatest() { const output = $("log-output"); return output.scrollHeight - output.scrollTop - output.clientHeight < 24; }
  function renderLogBuffer(empty = "No log lines were returned for this source.") {
    state.logText = state.logLines.map((line) => line.text).join("");
    $("log-output").textContent = state.logText || empty;
    $("copy-logs").disabled = !state.logText;
    $("log-buffer").textContent = state.logLines.length ? `${state.logLines.length.toLocaleString()} lines${state.logDropped ? " · buffer trimmed" : ""}` : "";
    if ($("autoscroll-logs").checked) $("log-output").scrollTop = $("log-output").scrollHeight;
    $("jump-logs").hidden = !state.logText || nearLatest();
  }
  function logParameters() {
    const source = $("log-source").value;
    const parameters = new URLSearchParams({ source: source === "engine" ? "engine" : "service", tail: $("log-tail").value });
    if (source.startsWith("container:")) {
      const [service, container] = source.slice("container:".length).split(":");
      parameters.set("service", decodeURIComponent(service)); parameters.set("container", container);
    } else if (source !== "engine") parameters.set("service", source.slice("service:".length));
    return parameters;
  }
  async function readLogs() {
    stopLogs(); state.liveStarted = false; updateLiveButton();
    const generation = state.logGeneration, source = $("log-source").value;
    clearLogBuffer(); $("copy-logs").disabled = true; $("log-buffer").textContent = "";
    $("jump-logs").hidden = true; message("log-error", ""); message("log-notice", "");
    if (!state.selected || !source) { resetLogs(); return; }
    const controller = new AbortController(); state.logAbort = controller;
    busy("refresh-logs", true);
    $("log-output").textContent = "Reading logs…";
    logStatus($("log-tail").value === "all" ? "Reading retained history within the snapshot limit…" : "Reading a bounded log snapshot…");
    try {
      const result = await api(`/api/environments/${encodeURIComponent(state.selected)}/logs?${logParameters()}`, controller.signal);
      if (generation !== state.logGeneration) return;
      appendLogText(result.text || ""); renderLogBuffer();
      logStatus(result.truncated ? "Output was truncated to the server’s size or source limit. This snapshot is incomplete." : `Read at ${new Date().toLocaleTimeString()} · refresh on request`);
    } catch (error) {
      if (generation !== state.logGeneration) return;
      message("log-error", errorText(error));
      $("log-output").textContent = "Logs are unavailable. Check the source and refresh to try again.";
      logStatus("Log read failed.");
    } finally {
      if (generation === state.logGeneration) { state.logAbort = null; busy("refresh-logs", false); }
    }
  }
  async function followLogs() {
    if (state.live) { stopLogs("Live connection paused. Resume live to open a new connection."); return; }
    stopLogs();
    if (!state.selected || !$("log-source").value) return;
    const generation = state.logGeneration, controller = new AbortController();
    state.logAbort = controller; state.live = true; state.liveStarted = true; updateLiveButton();
    message("log-error", ""); message("log-notice", ""); logStatus("Connecting to live logs…");
    let reader, timer, started = false, ended = false, pending = "";
    const deadline = (milliseconds) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (generation === state.logGeneration) stopLogs("Live connection stopped responding. Resume live to try a new connection.");
      }, milliseconds);
    };
    function consume(line) {
      if (!line.trim()) return;
      const event = JSON.parse(line);
      if (!event || typeof event !== "object") throw new Error("The live log response was not valid.");
      if (event.type === "start" && !started) {
        started = true; clearLogBuffer(); renderLogBuffer("Connected. Waiting for log lines…");
        logStatus($("log-tail").value === "all" ? "Live · receiving retained history and new lines" : "Live · receiving new lines", true); return;
      }
      if (!started) throw new Error("The live log response did not include a valid start event.");
      if (event.type === "line" && typeof event.text === "string") {
        const prefix = event.container ? `[${String(event.container).slice(0, 12)} · ${event.stream === "stderr" ? "stderr" : "stdout"}] ` : event.stream === "stderr" ? "[stderr] " : "";
        appendLogText(prefix + event.text);
      } else if (event.type === "notice") {
        message("log-notice", typeof event.text === "string" ? event.text : "Some log output could not be included.");
      } else if (event.type === "end") {
        ended = true;
        const reasons = {
          completed: "The log source closed. Resume live to connect again.",
          lifetime_limit: "The five-minute live window ended. Resume live to open a new connection.",
          ownership_changed: "The pod or container changed. Refresh its details before resuming live logs.",
          source_unavailable: "The log source became unavailable. Check the environment before resuming.",
          server_shutdown: "Podgrove web stopped. Start it again before resuming live logs.",
          client_cancelled: "Live connection closed.",
        };
        logStatus(reasons[event.reason] || "Live connection ended. Resume live to try a new connection.");
        if (event.partial_lines_discarded) message("log-notice", "An incomplete line was omitted when the connection ended.");
      } else if (event.type !== "heartbeat") {
        throw new Error("The live log response included an unsupported event.");
      }
    }
    try {
      if (!token) throw new Error("Open the browser link printed by podgrove web to connect this tab.");
      deadline(45000);
      const response = await fetch(`/api/environments/${encodeURIComponent(state.selected)}/logs/stream?${logParameters()}`, {
        headers: { "X-Podgrove-Token": token }, credentials: "omit", cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) {
        let data = {}; try { data = await response.json(); } catch (_) { /* Retain the HTTP status. */ }
        throw new Error(data.error || `Live logs are unavailable (${response.status}).`);
      }
      if (!response.body || !response.headers.get("content-type")?.includes("application/x-ndjson")) throw new Error("The server did not return a live log stream.");
      reader = response.body.getReader(); const decoder = new TextDecoder();
      while (!ended) {
        const { done, value } = await reader.read();
        if (generation !== state.logGeneration) return;
        if (done) {
          pending += decoder.decode();
          if (pending.trim()) consume(pending);
          break;
        }
        deadline(20000); pending += decoder.decode(value, { stream: true });
        let position;
        while (!ended && (position = pending.indexOf("\n")) !== -1) {
          if (position > 512 * 1024) throw new Error("The live log response exceeded the browser’s read limit.");
          const line = pending.slice(0, position); pending = pending.slice(position + 1); consume(line);
        }
        if (pending.length > 512 * 1024) throw new Error("The live log response exceeded the browser’s read limit.");
        renderLogBuffer("Connected. Waiting for log lines…");
      }
      if (generation === state.logGeneration && !ended) logStatus("Live connection was interrupted. Resume live to open a new connection.");
    } catch (error) {
      if (generation !== state.logGeneration) return;
      message("log-error", error instanceof TypeError ? "Cannot reach live logs. Keep podgrove web running, then resume to try again." : errorText(error));
      logStatus("Live logs stopped. Existing lines are preserved.");
    } finally {
      clearTimeout(timer);
      controller.abort();
      if (reader) { try { await reader.cancel(); } catch (_) { /* The connection may already be closed. */ } }
      if (generation === state.logGeneration) { state.logAbort = null; state.live = false; updateLiveButton(); }
    }
  }
  let fullscreenFocus = null, fullscreenInert = [];
  function leaveFullscreen() {
    if (!state.fullscreen) return;
    state.fullscreen = false;
    const panel = $("panel-logs"); panel.classList.remove("fullscreen"); panel.setAttribute("role", "tabpanel"); panel.removeAttribute("aria-modal");
    document.body.classList.remove("logs-fullscreen");
    for (const [element, previous] of fullscreenInert) element.inert = previous;
    fullscreenInert = [];
    $("fullscreen-logs").setAttribute("aria-label", "Fullscreen logs"); $("fullscreen-logs").title = "Fullscreen logs";
    $("fullscreen-logs").querySelector("span").textContent = "Fullscreen";
    if (document.fullscreenElement === panel) document.exitFullscreen().catch(() => {});
    if (fullscreenFocus?.isConnected) fullscreenFocus.focus({ preventScroll: true });
  }
  async function toggleFullscreen() {
    if (state.fullscreen) { leaveFullscreen(); return; }
    fullscreenFocus = document.activeElement; state.fullscreen = true;
    const panel = $("panel-logs"); panel.classList.add("fullscreen"); panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
    document.body.classList.add("logs-fullscreen");
    for (let current = panel; current.parentElement && current !== document.body; current = current.parentElement) {
      for (const sibling of current.parentElement.children) if (sibling !== current) {
        fullscreenInert.push([sibling, sibling.inert]); sibling.inert = true;
      }
    }
    $("fullscreen-logs").setAttribute("aria-label", "Exit fullscreen logs"); $("fullscreen-logs").title = "Exit fullscreen logs";
    $("fullscreen-logs").querySelector("span").textContent = "Exit fullscreen";
    $("fullscreen-logs").focus({ preventScroll: true });
    if (panel.requestFullscreen) {
      try {
        await panel.requestFullscreen();
        if (!state.fullscreen && document.fullscreenElement === panel) await document.exitFullscreen();
      } catch (_) { /* The viewport overlay remains usable if still requested. */ }
    }
  }
  document.addEventListener("fullscreenchange", () => {
    if (state.fullscreen && !document.fullscreenElement) leaveFullscreen();
    else if (!state.fullscreen && document.fullscreenElement === $("panel-logs")) document.exitFullscreen().catch(() => {});
  });
  document.addEventListener("keydown", (event) => {
    if (!state.fullscreen) return;
    if (event.key === "Escape") { event.preventDefault(); leaveFullscreen(); return; }
    if (event.key !== "Tab") return;
    const controls = [...$("panel-logs").querySelectorAll('button:not(:disabled), select:not(:disabled), input:not(:disabled), [tabindex="0"]')].filter((element) => element.getClientRects().length && !element.hidden);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  $("refresh").addEventListener("click", refresh);
  $("search").addEventListener("input", renderNavigation);
  $("settings-button").addEventListener("click", openConfiguration);
  $("settings-namespace").addEventListener("change", () => loadSettings($("settings-namespace").value));
  $("config-worktree").addEventListener("change", () => {
    const identity = $("config-worktree").value;
    selectEnvironment(identity, { keepSettings: true });
    loadSettings(state.environments.find((env) => env.identity === identity)?.namespace);
  });
  $("endpoint-search").addEventListener("input", () => renderEndpoints(state.detail || state.environments.find((env) => env.identity === state.selected)));
  $("nav-toggle").addEventListener("click", () => { const expanded = $("worktree-nav").classList.toggle("expanded"); $("nav-toggle").setAttribute("aria-expanded", String(expanded)); });
  const views = ["services", "endpoints", "storage", "logs", "engine"];
  for (const [index, view] of views.entries()) {
    const tab = $(`tab-${view}`);
    tab.addEventListener("click", () => setView(view));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % views.length;
      if (event.key === "ArrowLeft") next = (index + views.length - 1) % views.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = views.length - 1;
      if (next !== undefined) { event.preventDefault(); setView(views[next]); $(`tab-${views[next]}`).focus({ preventScroll: true }); }
    });
  }
  const configViews = ["cluster", "access", "worktree", "resources"];
  for (const [index, view] of configViews.entries()) {
    const tab = $(`config-tab-${view}`);
    tab.addEventListener("click", () => setConfigView(view));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % configViews.length;
      if (event.key === "ArrowLeft") next = (index + configViews.length - 1) % configViews.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = configViews.length - 1;
      if (next !== undefined) { event.preventDefault(); setConfigView(configViews[next]); $(`config-tab-${configViews[next]}`).focus({ preventScroll: true }); }
    });
  }
  $("log-source").addEventListener("change", readLogs);
  $("log-tail").addEventListener("change", readLogs);
  $("refresh-logs").addEventListener("click", readLogs);
  $("live-logs").addEventListener("click", followLogs);
  $("fullscreen-logs").addEventListener("click", toggleFullscreen);
  $("jump-logs").addEventListener("click", () => {
    $("autoscroll-logs").checked = true; $("log-output").scrollTop = $("log-output").scrollHeight; $("jump-logs").hidden = true;
  });
  $("autoscroll-logs").addEventListener("change", () => {
    if ($("autoscroll-logs").checked) $("log-output").scrollTop = $("log-output").scrollHeight;
    $("jump-logs").hidden = !state.logText || nearLatest();
  });
  $("log-output").addEventListener("scroll", () => {
    if (!nearLatest()) $("autoscroll-logs").checked = false;
    $("jump-logs").hidden = !state.logText || nearLatest();
  });
  $("wrap-logs").addEventListener("change", () => $("log-output").classList.toggle("wrap", $("wrap-logs").checked));
  $("copy-logs").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.logText); $("copy-logs").textContent = "Copied"; setTimeout(() => { $("copy-logs").textContent = "Copy logs"; }, 2000); }
    catch (_) { message("log-error", "Clipboard access is unavailable. Select the log text to copy it."); }
  });
  window.addEventListener("pagehide", () => stopLogs());
  window.addEventListener("hashchange", () => { if (consumeToken()) { stopLogs("Connection credentials changed. Resume live to reconnect."); refresh(); } });
  refresh();
})();
