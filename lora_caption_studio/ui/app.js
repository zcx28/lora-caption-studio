const isDirectFile = window.location.protocol === "file:";
document.body.classList.remove("booting");
document.body.classList.add(isDirectFile ? "file-mode" : "app-ready");

const state = {
  items: [],
  byId: new Map(),
  filtered: [],
  selectedId: null,
  filter: "all",
  view: "list",
  search: "",
  queueStatus: "idle",
  datasetPath: "",
  revision: null,
  dirty: false,
  polling: false,
  checked: new Set(),
  trigger: "my_style",
  providerReady: false,
  provider: null,
  settings: null,
  counts: { ungenerated: 0, generating: 0, pending: 0, confirmed: 0, failed: 0 },
};

const $ = (selector) => document.querySelector(selector);
let toastTimer;

function toast(message, duration = 3400) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), duration);
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function statusText(status) {
  return {
    ungenerated: "未生成",
    generating: "生成中",
    pending: "待复核",
    confirmed: "已确认",
    failed: "失败",
  }[status] || status;
}

function queueText(status) {
  return {
    idle: "空闲",
    running: "正在生成",
    pausing: "正在暂停",
    paused: "已暂停",
    complete: "已完成",
    complete_with_errors: "完成但有失败",
  }[status] || status;
}

function folderName(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || path;
}

function countEnglishWords(value) {
  const caption = String(value).trim();
  const prefix = `${state.trigger}.`;
  const body = caption.startsWith(prefix) ? caption.slice(prefix.length).trim() : caption;
  return (body.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g) || []).length;
}

function localWarnings(caption) {
  if (!caption.trim()) return [];
  const count = countEnglishWords(caption);
  const warnings = [];
  if (count < 60) warnings.push(`少于 60 词（当前 ${count}）`);
  else if (count > 200) warnings.push(`超过 200 词（当前 ${count}）`);
  if (!caption.trim().startsWith(`${state.trigger}.`)) warnings.push(`必须以 ${state.trigger}. 开头`);
  return warnings;
}

async function api(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function itemMatches(item) {
  if (state.filter === "ungenerated") {
    const needsCaption = ["ungenerated", "generating"].includes(item.status) || state.checked.has(item.id);
    if (!needsCaption) return false;
  } else if (state.filter !== "all" && item.status !== state.filter) {
    return false;
  }
  if (state.search && !`${item.current_file} ${item.original_file}`.toLocaleLowerCase().includes(state.search)) return false;
  return true;
}

function cardHtml(item) {
  const snippet = item.status === "generating"
    ? "正在生成英文 Caption…"
    : item.caption || item.error || "等待生成 Caption";
  return `<article class="image-card ${item.id === state.selectedId ? "selected" : ""} ${state.checked.has(item.id) ? "checked" : ""}" data-id="${item.id}" role="button" tabindex="0" aria-label="查看 ${escapeHtml(item.current_file)}">
    <label class="card-check" title="选择用于本次打标">
      <input type="checkbox" data-check-id="${item.id}" ${state.checked.has(item.id) ? "checked" : ""} aria-label="选择 ${escapeHtml(item.current_file)}" />
      <span aria-hidden="true"></span>
    </label>
    <span class="thumb"><img src="/thumb/${item.id}.jpg" alt="${escapeHtml(item.current_file)}" loading="lazy" decoding="async" /></span>
    <span class="card-copy">
      <strong>${escapeHtml(item.current_file)}</strong>
      <small>#${String(item.index).padStart(4, "0")} · ${statusText(item.status)}${item.word_count ? ` · ${item.word_count} 词` : ""}</small>
      <p>${escapeHtml(snippet)}</p>
    </span>
    <span class="status-dot ${item.status}" title="${statusText(item.status)}"></span>
  </article>`;
}

function selectedNeedCount() {
  return state.items.filter((item) => ["ungenerated", "generating"].includes(item.status) || state.checked.has(item.id)).length;
}

function renderSelectionControls() {
  const visibleIds = state.filtered.map((item) => item.id);
  const visibleSelected = visibleIds.filter((id) => state.checked.has(id)).length;
  const selectAll = $("#selectAll");
  selectAll.checked = visibleIds.length > 0 && visibleSelected === visibleIds.length;
  selectAll.indeterminate = visibleSelected > 0 && visibleSelected < visibleIds.length;
  selectAll.disabled = visibleIds.length === 0;
  $("#selectedCount").textContent = `已选 ${state.checked.size}`;
  $("#countUngenerated").textContent = selectedNeedCount();

  const running = state.queueStatus === "running";
  $("#startButton").disabled = running || state.checked.size === 0 || !state.providerReady;
  $("#startButton").textContent = state.checked.size ? `生成已选 ${state.checked.size} 张` : "生成已选图片";
}

function renderContextPanels() {
  const hasDataset = Boolean(state.datasetPath);
  $("#guidancePanel").classList.toggle("hidden", state.filter !== "ungenerated" || !hasDataset);
  $("#failureTools").classList.toggle("hidden", state.filter !== "failed" || state.counts.failed === 0);
}

function renderList() {
  state.filtered = state.items.filter(itemMatches);
  if (!state.selectedId || !state.filtered.some((item) => item.id === state.selectedId)) {
    state.selectedId = state.filtered[0]?.id || null;
    state.dirty = false;
  }
  $("#imageList").innerHTML = state.filtered.map(cardHtml).join("");
  $("#visibleCount").textContent = `${state.filtered.length} 张`;
  $("#empty").textContent = state.datasetPath ? "当前条件下没有图片" : "先选择一个包含训练图片的文件夹";
  $("#empty").classList.toggle("hidden", state.filtered.length > 0);
  renderSelectionControls();
  renderContextPanels();
  renderEditor();
  if (state.view === "list") requestAnimationFrame(scrollSelectedIntoView);
}

function scrollSelectedIntoView() {
  const card = document.querySelector(`.image-card[data-id="${state.selectedId}"]`);
  if (card) card.scrollIntoView({ block: "nearest" });
}

function activateFilterButton() {
  document.querySelectorAll("#statusTabs button[data-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === state.filter);
  });
}

