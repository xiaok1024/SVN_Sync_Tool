"use strict";

const MAX_HTML_FILE_BYTES = 1024 * 1024;

const SAMPLE_HTML = `
<style>
  .upgrade-file { color: rgb(232, 63, 63); }
  .context-file { color: #222222; }
</style>
<div>QC123456 优化门户登录体验 —— 门户</div>
<div class="upgrade-file">https://svn.example.com/svn/customer/ecology/src/com/example/LoginService.java(V120)</div>
<div class="context-file">https://svn.example.com/svn/customer/ecology/src/com/example/LoginConfig.java(V118)</div>
<div>QC123457 修复移动端展示问题 —— 移动端</div>
<div style="color: #d92d20">https://svn.example.com/svn/customer/ecology/mobile/js/demo/page.js(V121)</div>
`;

const state = {
  richHtml: "",
  format: "md",
  filename: "",
  result: "",
  suppressSourceInput: false,
  sourceRevision: 0,
  listRevision: 0,
};

const elements = {
  sourceInput: document.querySelector("#sourceInput"),
  sourceState: document.querySelector("#sourceState"),
  htmlFile: document.querySelector("#htmlFile"),
  fileButton: document.querySelector("#fileButton"),
  sampleButton: document.querySelector("#sampleButton"),
  extractButton: document.querySelector("#extractButton"),
  clearButton: document.querySelector("#clearButton"),
  listInput: document.querySelector("#listInput"),
  generateButton: document.querySelector("#generateButton"),
  warningList: document.querySelector("#warningList"),
  resultOutput: document.querySelector("#resultOutput"),
  resultCustomer: document.querySelector("#resultCustomer"),
  resultFilename: document.querySelector("#resultFilename"),
  resultHint: document.querySelector("#resultHint"),
  copyButton: document.querySelector("#copyButton"),
  downloadButton: document.querySelector("#downloadButton"),
  notice: document.querySelector("#notice"),
  qcCount: document.querySelector("#qcCount"),
  fileCount: document.querySelector("#fileCount"),
  redCount: document.querySelector("#redCount"),
  blackCount: document.querySelector("#blackCount"),
  formatButtons: Array.from(document.querySelectorAll(".format-option")),
  stages: Array.from(document.querySelectorAll("#upgradeToolView .flow-step")),
};

let noticeTimer = null;

function setSourceValue(value) {
  state.suppressSourceInput = true;
  elements.sourceInput.value = value;
  state.suppressSourceInput = false;
}

function setSourceState(kind, message) {
  elements.sourceState.dataset.state = kind;
  elements.sourceState.textContent = message;
}

function setStage(stage) {
  elements.stages.forEach((item, index) => {
    const step = index + 1;
    item.classList.toggle("is-active", step === stage);
    item.classList.toggle("is-complete", step < stage);
  });
}

function showNotice(message, kind = "info") {
  window.clearTimeout(noticeTimer);
  elements.notice.textContent = message;
  elements.notice.dataset.kind = kind;
  elements.notice.classList.add("is-visible");
  noticeTimer = window.setTimeout(() => elements.notice.classList.remove("is-visible"), 4200);
}

function setButtonBusy(button, busy, busyText) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.classList.toggle("is-busy", busy);
  const label = button.querySelector(".button-label");
  if (label) {
    label.textContent = busy ? busyText : label.dataset.default;
  } else {
    if (!button.dataset.label) button.dataset.label = button.textContent.trim();
    button.textContent = busy ? busyText : button.dataset.label;
  }
}

function resetResult(message = "编辑清单后，选择格式并生成。") {
  state.filename = "";
  state.result = "";
  elements.resultOutput.value = "";
  elements.resultCustomer.textContent = "尚未生成";
  elements.resultFilename.textContent = "—";
  elements.resultHint.textContent = message;
  elements.copyButton.disabled = true;
  elements.downloadButton.disabled = true;
}

function resetReview(message = "编辑清单后，选择格式并生成。") {
  state.listRevision += 1;
  elements.listInput.value = "";
  elements.listInput.disabled = true;
  elements.generateButton.disabled = true;
  elements.formatButtons.forEach((button) => {
    button.disabled = true;
  });
  updateSummary();
  showWarnings();
  resetResult(message);
  setStage(1);
}

function invalidateSourceRevision() {
  state.sourceRevision += 1;
  resetReview("源内容已变化，请重新提取升级清单。");
}

function updateSummary(summary = null) {
  elements.qcCount.textContent = summary ? String(summary.qc_count) : "—";
  elements.fileCount.textContent = summary ? String(summary.file_line_count) : "—";
  elements.redCount.textContent = summary ? String(summary.red_count) : "—";
  elements.blackCount.textContent = summary ? String(summary.black_count) : "—";
}

function showWarnings(warnings = []) {
  elements.warningList.replaceChildren();
  if (!warnings.length) {
    elements.warningList.hidden = true;
    return;
  }
  warnings.forEach((warning) => {
    const item = document.createElement("p");
    item.className = "warning-item";
    item.textContent = warning.message;
    elements.warningList.append(item);
  });
  elements.warningList.hidden = false;
}

/** 服务端没有返回统一错误体时的兜底说明，避免把 404 误读成业务失败。 */
function httpFallbackMessage(status) {
  if (status === 404) {
    return "接口不存在（404）：页面已是新版，但服务端进程还是旧的，请重启 Web 服务。";
  }
  if (status === 405) return "接口不支持该请求方式（405），请重启 Web 服务后重试。";
  if (status === 502 || status === 503 || status === 504) {
    return `服务暂时不可用（${status}），请稍后重试。`;
  }
  return `处理失败（HTTP ${status}），请稍后重试。`;
}

async function requestJson(method, url, payload) {
  const init = { method, headers: { Accept: "application/json" } };
  if (payload !== null && payload !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload);
  }
  const response = await fetch(url, init);
  let data;
  try {
    data = await response.json();
  } catch (_error) {
    throw new Error(response.ok
      ? "服务返回了无法识别的响应"
      : httpFallbackMessage(response.status));
  }
  if (!response.ok || !data.ok) {
    // 会话过期或未登录：任何接口都可能返回，统一退回登录门。
    if (response.status === 401 && data?.error?.code === "login_required"
        && typeof applyUser === "function") {
      applyUser(null);
    }
    throw new Error(data?.error?.message || httpFallbackMessage(response.status));
  }
  return data;
}

function postJson(url, payload) {
  return requestJson("POST", url, payload);
}

function plainTextFromHtml(html) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  return (parsed.body.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
}

function loadRichHtml(html, label) {
  invalidateSourceRevision();
  state.richHtml = html;
  setSourceValue(plainTextFromHtml(html));
  setSourceState("rich", label);
  elements.sourceInput.focus();
}

function invalidateSource() {
  if (state.suppressSourceInput) return;
  invalidateSourceRevision();
  state.richHtml = "";
  setSourceState(elements.sourceInput.value.trim() ? "plain" : "empty",
    elements.sourceInput.value.trim() ? "仅检测到纯文本" : "等待粘贴");
}

