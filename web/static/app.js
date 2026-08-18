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
  stages: Array.from(document.querySelectorAll(".flow-step")),
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

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data;
  try {
    data = await response.json();
  } catch (_error) {
    throw new Error("服务返回了无法识别的响应");
  }
  if (!response.ok || !data.ok) {
    throw new Error(data?.error?.message || "处理失败，请稍后重试");
  }
  return data;
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