function renderProgress(data) {
  const counts = data.counts;
  state.counts = counts;
  const total = state.items.length;
  const completed = counts.pending + counts.confirmed;
  const processed = completed + counts.failed;
  $("#countAll").textContent = total;
  $("#countPending").textContent = counts.pending;
  $("#countConfirmed").textContent = counts.confirmed;
  $("#countFailed").textContent = counts.failed;
  $("#failedTab").classList.toggle("hidden", counts.failed === 0);

  if (state.filter === "failed" && counts.failed === 0) {
    state.filter = "ungenerated";
    activateFilterButton();
  }

  $("#progressTitle").textContent = `${processed} / ${total}`;
  if (!data.selected_dir) {
    $("#progressNote").textContent = "选择文件夹后开始";
  } else {
    const notes = [`待复核 ${counts.pending}`, `已确认 ${counts.confirmed}`];
    if (counts.generating) notes.unshift(`生成中 ${counts.generating}`);
    if (counts.failed) notes.push(`失败 ${counts.failed}`);
    $("#progressNote").textContent = notes.join(" · ");
  }
  $("#progressBar").style.width = total ? `${(processed / total) * 100}%` : "0%";
  $("#queueState").textContent = queueText(data.queue_status);
  $("#lastLog").textContent = data.last_log || "";
  const providerName = data.provider?.name || "AI Provider";
  $("#modelName").textContent = `${providerName} · ${data.model} · ${data.batch_size} 张/批`;
  $("#providerStatusButton").classList.toggle("provider-unready", !data.provider?.ready);

  const running = data.queue_status === "running";
  const pausing = data.queue_status === "pausing";
  const paused = data.queue_status === "paused";
  $("#pauseButton").disabled = pausing || (!running && !paused);
  $("#pauseButton").textContent = pausing ? "正在暂停…" : paused ? "继续生成" : "立即暂停";
  $("#retryButton").disabled = counts.failed === 0 || running;
  $("#blockingError").classList.toggle("hidden", !data.dataset_error);
  $("#blockingError").textContent = data.dataset_error || "";
}

function renderEditor() {
  const item = state.byId.get(state.selectedId);
  $("#emptyEditor").classList.toggle("hidden", Boolean(item));
  $("#editorContent").classList.toggle("hidden", !item);
  $("#emptyEditorTitle").textContent = state.datasetPath ? "选择一张图片开始复核" : "先选择图片文件夹";
  if (!item) return;

  $("#mainImage").src = `/original/${item.id}`;
  $("#previewImage").src = `/original/${item.id}`;
  $("#imageNumber").textContent = `${item.index} / ${state.items.length}`;
  $("#currentFilename").textContent = item.current_file;
  $("#statusBadge").textContent = statusText(item.status);
  $("#statusBadge").className = `badge ${item.status}`;
  if (!state.dirty) $("#captionInput").value = item.caption || "";
  $("#translation").textContent = item.translation || "暂未生成中文翻译。";
  $("#errorBox").textContent = item.error || "";
  $("#errorBox").classList.toggle("hidden", !item.error);
  $("#regenerateButton").disabled = item.status === "generating";
  $("#regenerateGuidance").disabled = item.status === "generating";
  $("#confirmButton").disabled = item.status === "generating";
  updateCaptionStats();
}