async function extractList() {
  let html = state.richHtml;
  const sourceText = elements.sourceInput.value.trim();
  if (!html && /^\s*</.test(sourceText)) html = sourceText;
  if (!html) {
    showNotice("没有检测到富文本 HTML，请重新从网页复制或读取 HTML 文件。", "warning");
    elements.sourceInput.focus();
    return;
  }
  const sourceRevision = state.sourceRevision;
  setButtonBusy(elements.extractButton, true, "正在提取…");
  try {
    const data = await postJson("/api/v1/upgrade-list/extract", { html });
    if (sourceRevision !== state.sourceRevision) return;
    elements.listInput.disabled = false;
    elements.listInput.value = data.list_text;
    elements.generateButton.disabled = false;
    elements.formatButtons.forEach((button) => {
      button.disabled = false;
    });
    state.listRevision += 1;
    updateSummary(data.summary);
    showWarnings(data.warnings);
    resetResult("清单已提取，可以校对后生成 Markdown。");
    setStage(2);
    showNotice(`已提取 ${data.summary.qc_count} 个 QC、${data.summary.file_line_count} 个文件。`, "success");
    elements.listInput.focus();
  } catch (error) {
    if (sourceRevision !== state.sourceRevision) return;
    showNotice(error.message, "error");
  } finally {
    setButtonBusy(elements.extractButton, false);
  }
}

async function generateMarkdown() {
  const listText = elements.listInput.value;
  if (!listText.trim()) {
    showNotice("请先提取或填写升级清单。", "warning");
    elements.listInput.focus();
    return;
  }
  const listRevision = state.listRevision;
  setButtonBusy(elements.generateButton, true, "正在生成…");
  try {
    const data = await postJson("/api/v1/upgrade-list/generate", {
      list_text: listText,
      format: state.format,
    });
    if (listRevision !== state.listRevision) return;
    state.filename = data.filename;
    state.result = data.content;
    elements.resultOutput.value = data.content;
    elements.resultCustomer.textContent = `客户：${data.customer}`;
    elements.resultFilename.textContent = data.filename;
    elements.resultHint.textContent = `共 ${data.stats.qc} 个 QC、${data.stats.unique_files} 个唯一文件。`;
    elements.copyButton.disabled = false;
    elements.downloadButton.disabled = false;
    showWarnings(data.warnings);
    setStage(3);
    showNotice("Markdown 已生成。", "success");
    elements.resultOutput.focus();
  } catch (error) {
    if (listRevision !== state.listRevision) return;
    showNotice(error.message, "error");
  } finally {
    setButtonBusy(elements.generateButton, false);
    elements.generateButton.disabled = elements.listInput.disabled || !elements.listInput.value.trim();
  }
}

async function copyResult() {
  if (!state.result) return;
  try {
    await navigator.clipboard.writeText(state.result);
    showNotice("结果已复制到剪贴板。", "success");
  } catch (_error) {
    elements.resultOutput.select();
    const copied = document.execCommand("copy");
    showNotice(copied ? "结果已复制到剪贴板。" : "复制失败，请手工复制。", copied ? "success" : "error");
  }
}

function downloadResult() {
  if (!state.result || !state.filename) return;
  const blob = new Blob([state.result], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = state.filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showNotice(`已下载 ${state.filename}。`, "success");
}

function clearWorkspace() {
  state.sourceRevision += 1;
  state.richHtml = "";
  state.format = "md";
  setSourceValue("");
  setSourceState("empty", "等待粘贴");
  elements.htmlFile.value = "";
  resetReview();
  elements.formatButtons.forEach((button) => {
    const selected = button.dataset.format === "md";
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  elements.sourceInput.focus();
}

elements.sourceInput.addEventListener("paste", (event) => {
  const html = event.clipboardData?.getData("text/html") || "";
  const text = event.clipboardData?.getData("text/plain") || "";
  if (html) {
    event.preventDefault();
    invalidateSourceRevision();
    state.richHtml = html;
    setSourceValue(text || plainTextFromHtml(html));
    setSourceState("rich", "已保留富文本颜色");
    showNotice("已读取富文本，可以开始提取。", "success");
  } else {
    window.setTimeout(() => {
      state.richHtml = "";
      setSourceState("plain", "仅检测到纯文本");
      showNotice("当前剪贴板没有 HTML，红黑颜色可能已经丢失。", "warning");
    }, 0);
  }
});

elements.sourceInput.addEventListener("input", invalidateSource);
elements.sourceInput.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    extractList();
  }
});

elements.listInput.addEventListener("input", () => {
  state.listRevision += 1;
  resetResult("清单已修改，请重新生成 Markdown。");
  elements.generateButton.disabled = !elements.listInput.value.trim();
  setStage(2);
});
elements.listInput.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    generateMarkdown();
  }
});

elements.sampleButton.addEventListener("click", () => {
  loadRichHtml(SAMPLE_HTML, "已加载演示富文本");
  showNotice("示例已载入，点击“提取升级清单”查看效果。", "info");
});
elements.fileButton.addEventListener("click", () => elements.htmlFile.click());
elements.htmlFile.addEventListener("change", async () => {
  const file = elements.htmlFile.files?.[0];
  if (!file) return;
  if (file.size > MAX_HTML_FILE_BYTES) {
    elements.htmlFile.value = "";
    showNotice("HTML 文件超过 1 MiB 限制。", "error");
    return;
  }
  try {
    const html = await file.text();
    loadRichHtml(html, `已读取 ${file.name}`);
    showNotice("HTML 文件已读取，可以开始提取。", "success");
  } catch (_error) {
    showNotice("读取 HTML 文件失败。", "error");
  }
});

elements.formatButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.listRevision += 1;
    state.format = button.dataset.format;
    elements.formatButtons.forEach((item) => {
      const selected = item === button;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    resetResult("输出格式已切换，请重新生成。");
    setStage(2);
  });
});

elements.extractButton.addEventListener("click", extractList);
elements.generateButton.addEventListener("click", generateMarkdown);
elements.clearButton.addEventListener("click", clearWorkspace);
elements.copyButton.addEventListener("click", copyResult);
elements.downloadButton.addEventListener("click", downloadResult);

const STANDARD_SAMPLE = `src/com/example/LoginService.java
WEB-INF/prop/example-login.properties`;

const standardState = {
  profilesReady: false,
  taskId: null,
  accessToken: null,
  confirmationToken: null,
  commitIdempotencyKey: null,
  previewSignature: null,
  task: null,
  pollTimer: null,
  pollFailures: 0,
};

const standardElements = {
  toolLinks: Array.from(document.querySelectorAll("[data-tool-link]")),
  toolViews: Array.from(document.querySelectorAll("[data-tool-view]")),
  stages: Array.from(document.querySelectorAll("#standardToolView .flow-step")),
  form: document.querySelector("#standardTaskForm"),
  errorSummary: document.querySelector("#standardErrorSummary"),
  profileAlert: document.querySelector("#sourceProfileAlert"),
  svnUrl: document.querySelector("#standardSvnUrl"),
  sourceProfile: document.querySelector("#standardSourceProfile"),
  sourceProfileDetail: document.querySelector("#standardSourceProfileDetail"),
  customerPath: document.querySelector("#standardCustomerPath"),
  fileList: document.querySelector("#standardFileList"),
  fileCount: document.querySelector("#standardFileCount"),
  coverAllConfirm: document.querySelector("#standardCoverAllConfirm"),
  coverAllConfirmWrap: document.querySelector("#standardCoverAllConfirmWrap"),
  commitMessage: document.querySelector("#standardCommitMessage"),
  messageCount: document.querySelector("#standardMessageCount"),
  sampleButton: document.querySelector("#standardSampleButton"),
  createButton: document.querySelector("#createStandardPreviewButton"),
  resetButton: document.querySelector("#resetStandardFormButton"),
  stateBadge: document.querySelector("#standardTaskStateBadge"),
  status: document.querySelector("#standardTaskStatus"),
  progressBar: document.querySelector("#standardTaskProgressBar"),
  emptyState: document.querySelector("#standardTaskEmptyState"),
  previewSection: document.querySelector("#standardPreviewSection"),
  previewTitle: document.querySelector("#standardPreviewTitle"),
  previewRevision: document.querySelector("#standardPreviewRevision"),
  previewSummary: document.querySelector("#standardPreviewSummary"),
  previewIssues: document.querySelector("#standardPreviewIssues"),
  previewItems: document.querySelector("#standardPreviewItems"),
  confirmButton: document.querySelector("#confirmStandardCommitButton"),
  cancelButton: document.querySelector("#cancelStandardTaskButton"),
  result: document.querySelector("#standardCommitResult"),
  resultMark: document.querySelector("#standardResultMark"),
  resultTitle: document.querySelector("#standardResultTitle"),
  resultMessage: document.querySelector("#standardResultMessage"),
  resultRevision: document.querySelector("#standardResultRevision"),
  resultUrls: document.querySelector("#standardResultUrls"),
  log: document.querySelector("#standardTaskLog"),
  dialog: document.querySelector("#standardCommitDialog"),
  dialogSvnUrl: document.querySelector("#dialogSvnUrl"),
  dialogFileCount: document.querySelector("#dialogFileCount"),
  dialogCommitMessage: document.querySelector("#dialogCommitMessage"),
  acknowledge: document.querySelector("#standardCommitAcknowledge"),
  dialogError: document.querySelector("#standardCommitDialogError"),
  submitCommitButton: document.querySelector("#submitStandardCommitButton"),
  closeDialogButton: document.querySelector("#closeStandardCommitDialogButton"),
};