function updateCaptionStats() {
  const value = $("#captionInput").value;
  const count = countEnglishWords(value);
  $("#wordCount").textContent = `${count} 词`;
  const warnings = localWarnings(value);
  $("#warnings").textContent = warnings.join(" · ");
  $("#warnings").classList.toggle("hidden", warnings.length === 0);
}

function applyData(data, { preserveSelection = true } = {}) {
  const datasetChanged = data.selected_dir !== state.datasetPath;
  const previousId = preserveSelection && !datasetChanged ? state.selectedId : null;
  if (datasetChanged) {
    state.checked.clear();
    state.dirty = false;
    $("#guidanceInput").value = "";
    $("#regenerateGuidance").value = "";
  }
  state.datasetPath = data.selected_dir;
  state.revision = data.revision;
  state.items = data.items;
  state.byId = new Map(data.items.map((item) => [item.id, item]));
  state.checked = new Set([...state.checked].filter((id) => state.byId.has(id)));
  state.selectedId = previousId && state.byId.has(previousId) ? previousId : null;
  state.queueStatus = data.queue_status;
  state.trigger = data.trigger || "my_style";
  state.provider = data.provider || null;
  state.providerReady = Boolean(data.provider?.ready);
  state.settings = data.settings || state.settings;
  document.body.classList.toggle("has-no-dataset", !data.selected_dir);
  $("#sourcePath").textContent = data.selected_dir ? folderName(data.selected_dir) : "尚未选择图片文件夹";
  $("#sourcePath").title = data.selected_dir || "";
  if (state.settings && !$("#settingsDialog").open) populateSettings();
  renderProgress(data);
  renderList();
}

async function refresh({ preserveSelection = true } = {}) {
  if (state.polling) return;
  state.polling = true;
  try {
    const response = await fetch("/api/data", { cache: "no-store" });
    if (!response.ok) throw new Error("读取状态失败");
    const data = await response.json();
    if (
      data.revision === state.revision
      && data.selected_dir === state.datasetPath
      && Boolean(data.provider?.ready) === state.providerReady
      && data.provider?.model === state.provider?.model
    ) return;
    applyData(data, { preserveSelection });
  } catch (error) {
    toast(error.message, 5000);
  } finally {
    state.polling = false;
  }
}

function selectItem(itemId) {
  if (!state.byId.has(itemId)) return;
  state.selectedId = itemId;
  state.dirty = false;
  document.querySelectorAll(".image-card").forEach((card) => card.classList.toggle("selected", card.dataset.id === itemId));
  renderEditor();
  if (state.view === "list") scrollSelectedIntoView();
}

function moveSelection(offset) {
  if (!state.filtered.length) return;
  let index = state.filtered.findIndex((item) => item.id === state.selectedId);
  index = index < 0 ? 0 : Math.max(0, Math.min(state.filtered.length - 1, index + offset));
  selectItem(state.filtered[index].id);
}

async function confirmCurrent() {
  const item = state.byId.get(state.selectedId);
  if (!item) return;
  const button = $("#confirmButton");
  button.disabled = true;
  try {
    const result = await api("/api/caption/confirm", {
      id: item.id,
      caption: $("#captionInput").value,
    });
    state.dirty = false;
    state.byId.set(item.id, result);
    state.items = state.items.map((entry) => entry.id === item.id ? result : entry);
    toast("已确认并写入同名 .txt");
    renderList();
  } catch (error) {
    toast(error.message, 5500);
  } finally {
    button.disabled = state.byId.get(state.selectedId)?.status === "generating";
  }
}

async function startSelected() {
  if (!state.checked.size) {
    toast("请先在“全部”中勾选要打标的图片；也可以全选当前结果");
    return;
  }
  const button = $("#startButton");
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const result = await api("/api/start", {
      ids: [...state.checked],
      guidance: $("#guidanceInput").value,
    });
    state.checked.clear();
    setFilter("ungenerated");
    toast(`已开始处理 ${result.queued} 张图片`, 5000);
    await refresh();
  } catch (error) {
    toast(`无法开始：${error.message}`, 7000);
  } finally {
    renderSelectionControls();
  }
}

function setFilter(filter) {
  state.filter = filter;
  state.selectedId = null;
  state.dirty = false;
  activateFilterButton();
  renderList();
}

function setView(view) {
  state.view = view;
  $("#workspace").classList.toggle("gallery-view", view === "gallery");
  $("#listViewButton").classList.toggle("active", view === "list");
  $("#galleryViewButton").classList.toggle("active", view === "gallery");
  $("#listViewButton").setAttribute("aria-pressed", String(view === "list"));
  $("#galleryViewButton").setAttribute("aria-pressed", String(view === "gallery"));
  try { localStorage.setItem("captionStudioView", view); } catch (_) { /* local preference is optional */ }
  renderList();
}

function keySourceText(source) {
  return {
    session: "本次运行已配置",
    environment: "来自 OPENAI_API_KEY",
    local_file: "已保存在此电脑",
    none: "尚未配置",
  }[source] || "尚未配置";
}

function toggleProviderSettings() {
  const provider = $("#providerSelect").value;
  $("#codexSettings").classList.toggle("hidden", provider !== "codex");
  $("#openaiSettings").classList.toggle("hidden", provider !== "openai");
  $("#providerDetail").textContent = provider === state.provider?.id
    ? state.provider.detail
    : `保存后切换到 ${provider === "codex" ? "Codex CLI" : "OpenAI API"}`;
}

function populateSettings() {
  if (!state.settings) return;
  $("#providerSelect").value = state.settings.provider || "codex";
  $("#openaiModelInput").value = state.settings.openai_model || "gpt-5.6-luna";
  $("#codexModelInput").value = state.settings.codex_model || "";
  $("#triggerInput").value = state.settings.trigger || state.trigger;
  $("#apiKeyStatus").textContent = keySourceText(state.settings.api_key_source);
  $("#codexStatus").textContent = state.provider?.id === "codex"
    ? state.provider.detail
    : "启动时只会检查系统 PATH 中是否存在 codex。";
  $("#providerDetail").textContent = state.provider?.detail || "";
  toggleProviderSettings();
}

function openSettings() {
  populateSettings();
  $("#apiKeyInput").value = "";
  $("#saveApiKey").checked = false;
  if (!$("#settingsDialog").open) $("#settingsDialog").showModal();
}

async function saveSettings({ forgetApiKey = false } = {}) {
  const submit = $("#settingsForm button[type='submit']");
  submit.disabled = true;
  try {
    const data = await api("/api/settings", {
      provider: $("#providerSelect").value,
      openai_model: $("#openaiModelInput").value.trim(),
      codex_model: $("#codexModelInput").value.trim(),
      trigger: $("#triggerInput").value.trim(),
      api_key: $("#apiKeyInput").value.trim(),
      save_api_key: $("#saveApiKey").checked,
      forget_api_key: forgetApiKey,
    });
    $("#apiKeyInput").value = "";
    applyData(data);
    if (forgetApiKey) toast("已清除本机保存的 OpenAI API Key");
    else {
      $("#settingsDialog").close();
      toast(data.provider.ready ? "AI 设置已保存" : data.provider.detail, 6500);
    }
  } catch (error) {
    toast(error.message, 6500);
  } finally {
    submit.disabled = false;
  }
}

$("#imageList").addEventListener("click", (event) => {
  const checkbox = event.target.closest("input[data-check-id]");
  if (checkbox) {
    const id = checkbox.dataset.checkId;
    checkbox.checked ? state.checked.add(id) : state.checked.delete(id);
    renderList();
    return;
  }
  if (event.target.closest(".card-check")) return;
  const card = event.target.closest(".image-card");
  if (card) selectItem(card.dataset.id);
});

$("#imageList").addEventListener("dblclick", (event) => {
  const card = event.target.closest(".image-card");
  if (!card) return;
  selectItem(card.dataset.id);
  openPreview();
});

$("#imageList").addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const card = event.target.closest(".image-card");
  if (!card || event.target.closest(".card-check")) return;
  event.preventDefault();
  selectItem(card.dataset.id);
});

$("#selectAll").addEventListener("change", (event) => {
  const items = [...state.filtered];
  items.forEach((item) => event.target.checked ? state.checked.add(item.id) : state.checked.delete(item.id));
  renderList();
});

$("#statusTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (button) setFilter(button.dataset.filter);
});