/** 键的顺序即导航顺序；第一项同时是无 hash 时的默认工具。 */
const TOOLS = {
  path: { hash: "#revision-paths", title: "#path-page-title" },
  standard: { hash: "#svn-standard", title: "#standard-page-title" },
  upgrade: { hash: "#upgrade-list", title: "#page-title" },
};
const DEFAULT_TOOL = Object.keys(TOOLS)[0];

function toolFromHash(hash) {
  const found = Object.keys(TOOLS).find((name) => TOOLS[name].hash === hash);
  return found || DEFAULT_TOOL;
}

function selectTool(tool, focusTitle = false) {
  const selected = TOOLS[tool] ? tool : DEFAULT_TOOL;
  standardElements.toolViews.forEach((view) => {
    view.hidden = view.dataset.toolView !== selected;
  });
  standardElements.toolLinks.forEach((link) => {
    if (link.dataset.toolLink === selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });
  const desiredHash = TOOLS[selected].hash;
  if (window.location.hash !== desiredHash) history.replaceState(null, "", desiredHash);
  if (focusTitle) {
    document.querySelector(TOOLS[selected].title)?.focus({ preventScroll: true });
  }
}

standardElements.toolLinks.forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    selectTool(link.dataset.toolLink, true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
});

window.addEventListener("hashchange", () => {
  selectTool(toolFromHash(window.location.hash));
});

function standardApiError(message, code = "request_failed") {
  const error = new Error(message);
  error.code = code;
  return error;
}

async function standardRequest(url, { method = "GET", payload = null, token = null } = {}) {
  const headers = {};
  if (payload !== null) headers["Content-Type"] = "application/json";
  if (token) headers["X-LZR-Job-Token"] = token;
  const response = await fetch(url, {
    method,
    headers,
    body: payload === null ? null : JSON.stringify(payload),
  });
  let data;
  try {
    data = await response.json();
  } catch (_error) {
    throw standardApiError("服务返回了无法识别的响应");
  }
  if (!response.ok || !data.ok) {
    if (response.status === 401 && data?.error?.code === "login_required") {
      applyUser(null);
    }
    throw standardApiError(
      data?.error?.message || httpFallbackMessage(response.status), data?.error?.code);
  }
  return data;
}

function updateStandardCounters() {
  const lineCount = standardElements.fileList.value.split(/\r?\n/).filter((line) => line.trim()).length;
  const coverAll = lineCount === 0;
  standardElements.fileCount.textContent = coverAll ? "空清单 · 全部交集模式" : `${lineCount} 行 · 指定清单模式`;
  standardElements.coverAllConfirmWrap.hidden = !coverAll;
  if (!coverAll) standardElements.coverAllConfirm.checked = false;
  standardElements.messageCount.textContent = `${standardElements.commitMessage.value.length} / 500`;
}

function setStandardStage(stage) {
  const order = ["form", "checkout", "preview", "commit", "cleanup"];
  const activeIndex = Math.max(0, order.indexOf(stage));
  standardElements.stages.forEach((item, index) => {
    item.classList.toggle("is-active", index === activeIndex);
    item.classList.toggle("is-complete", index < activeIndex);
  });
}

function stageForTask(task) {
  if (["queued", "preparing"].includes(task.status)) return "checkout";
  if (task.status === "preview_ready") return "preview";
  if (["commit_queued", "committing"].includes(task.status)) return "commit";
  if (task.status === "committed") return "cleanup";
  if (task.status === "commit_unknown" || task.error?.code?.startsWith("commit_")) return "commit";
  if (["no_changes", "failed", "expired", "cancelled"].includes(task.status)) {
    return task.checkout_revision ? "preview" : "checkout";
  }
  return "form";
}

function setStandardStatus(title, detail, { active = false, kind = "idle" } = {}) {
  const titleElement = standardElements.status.querySelector("strong");
  const detailElement = standardElements.status.querySelector("span:last-child");
  titleElement.textContent = title;
  detailElement.textContent = detail;
  standardElements.status.dataset.active = String(active);
  standardElements.stateBadge.dataset.state = kind;
  standardElements.stateBadge.textContent = title;
}

function setStandardFormLocked(locked) {
  [
    standardElements.svnUrl,
    standardElements.sourceProfile,
    standardElements.customerPath,
    standardElements.fileList,
    standardElements.coverAllConfirm,
    standardElements.commitMessage,
    standardElements.sampleButton,
  ].forEach((control) => {
    control.disabled = locked;
  });
  standardElements.createButton.disabled = locked || !standardState.profilesReady;
}

function clearStandardErrors() {
  standardElements.errorSummary.hidden = true;
  standardElements.errorSummary.textContent = "";
  [
    standardElements.svnUrl,
    standardElements.sourceProfile,
    standardElements.customerPath,
    standardElements.fileList,
    standardElements.commitMessage,
  ].forEach((control) => control.removeAttribute("aria-invalid"));
}

function showStandardError(message, control = null) {
  standardElements.errorSummary.textContent = message;
  standardElements.errorSummary.hidden = false;
  if (control) {
    control.setAttribute("aria-invalid", "true");
    control.focus();
  } else {
    standardElements.errorSummary.focus();
  }
}

function validateStandardForm() {
  clearStandardErrors();
  const checks = [
    [standardElements.svnUrl, "请填写客户 SVN 检出根。"],
    [standardElements.sourceProfile, "请选择可用的标准文件来源。"],
    [standardElements.customerPath, "请填写客户标准文件 ecology 目录。"],
    [standardElements.commitMessage, "请填写 SVN 提交说明。"],
  ];
  for (const [control, message] of checks) {
    if (!control.value.trim()) {
      showStandardError(message, control);
      return false;
    }
  }
  try {
    const parsed = new URL(standardElements.svnUrl.value.trim());
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("scheme");
  } catch (_error) {
    showStandardError("SVN 检出根必须是完整的 http 或 https 地址。", standardElements.svnUrl);
    return false;
  }
  if (!standardElements.fileList.value.trim() && !standardElements.coverAllConfirm.checked) {
    showStandardError("清单为空时，请确认只覆盖 SVN 与标准目录同时存在的全部文件。", standardElements.coverAllConfirm);
    return false;
  }
  return true;
}

async function loadStandardProfiles() {
  try {
    const data = await standardRequest("/api/v1/standard-files/source-profiles");
    standardElements.sourceProfile.replaceChildren();
    const available = data.profiles.filter((profile) => profile.available);
    if (!available.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = data.configured ? "来源配置当前不可用" : "服务端尚未配置来源";
      standardElements.sourceProfile.append(option);
      standardElements.profileAlert.hidden = false;
      standardElements.sourceProfile.disabled = true;
      standardElements.createButton.disabled = true;
      standardElements.sourceProfileDetail.textContent = "服务端配置完成后刷新页面即可使用。";
      return;
    }
    available.forEach((profile) => {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = profile.label;
      option.dataset.detail = profile.priority;
      option.dataset.uncPrefix = profile.unc_prefix || "";
      standardElements.sourceProfile.append(option);
    });
    standardState.profilesReady = true;
    standardElements.profileAlert.hidden = true;
    standardElements.sourceProfile.disabled = Boolean(standardState.taskId);
    standardElements.createButton.disabled = Boolean(standardState.taskId);
    updateSelectedProfileDetail();
  } catch (error) {
    standardElements.profileAlert.hidden = false;
    standardElements.profileAlert.querySelector("strong").textContent = "无法读取标准文件来源";
    standardElements.profileAlert.querySelector("span").textContent = error.message;
    standardElements.sourceProfile.disabled = true;
    standardElements.createButton.disabled = true;
  }
}

function updateSelectedProfileDetail() {
  const option = standardElements.sourceProfile.selectedOptions[0];
  standardElements.sourceProfileDetail.textContent = option?.dataset.detail
    ? `${option.dataset.detail}；允许的共享根：${option.dataset.uncPrefix || "由服务器配置"}。`
    : "网页只显示配置名称，不暴露服务器真实目录。";
}

function saveStandardTaskSession() {
  if (!standardState.taskId || !standardState.accessToken) return;
  sessionStorage.setItem("lzr-standard-task", JSON.stringify({
    id: standardState.taskId,
    token: standardState.accessToken,
  }));
}

function clearStandardTaskSession() {
  sessionStorage.removeItem("lzr-standard-task");
}

function restoreStandardTaskSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("lzr-standard-task") || "null");
    if (saved?.id && saved?.token) {
      standardState.taskId = saved.id;
      standardState.accessToken = saved.token;
      setStandardFormLocked(true);
      scheduleStandardPoll(0);
    }
  } catch (_error) {
    clearStandardTaskSession();
  }
}

async function createStandardTask(event) {
  event.preventDefault();
  if (!validateStandardForm()) return;
  const payload = {
    svn_url: standardElements.svnUrl.value.trim(),
    source_profile_id: standardElements.sourceProfile.value,
    customer_standard_path: standardElements.customerPath.value.trim(),
    file_list: standardElements.fileList.value,
    cover_all_confirmed: !standardElements.fileList.value.trim() && standardElements.coverAllConfirm.checked,
    commit_message: standardElements.commitMessage.value.trim(),
  };
  setButtonBusy(standardElements.createButton, true, "正在创建…");
  clearStandardErrors();
  try {
    const data = await standardRequest("/api/v1/standard-files/tasks", { method: "POST", payload });
    standardState.taskId = data.task.id;
    standardState.accessToken = data.task.access_token;
    standardState.confirmationToken = null;
    standardState.commitIdempotencyKey = null;
    standardState.previewSignature = null;
    standardState.pollFailures = 0;
    saveStandardTaskSession();
    setStandardFormLocked(true);
    standardElements.emptyState.hidden = true;
    setStandardStatus("任务已创建", "正在等待独立临时检出。", { active: true, kind: "active" });
    setStandardStage("checkout");
    showNotice("标准文件任务已创建，正在生成安全预览。", "success");
    scheduleStandardPoll(250);
  } catch (error) {
    showStandardError(error.message);
    showNotice(error.message, "error");
  } finally {
    setButtonBusy(standardElements.createButton, false);
    standardElements.createButton.disabled = Boolean(standardState.taskId) || !standardState.profilesReady;
  }
}

function scheduleStandardPoll(delay = 1500) {
  window.clearTimeout(standardState.pollTimer);
  standardState.pollTimer = window.setTimeout(pollStandardTask, delay);
}

function schedulePollForStandardTask(task, activeDelay = 1500) {
  const active = ["queued", "preparing", "preview_ready", "commit_queued", "committing"].includes(task.status);
  const terminalCleanupPending = ["committed", "no_changes", "failed", "expired", "cancelled", "commit_unknown"]
    .includes(task.status) && task.cleanup?.status !== "cleaned";
  if (active) {
    scheduleStandardPoll(task.status === "preview_ready" ? 5000 : activeDelay);
  } else if (terminalCleanupPending) {
    scheduleStandardPoll(30000);
  }
}

async function pollStandardTask() {
  if (!standardState.taskId || !standardState.accessToken) return;
  try {
    const data = await standardRequest(
      `/api/v1/standard-files/tasks/${encodeURIComponent(standardState.taskId)}`,
      { token: standardState.accessToken },
    );
    standardState.pollFailures = 0;
    standardState.task = data.task;
    renderStandardTask(data.task);
    schedulePollForStandardTask(data.task);
  } catch (error) {
    standardState.pollFailures += 1;
    if (error.code === "job_not_found") {
      showStandardError("任务不存在或服务已重启，请重新创建预览。");
      releaseStandardTaskState();
      return;
    }
    setStandardStatus("正在重新连接", "暂时无法读取任务状态；系统不会重复创建或提交。", { active: true, kind: "active" });
    scheduleStandardPoll(Math.min(15000, 1000 * (2 ** Math.min(standardState.pollFailures, 4))));
  }
}

function renderStandardSummary(summary = {}) {
  standardElements.previewSummary.replaceChildren();
  const fields = [
    ["清单", summary.requested ?? 0],
    ["变更", summary.changed ?? 0],
    ["相同", summary.unchanged ?? 0],
    ["缺失", summary.missing ?? 0],
  ];
  fields.forEach(([label, value]) => {
    const card = document.createElement("div");
    const name = document.createElement("span");
    const count = document.createElement("strong");
    name.textContent = label;
    count.textContent = String(value);
    card.append(name, count);
    standardElements.previewSummary.append(card);
  });
}