$("#searchInput").addEventListener("input", (event) => {
  state.search = event.target.value.trim().toLocaleLowerCase();
  renderList();
});

$("#captionInput").addEventListener("input", () => {
  state.dirty = true;
  updateCaptionStats();
});

$("#startButton").addEventListener("click", startSelected);
$("#pauseButton").addEventListener("click", async () => {
  try {
    if (state.queueStatus === "paused") {
      await api("/api/resume");
      toast("已继续生成");
    } else {
      await api("/api/pause");
      toast("已立即暂停；当前批次不会写入，可稍后继续");
    }
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

$("#retryButton").addEventListener("click", async () => {
  try {
    const result = await api("/api/retry");
    setFilter("ungenerated");
    toast(`已重新排队 ${result.queued} 张`);
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

$("#regenerateButton").addEventListener("click", async () => {
  if (!state.selectedId) return;
  const button = $("#regenerateButton");
  button.disabled = true;
  try {
    await api("/api/generate", {
      ids: [state.selectedId],
      guidance: $("#regenerateGuidance").value,
    });
    $("#regenerateGuidance").value = "";
    toast("当前图片已加入重新生成队列");
    await refresh();
  } catch (error) {
    toast(error.message, 6000);
  } finally {
    button.disabled = state.byId.get(state.selectedId)?.status === "generating";
  }
});

$("#confirmButton").addEventListener("click", confirmCurrent);
$("#listViewButton").addEventListener("click", () => setView("list"));
$("#galleryViewButton").addEventListener("click", () => setView("gallery"));
$("#settingsButton").addEventListener("click", openSettings);
$("#providerStatusButton").addEventListener("click", openSettings);
$("#closeSettings").addEventListener("click", () => $("#settingsDialog").close());
$("#providerSelect").addEventListener("change", toggleProviderSettings);
$("#settingsForm").addEventListener("submit", (event) => {
  event.preventDefault();
  saveSettings();
});
$("#forgetApiKey").addEventListener("click", () => saveSettings({ forgetApiKey: true }));
$("#settingsDialog").addEventListener("click", (event) => {
  if (event.target === $("#settingsDialog")) $("#settingsDialog").close();
});

$("#chooseFolderButton").addEventListener("click", async () => {
  const button = $("#chooseFolderButton");
  button.disabled = true;
  button.textContent = "等待选择…";
  try {
    const result = await api("/api/dataset/select");
    if (!result.cancelled) {
      applyData(result.data, { preserveSelection: false });
      setFilter("all");
      toast(`已载入 ${result.data.items.length} 张图片`);
    }
  } catch (error) {
    toast(error.message, 6500);
  } finally {
    button.disabled = false;
    button.textContent = "选择文件夹";
  }
});

$("#emptyChooseFolderButton").addEventListener("click", () => $("#chooseFolderButton").click());

function openPreview() {
  if (!state.selectedId) return;
  $("#previewImage").src = `/original/${state.selectedId}`;
  if (!$("#previewDialog").open) $("#previewDialog").showModal();
}

$("#expandButton").addEventListener("click", openPreview);
$("#mainImage").addEventListener("dblclick", openPreview);
$("#closePreview").addEventListener("click", () => $("#previewDialog").close());
$("#previewDialog").addEventListener("click", (event) => {
  if (event.target === $("#previewDialog")) $("#previewDialog").close();
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    confirmCurrent();
    return;
  }
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (typing) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    moveSelection(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    moveSelection(1);
  } else if (event.code === "Space") {
    event.preventDefault();
    if ($("#previewDialog").open) $("#previewDialog").close(); else openPreview();
  }
});

if (isDirectFile) {
  const projectUrl = new URL("../../", window.location.href);
  const projectPath = decodeURIComponent(projectUrl.pathname);
  $("#launcherPath").textContent = projectPath;
  $("#copyLauncherPath").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    try {
      await navigator.clipboard.writeText(projectPath);
    } catch (_) {
      const temporary = document.createElement("textarea");
      temporary.value = projectPath;
      document.body.appendChild(temporary);
      temporary.select();
      document.execCommand("copy");
      temporary.remove();
    }
    button.textContent = "路径已复制";
    setTimeout(() => { button.textContent = "复制项目路径"; }, 1800);
  });
} else {
  try {
    if (localStorage.getItem("captionStudioView") === "gallery") state.view = "gallery";
  } catch (_) { /* local preference is optional */ }
  setView(state.view);
  refresh({ preserveSelection: false });
  setInterval(() => refresh(), 2200);
}