function renderStandardPreview(preview, task) {
  standardElements.previewSection.hidden = false;
  standardElements.emptyState.hidden = true;
  standardElements.result.hidden = true;
  standardState.confirmationToken = preview.confirmation_token || standardState.confirmationToken;
  const previewSignature = JSON.stringify({
    revision: task.checkout_revision,
    canCommit: task.can_commit,
    summary: preview.summary,
    items: preview.items,
    itemsTotal: preview.items_total,
    itemsTruncated: preview.items_truncated,
    issues: preview.blocking_issues,
  });
  if (standardState.previewSignature === previewSignature) {
    standardElements.confirmButton.disabled = !task.can_commit;
    standardElements.cancelButton.disabled = task.status !== "preview_ready";
    return;
  }
  standardState.previewSignature = previewSignature;
  standardElements.previewRevision.textContent = task.checkout_revision ? `基于 r${task.checkout_revision}` : "版本待确认";
  renderStandardSummary(preview.summary);
  standardElements.previewItems.replaceChildren();
  preview.items.forEach((item) => {
    const row = document.createElement("tr");
    const path = document.createElement("td");
    const source = document.createElement("td");
    const result = document.createElement("td");
    const tag = document.createElement("span");
    path.textContent = item.path;
    source.textContent = item.source;
    tag.className = "preview-status-tag";
    tag.dataset.kind = item.result === "已覆盖" ? "changed" : (item.result === "内容相同" ? "same" : "missing");
    tag.textContent = item.result;
    result.append(tag);
    row.append(path, source, result);
    standardElements.previewItems.append(row);
  });
  standardElements.previewIssues.replaceChildren();
  const previewNotices = [...preview.blocking_issues];
  if (preview.items_truncated) {
    previewNotices.push(`交集共有 ${preview.items_total} 个文件，页面仅展示前 1000 个；实际覆盖、校验和提交仍使用完整交集。`);
  }
  if (previewNotices.length) {
    previewNotices.forEach((issue) => {
      const paragraph = document.createElement("p");
      paragraph.textContent = issue;
      standardElements.previewIssues.append(paragraph);
    });
    standardElements.previewIssues.hidden = false;
  } else {
    standardElements.previewIssues.hidden = true;
  }
  standardElements.confirmButton.disabled = !task.can_commit;
  standardElements.cancelButton.disabled = task.status !== "preview_ready";
}

function renderStandardEvents(events = []) {
  standardElements.log.replaceChildren();
  events.forEach((event) => {
    const item = document.createElement("li");
    const timeElement = document.createElement("time");
    const message = document.createElement("span");
    const parsed = new Date(event.time);
    timeElement.dateTime = event.time;
    timeElement.textContent = Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleTimeString("zh-CN", { hour12: false });
    message.textContent = event.message;
    item.append(timeElement, message);
    standardElements.log.append(item);
  });
}

function renderStandardResult(task) {
  standardElements.previewSection.hidden = true;
  standardElements.emptyState.hidden = true;
  standardElements.result.hidden = false;
  standardElements.resultRevision.hidden = true;
  standardElements.resultUrls.replaceChildren();
  const cleanupStatus = task.cleanup?.status || "pending";
  const cleanupText = cleanupStatus === "cleaned"
    ? "临时工作副本和独立认证配置已清理。"
    : (cleanupStatus === "failed"
      ? "临时目录清理暂未完成，后台会继续安全重试。"
      : "临时目录正在等待后台清理。");
  if (task.status === "committed") {
    standardElements.result.dataset.kind = "success";
    standardElements.resultMark.textContent = "✓";
    standardElements.resultTitle.textContent = "SVN 提交成功";
    standardElements.resultMessage.textContent = `提交结果已经保存，${cleanupText}请通知相关人员更新 SVN 最新版本。`;
    if (task.result?.revision) {
      standardElements.resultRevision.textContent = `Revision r${task.result.revision}`;
      standardElements.resultRevision.hidden = false;
    }
    (task.result?.urls || []).forEach((url) => {
      const line = document.createElement("p");
      line.textContent = url;
      standardElements.resultUrls.append(line);
    });
  } else if (task.status === "no_changes") {
    standardElements.result.dataset.kind = "success";
    standardElements.resultMark.textContent = "✓";
    standardElements.resultTitle.textContent = "无需提交";
    standardElements.resultMessage.textContent = `标准文件与仓库内容一致，没有创建新的 SVN 版本；${cleanupText}`;
  } else if (task.status === "commit_unknown") {
    standardElements.result.dataset.kind = "warning";
    standardElements.resultMark.textContent = "?";
    standardElements.resultTitle.textContent = "提交结果需要核验";
    const unknownCleanupText = cleanupStatus === "pending"
      ? "用于核验的临时目录最多保留 1 小时，之后自动清理。"
      : cleanupText;
    standardElements.resultMessage.textContent = `${task.error?.message || "请勿重复提交，并在 SVN 日志中核验本次提交说明。"} ${unknownCleanupText}`;
    const markerLine = document.createElement("p");
    markerLine.textContent = `核验提交说明：${task.commit_message}`;
    standardElements.resultUrls.append(markerLine);
  } else {
    const cancelled = task.status === "cancelled";
    standardElements.result.dataset.kind = cancelled ? "neutral" : "error";
    standardElements.resultMark.textContent = cancelled ? "×" : "!";
    standardElements.resultTitle.textContent = cancelled ? "任务已取消" : "任务未完成";
    const baseMessage = task.error?.message || "临时任务已停止，没有再次发起 SVN 提交。";
    standardElements.resultMessage.textContent = `${baseMessage} ${cleanupText}`;
  }
}

function renderStandardTask(task) {
  if (task.status !== "preview_ready" && standardElements.dialog.open) {
    standardElements.dialog.close();
  }
  const active = ["queued", "preparing", "commit_queued", "committing"].includes(task.status);
  const success = ["preview_ready", "committed", "no_changes"].includes(task.status);
  const detail = task.error?.message || (
    task.status === "preview_ready"
      ? (task.can_commit ? "预览已固定，核对后可进行一次性提交。" : "预览存在阻塞项，当前不能提交。")
      : "任务在独立临时目录中运行。"
  );
  setStandardStatus(task.stage_label, detail, {
    active,
    kind: active ? "active" : (success ? "success" : (["failed", "expired", "cancelled", "commit_unknown"].includes(task.status) ? "error" : "idle")),
  });
  standardElements.progressBar.style.width = `${Math.max(0, Math.min(100, task.progress || 0))}%`;
  setStandardStage(stageForTask(task));
  renderStandardEvents(task.events);
  if (task.preview) renderStandardPreview(task.preview, task);
  if (["committed", "no_changes", "failed", "expired", "cancelled", "commit_unknown"].includes(task.status)) {
    renderStandardResult(task);
  }
}

function openStandardCommitDialog() {
  const task = standardState.task;
  if (!task?.can_commit || !standardState.confirmationToken) return;
  standardElements.dialogSvnUrl.textContent = task.svn_url;
  standardElements.dialogFileCount.textContent = `${task.preview?.summary?.changed || 0} 个`;
  standardElements.dialogCommitMessage.textContent = task.commit_message;
  standardElements.acknowledge.checked = false;
  standardElements.submitCommitButton.disabled = true;
  standardElements.dialogError.textContent = "";
  standardElements.dialogError.hidden = true;
  standardElements.dialog.showModal();
}

async function submitStandardCommit() {
  if (!standardState.taskId || !standardState.accessToken || !standardState.confirmationToken) return;
  setButtonBusy(standardElements.submitCommitButton, true, "正在提交…");
  if (!standardState.commitIdempotencyKey) {
    standardState.commitIdempotencyKey = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID().replaceAll("-", "")
      : `${Date.now()}_${Math.random().toString(36).slice(2)}_commit`;
  }
  try {
    const data = await standardRequest(
      `/api/v1/standard-files/tasks/${encodeURIComponent(standardState.taskId)}/commit`,
      {
        method: "POST",
        token: standardState.accessToken,
        payload: {
          confirmation_token: standardState.confirmationToken,
          idempotency_key: standardState.commitIdempotencyKey,
        },
      },
    );
    standardState.confirmationToken = null;
    standardElements.dialog.close();
    standardState.task = data.task;
    renderStandardTask(data.task);
    showNotice("提交请求已确认，正在复核并写入 SVN。", "success");
    scheduleStandardPoll(400);
  } catch (error) {
    showNotice(error.message, "error");
    standardElements.dialogError.textContent = `${error.message}。系统正在重新查询任务状态，不会自动重复提交。`;
    standardElements.dialogError.hidden = false;
    scheduleStandardPoll(0);
  } finally {
    setButtonBusy(standardElements.submitCommitButton, false);
    standardElements.submitCommitButton.disabled = !standardElements.acknowledge.checked;
  }
}

async function cancelStandardTask() {
  if (!standardState.taskId || !standardState.accessToken) return;
  standardElements.cancelButton.disabled = true;
  try {
    const data = await standardRequest(
      `/api/v1/standard-files/tasks/${encodeURIComponent(standardState.taskId)}`,
      { method: "DELETE", token: standardState.accessToken },
    );
    standardState.task = data.task;
    renderStandardTask(data.task);
    showNotice("取消请求已发送，临时目录会安全清理。", "success");
    schedulePollForStandardTask(data.task, 600);
  } catch (error) {
    showNotice(error.message, "error");
    standardElements.cancelButton.disabled = false;
  }
}

function releaseStandardTaskState() {
  window.clearTimeout(standardState.pollTimer);
  standardState.taskId = null;
  standardState.accessToken = null;
  standardState.confirmationToken = null;
  standardState.commitIdempotencyKey = null;
  standardState.previewSignature = null;
  standardState.task = null;
  clearStandardTaskSession();
  setStandardFormLocked(false);
  standardElements.previewSection.hidden = true;
  standardElements.result.hidden = true;
  standardElements.emptyState.hidden = false;
  standardElements.progressBar.style.width = "0%";
  setStandardStatus("等待配置", "填写左侧信息后创建提交预览。", { kind: "idle" });
  setStandardStage("form");
}

standardElements.form.addEventListener("submit", createStandardTask);
standardElements.form.addEventListener("reset", (event) => {
  const terminal = standardState.task
    && ["committed", "no_changes", "failed", "expired", "cancelled", "commit_unknown"].includes(standardState.task.status);
  if (standardState.taskId && !terminal) {
    event.preventDefault();
    cancelStandardTask();
    return;
  }
  window.setTimeout(() => {
    releaseStandardTaskState();
    clearStandardErrors();
    updateStandardCounters();
    standardElements.sourceProfile.disabled = !standardState.profilesReady;
  }, 0);
});
standardElements.fileList.addEventListener("input", updateStandardCounters);
standardElements.coverAllConfirm.addEventListener("change", clearStandardErrors);
standardElements.commitMessage.addEventListener("input", updateStandardCounters);
standardElements.sourceProfile.addEventListener("change", updateSelectedProfileDetail);
standardElements.sampleButton.addEventListener("click", () => {
  standardElements.svnUrl.value = "https://svn.example.com/svn/customer/ecology";
  standardElements.customerPath.value = "\\\\192.168.7.215\\ECOLOGY_customer\\Y\\示例客户\\QC123456\\ecology";
  standardElements.fileList.value = STANDARD_SAMPLE;
  standardElements.commitMessage.value = "QC123456 补充示例标准文件";
  updateStandardCounters();
  showNotice("已加载演示字段，请替换为真实仓库与清单。", "info");
});
standardElements.confirmButton.addEventListener("click", openStandardCommitDialog);
standardElements.cancelButton.addEventListener("click", cancelStandardTask);
standardElements.acknowledge.addEventListener("change", () => {
  standardElements.submitCommitButton.disabled = !standardElements.acknowledge.checked;
});
standardElements.closeDialogButton.addEventListener("click", () => standardElements.dialog.close());
standardElements.submitCommitButton.addEventListener("click", submitStandardCommit);

const PATH_MAX_REVISIONS = 200;
const PATH_SORT_LABELS = { rev: "按版本排序", path: "按路径排序", name: "按文件名排序" };

const pathState = {
  revision: 0,
  sort: "rev",
};

const pathElements = {
  form: document.querySelector("#pathQueryForm"),
  errorSummary: document.querySelector("#pathErrorSummary"),
  svnUrl: document.querySelector("#pathSvnUrl"),
  useHostCache: document.querySelector("#pathUseHostCache"),
  revisionSpec: document.querySelector("#pathRevisionSpec"),
  revisionCount: document.querySelector("#pathRevisionCount"),
  sortMode: document.querySelector("#pathSortMode"),
  queryButton: document.querySelector("#pathQueryButton"),
  sortButton: document.querySelector("#pathSortButton"),
  resetButton: document.querySelector("#pathResetButton"),
  stateBadge: document.querySelector("#pathStateBadge"),
  fileCount: document.querySelector("#pathFileCount"),
  requestedCount: document.querySelector("#pathRequestedCount"),
  matchedCount: document.querySelector("#pathMatchedCount"),
  errorCount: document.querySelector("#pathErrorCount"),
  output: document.querySelector("#pathResultOutput"),
  warningList: document.querySelector("#pathWarningList"),
  resultHint: document.querySelector("#pathResultHint"),
  copyButton: document.querySelector("#pathCopyButton"),
  downloadButton: document.querySelector("#pathDownloadButton"),
};

/** 与服务端 parse_revision_spec 一致的计数，仅用于界面提示。 */
function countRevisionSpec(spec) {
  const normalized = spec.trim().replace(/，/g, ",").replace(/[－–—]/g, "-");
  if (!normalized) return 0;
  const revisions = new Set();
  normalized.split(/[,\s]+/).forEach((part) => {
    if (!part) return;
    if (part.includes("-")) {
      const [rawStart, rawEnd] = part.split("-", 2);
      const start = Number.parseInt(rawStart, 10);
      const end = Number.parseInt(rawEnd, 10);
      if (!Number.isInteger(start) || !Number.isInteger(end)) return;
      const low = Math.min(start, end);
      const high = Math.max(start, end);
      // 界面只需要数量；超过上限即可停止展开，避免超大区间卡住输入框。
      for (let value = low; value <= high && revisions.size <= PATH_MAX_REVISIONS; value += 1) {
        revisions.add(value);
      }
      return;
    }
    const single = Number.parseInt(part, 10);
    if (Number.isInteger(single)) revisions.add(single);
  });
  return revisions.size;
}

function updatePathRevisionCount() {
  const count = countRevisionSpec(pathElements.revisionSpec.value);
  pathElements.revisionCount.textContent = count > PATH_MAX_REVISIONS
    ? `超过 ${PATH_MAX_REVISIONS} 个版本上限`
    : `${count} 个版本`;
}

function setPathBadge(state, label) {
  pathElements.stateBadge.dataset.state = state;
  pathElements.stateBadge.textContent = label;
}

function clearPathErrors() {
  pathElements.errorSummary.hidden = true;
  pathElements.errorSummary.textContent = "";
  [pathElements.svnUrl, pathElements.revisionSpec, pathElements.sortMode]
    .forEach((control) => control.removeAttribute("aria-invalid"));
}

function showPathError(message, control = null) {
  pathElements.errorSummary.textContent = message;
  pathElements.errorSummary.hidden = false;
  if (control) {
    control.setAttribute("aria-invalid", "true");
    control.focus();
  } else {
    pathElements.errorSummary.focus();
  }
}

function renderPathMessages(messages = []) {
  pathElements.warningList.replaceChildren();
  if (!messages.length) {
    pathElements.warningList.hidden = true;
    return;
  }
  messages.forEach((message) => {
    const item = document.createElement("p");
    item.className = "warning-item";
    item.textContent = message;
    pathElements.warningList.append(item);
  });
  pathElements.warningList.hidden = false;
}

function updatePathSummary(stats = null) {
  pathElements.fileCount.textContent = stats ? String(stats.file_count) : "—";
  pathElements.requestedCount.textContent = stats?.revision_count === undefined
    ? "—" : String(stats.revision_count);
  pathElements.matchedCount.textContent = stats?.matched_revisions
    ? String(stats.matched_revisions.length) : "—";
  pathElements.errorCount.textContent = stats ? String(stats.error_count) : "—";
}

function setPathResultAvailability() {
  const hasText = Boolean(pathElements.output.value.trim());
  pathElements.copyButton.disabled = !hasText;
  pathElements.downloadButton.disabled = !hasText;
}

function pathDownloadFilename() {
  const raw = pathElements.svnUrl.value.trim().replace(/\/+$/, "");
  let segment = "";
  if (raw) {
    try {
      segment = decodeURIComponent(raw.split("/").pop() || "");
    } catch (_error) {
      segment = raw.split("/").pop() || "";
    }
  }
  const safe = segment
    .replace(/[<>:"/\\|?*\s]/g, "")
    .replace(/^\.+|\.+$/g, "")
    .slice(0, 60);
  return `${safe || "svn"}-revision-paths.txt`;
}

function validatePathForm() {
  clearPathErrors();
  if (!pathElements.svnUrl.value.trim()) {
    showPathError("请填写 SVN 仓库地址。", pathElements.svnUrl);
    return false;
  }
  try {
    const parsed = new URL(pathElements.svnUrl.value.trim());
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("scheme");
  } catch (_error) {
    showPathError("SVN 仓库地址必须是完整的 http 或 https 地址。", pathElements.svnUrl);
    return false;
  }
  if (!pathElements.revisionSpec.value.trim()) {
    showPathError("请填写要查询的 SVN 版本号。", pathElements.revisionSpec);
    return false;
  }
  const count = countRevisionSpec(pathElements.revisionSpec.value);
  if (!count) {
    showPathError("版本号只能包含数字、逗号、空格和连字符。", pathElements.revisionSpec);
    return false;
  }
  if (count > PATH_MAX_REVISIONS) {
    showPathError(`单次最多查询 ${PATH_MAX_REVISIONS} 个版本，请缩小版本范围。`, pathElements.revisionSpec);
    return false;
  }
  return true;
}

function applyPathResult(data, buildHint) {
  pathElements.output.value = data.text;
  pathState.sort = data.sort;
  updatePathSummary(data.stats);
  renderPathMessages(data.errors || []);
  pathElements.resultHint.textContent = buildHint(data);
  setPathResultAvailability();
}

async function runPathQuery(event) {
  event.preventDefault();
  if (!validatePathForm()) return;
  const revision = (pathState.revision += 1);
  setButtonBusy(pathElements.queryButton, true, "正在查询…");
  pathElements.sortButton.disabled = true;
  setPathBadge("active", "正在查询 SVN");
  try {
    const data = await postJson("/api/v1/revision-paths/query", {
      svn_url: pathElements.svnUrl.value.trim(),
      revision_spec: pathElements.revisionSpec.value,
      sort: pathElements.sortMode.value,
      use_host_cache: pathElements.useHostCache.checked,
    });
    if (revision !== pathState.revision) return;
    applyPathResult(data, (result) => (
      `共 ${result.stats.file_count} 个文件，命中 ${result.stats.matched_revisions.length} / `
      + `${result.stats.revision_count} 个版本（${PATH_SORT_LABELS[result.sort]}，`
      + `${result.auth_mode === "host-cache" ? "本机缓存认证" : "我的 SVN 账号"}）。`
    ));
    if (!data.stats.file_count) {
      setPathBadge("warning", "无变更文件");
      showNotice("这些版本没有查询到变更文件。", "warning");
    } else if (data.stats.error_count) {
      setPathBadge("warning", "已完成，有提示");
      showNotice(`已生成 ${data.stats.file_count} 个文件路径，另有 ${data.stats.error_count} 条提示。`, "warning");
    } else {
      setPathBadge("success", "查询完成");
      showNotice(`已生成 ${data.stats.file_count} 个文件路径。`, "success");
    }
  } catch (error) {
    if (revision !== pathState.revision) return;
    setPathBadge("error", "查询失败");
    showPathError(error.message);
    showNotice(error.message, "error");
  } finally {
    setButtonBusy(pathElements.queryButton, false);
    pathElements.sortButton.disabled = false;
  }
}

async function runPathSort() {
  if (!pathElements.output.value.trim()) {
    showPathError("请先查询，或在下方粘贴已有的 (V版本) 路径。", pathElements.output);
    return;
  }
  const revision = (pathState.revision += 1);
  clearPathErrors();
  setButtonBusy(pathElements.sortButton, true, "正在排序…");
  try {
    const data = await postJson("/api/v1/revision-paths/sort", {
      text: pathElements.output.value,
      sort: pathElements.sortMode.value,
    });
    if (revision !== pathState.revision) return;
    applyPathResult(data, (result) => (
      `已本地排序 ${result.stats.file_count} 条路径（${PATH_SORT_LABELS[result.sort]}），未访问 SVN。`
    ));
    setPathBadge("success", "排序完成");
    showNotice(`已按${PATH_SORT_LABELS[data.sort]}重排 ${data.stats.file_count} 条路径。`, "success");
  } catch (error) {
    if (revision !== pathState.revision) return;
    showPathError(error.message);
    showNotice(error.message, "error");
  } finally {
    setButtonBusy(pathElements.sortButton, false);
  }
}

async function copyPathResult() {
  const text = pathElements.output.value;
  if (!text.trim()) return;
  try {
    await navigator.clipboard.writeText(text);
    showNotice("文件路径已复制到剪贴板。", "success");
  } catch (_error) {
    pathElements.output.select();
    const copied = document.execCommand("copy");
    showNotice(copied ? "文件路径已复制到剪贴板。" : "复制失败，请手工复制。", copied ? "success" : "error");
  }
}

function downloadPathResult() {
  const text = pathElements.output.value;
  if (!text.trim()) return;
  const filename = pathDownloadFilename();
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showNotice(`已下载 ${filename}。`, "success");
}

pathElements.form.addEventListener("submit", runPathQuery);
pathElements.form.addEventListener("reset", () => {
  pathState.revision += 1;
  window.setTimeout(() => {
    clearPathErrors();
    pathElements.output.value = "";
    pathElements.sortMode.value = "rev";
    pathState.sort = "rev";
    updatePathRevisionCount();
    updatePathSummary();
    renderPathMessages();
    setPathResultAvailability();
    setPathBadge("idle", "尚未查询");
    pathElements.resultHint.textContent = "填写仓库地址与版本号后开始查询。";
  }, 0);
});
pathElements.revisionSpec.addEventListener("input", () => {
  clearPathErrors();
  updatePathRevisionCount();
});
pathElements.revisionSpec.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    pathElements.form.requestSubmit();
  }
});
pathElements.output.addEventListener("input", setPathResultAvailability);
pathElements.sortButton.addEventListener("click", runPathSort);
pathElements.copyButton.addEventListener("click", copyPathResult);
pathElements.downloadButton.addEventListener("click", downloadPathResult);

selectTool(toolFromHash(window.location.hash));
updateStandardCounters();
loadStandardProfiles();
restoreStandardTaskSession();
updatePathRevisionCount();

/* ── 账号与会话 ───────────────────────────────────────────────
   登录门只是界面引导：服务端对每个接口都独立校验会话，绕过这层
   拿不到任何数据。 */

const authState = { user: null };

const authElements = {
  gate: document.querySelector("#authGate"),
  error: document.querySelector("#authError"),
  tabLogin: document.querySelector("#tabLogin"),
  tabRegister: document.querySelector("#tabRegister"),
  loginForm: document.querySelector("#loginForm"),
  loginUsername: document.querySelector("#loginUsername"),
  loginPassword: document.querySelector("#loginPassword"),
  loginSubmit: document.querySelector("#loginSubmit"),
  registerForm: document.querySelector("#registerForm"),
  registerUsername: document.querySelector("#registerUsername"),
  registerDisplayName: document.querySelector("#registerDisplayName"),
  registerPassword: document.querySelector("#registerPassword"),
  registerSubmit: document.querySelector("#registerSubmit"),
  accountArea: document.querySelector("#accountArea"),
  accountName: document.querySelector("#accountName"),
  logoutButton: document.querySelector("#logoutButton"),
  svnButton: document.querySelector("#svnAccountButton"),
};

const svnDialogElements = {
  dialog: document.querySelector("#svnDialog"),
  error: document.querySelector("#svnDialogError"),
  status: document.querySelector("#svnDialogStatus"),
  form: document.querySelector("#svnCredentialForm"),
  username: document.querySelector("#svnAccountUsername"),
  password: document.querySelector("#svnAccountPassword"),
  saveButton: document.querySelector("#svnSaveButton"),
  clearButton: document.querySelector("#svnClearButton"),
  closeButton: document.querySelector("#svnCloseButton"),
};

function showAuthError(message, control) {
  authElements.error.textContent = message;
  authElements.error.hidden = false;
  if (control) control.focus(); else authElements.error.focus();
}

function clearAuthError() {
  authElements.error.hidden = true;
  authElements.error.textContent = "";
}

function selectAuthTab(which) {
  const login = which === "login";
  authElements.tabLogin.setAttribute("aria-selected", String(login));
  authElements.tabRegister.setAttribute("aria-selected", String(!login));
  authElements.loginForm.hidden = !login;
  authElements.registerForm.hidden = login;
  clearAuthError();
  (login ? authElements.loginUsername : authElements.registerUsername).focus();
}

function applyUser(user) {
  authState.user = user;
  const signedIn = Boolean(user);
  authElements.gate.hidden = signedIn;
  authElements.accountArea.hidden = !signedIn;
  document.body.classList.toggle("is-gated", !signedIn);
  if (signedIn) {
    authElements.accountName.textContent = user.display_name || user.username;
    authElements.svnButton.textContent = user.has_svn_credentials
      ? "我的 SVN 账号" : "我的 SVN 账号 · 待设置";
    authElements.svnButton.classList.toggle("needs-attention", !user.has_svn_credentials);
  } else {
    selectAuthTab("login");
  }
}

async function refreshSession() {
  try {
    const response = await fetch("/api/v1/auth/me", { headers: { Accept: "application/json" } });
    const data = await response.json();
    applyUser(data.authenticated ? data.user : null);
  } catch (_error) {
    applyUser(null);
  }
}

async function submitLogin(event) {
  event.preventDefault();
  clearAuthError();
  if (!authElements.loginUsername.value.trim()) {
    showAuthError("请填写账号。", authElements.loginUsername);
    return;
  }
  setButtonBusy(authElements.loginSubmit, true, "登录中…");
  try {
    const data = await postJson("/api/v1/auth/login", {
      username: authElements.loginUsername.value,
      password: authElements.loginPassword.value,
    });
    authElements.loginPassword.value = "";
    applyUser(data.user);
    showNotice(`欢迎回来，${data.user.display_name}。`, "success");
    if (!data.user.has_svn_credentials) openSvnDialog();
  } catch (error) {
    showAuthError(error.message, authElements.loginPassword);
  } finally {
    setButtonBusy(authElements.loginSubmit, false);
  }
}

async function submitRegister(event) {
  event.preventDefault();
  clearAuthError();
  if (authElements.registerPassword.value.length < 8) {
    showAuthError("登录密码至少 8 位。", authElements.registerPassword);
    return;
  }
  setButtonBusy(authElements.registerSubmit, true, "注册中…");
  try {
    await postJson("/api/v1/auth/register", {
      username: authElements.registerUsername.value,
      password: authElements.registerPassword.value,
      display_name: authElements.registerDisplayName.value,
    });
    // 注册接口不建会话，随即用同一组凭据登录，省去二次输入。
    const data = await postJson("/api/v1/auth/login", {
      username: authElements.registerUsername.value,
      password: authElements.registerPassword.value,
    });
    authElements.registerPassword.value = "";
    applyUser(data.user);
    showNotice("注册成功，请先设置你的 SVN 账号。", "success");
    openSvnDialog();
  } catch (error) {
    showAuthError(error.message, authElements.registerUsername);
  } finally {
    setButtonBusy(authElements.registerSubmit, false);
  }
}

async function submitLogout() {
  try {
    await postJson("/api/v1/auth/logout", {});
  } catch (_error) {
    // 登出失败也按已登出处理：本地状态清掉，重新拉一次会话即可。
  }
  applyUser(null);
  showNotice("已退出登录。", "success");
}

function openSvnDialog() {
  svnDialogElements.error.hidden = true;
  svnDialogElements.dialog.hidden = false;
  const user = authState.user;
  svnDialogElements.username.value = user?.svn_username || "";
  svnDialogElements.password.value = "";
  svnDialogElements.status.textContent = user?.has_svn_credentials
    ? `已保存 SVN 账号：${user.svn_username}` : "尚未保存 SVN 账号。";
  svnDialogElements.username.focus();
}

function closeSvnDialog() {
  svnDialogElements.dialog.hidden = true;
  svnDialogElements.password.value = "";
}

async function submitSvnCredentials(event) {
  event.preventDefault();
  svnDialogElements.error.hidden = true;
  if (!svnDialogElements.username.value.trim() || !svnDialogElements.password.value) {
    svnDialogElements.error.textContent = "SVN 账号和密码都需要填写。";
    svnDialogElements.error.hidden = false;
    return;
  }
  setButtonBusy(svnDialogElements.saveButton, true, "保存中…");
  try {
    const data = await requestJson("PUT", "/api/v1/auth/svn-credentials", {
      svn_username: svnDialogElements.username.value,
      svn_password: svnDialogElements.password.value,
    });
    applyUser(data.user);
    closeSvnDialog();
    showNotice("SVN 账号已保存。", "success");
  } catch (error) {
    svnDialogElements.error.textContent = error.message;
    svnDialogElements.error.hidden = false;
  } finally {
    setButtonBusy(svnDialogElements.saveButton, false);
  }
}

async function clearSvnCredentials() {
  if (!window.confirm("确定清除已保存的 SVN 账号密码？之后需要重新填写才能查询或提交。")) return;
  try {
    const data = await requestJson("DELETE", "/api/v1/auth/svn-credentials", null);
    applyUser(data.user);
    openSvnDialog();
    showNotice("已清除保存的 SVN 账号。", "success");
  } catch (error) {
    svnDialogElements.error.textContent = error.message;
    svnDialogElements.error.hidden = false;
  }
}

authElements.tabLogin.addEventListener("click", () => selectAuthTab("login"));
authElements.tabRegister.addEventListener("click", () => selectAuthTab("register"));
authElements.loginForm.addEventListener("submit", submitLogin);
authElements.registerForm.addEventListener("submit", submitRegister);
authElements.logoutButton.addEventListener("click", submitLogout);
authElements.svnButton.addEventListener("click", openSvnDialog);
svnDialogElements.form.addEventListener("submit", submitSvnCredentials);
svnDialogElements.clearButton.addEventListener("click", clearSvnCredentials);
svnDialogElements.closeButton.addEventListener("click", closeSvnDialog);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !svnDialogElements.dialog.hidden) closeSvnDialog();
});

refreshSession();
