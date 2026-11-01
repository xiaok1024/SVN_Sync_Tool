// node tests/test_web_task_display.js；无需 npm 依赖。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../web/static/app.js"), "utf8");
const stages = Array.from({ length: 4 }, () => {
  const classes = new Set();
  const iconClasses = new Set();
  const attributes = new Map();
  const icon = {
    classList: { toggle(name, on) { on ? iconClasses.add(name) : iconClasses.delete(name); } },
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
  };
  return {
    classes,
    iconClasses,
    attributes,
    classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); } },
    querySelector(selector) { return selector === ".step-index" ? icon : null; },
  };
});
let detail = "";
const context = vm.createContext({
  options: { stageOrder: ["form", "checkout", "preview", "commit", "cleanup"], showCleanup: true },
  standardElements: {
    stages, taskReference: { hidden: true, textContent: "" },
    dialog: { open: false }, progressBar: { style: {} },
  },
  setStandardStatus(_title, text) { detail = text; },
  renderStandardEvents() {}, renderStandardResult() {}, renderStandardPreview() {},
});
vm.runInContext(source.slice(source.indexOf("function setStandardStage("),
                            source.indexOf("function setStandardStatus(")), context);
vm.runInContext(source.slice(source.indexOf("function renderStandardTask("),
                            source.indexOf("function openStandardCommitDialog(")), context);
vm.runInContext(source.slice(
  source.indexOf("function extractUncCustomerPath("),
  source.indexOf("/** 粘贴升级清单时自动填写客户来源目录", source.indexOf("function extractUncCustomerPath(")),
), context);
const manifest = String.raw`标准文件请到\\192.168.7.215\ECOLOGY_customer\J\J江苏美思德化学股份\QC5081286\ecology下面提取`;
assert.equal(
  context.extractUncCustomerPath(manifest),
  String.raw`\\192.168.7.215\ECOLOGY_customer\J\J江苏美思德化学股份\QC5081286\ecology`,
);
assert.equal(context.extractUncCustomerPath("https://192.168.0.71/svn/J客户/ecology"), "");
context.task = { id: "a".repeat(32), status: "committed", progress: 100, cleanup: { status: "cleaned" } };
vm.runInContext("renderStandardTask(task)", context);
assert(stages.every(item => item.classes.has("is-complete") && !item.classes.has("is-active")));
assert.equal(detail, "任务已结束，服务器临时文件已清理。");
assert(context.standardElements.taskReference.textContent.includes(context.task.id));
context.task.cleanup.status = "failed";
vm.runInContext("renderStandardTask(task)", context);
assert(stages.every(item => item.classes.has("is-complete") && !item.classes.has("is-active")));
assert.equal(detail, "任务已结束，临时文件清理失败，后台会重试。");
context.task.status = "committing";
vm.runInContext("renderStandardTask(task)", context);
assert(stages[3].classes.has("is-active"));
assert(!stages[3].iconClasses.has("is-back"));
assert.equal(detail, "任务正在服务器上执行。");
// 合并页没有收尾阶段：committed 直接视为结束，不进入 cleanup。
context.options.showCleanup = false;
context.task.status = "committed";
vm.runInContext("renderStandardTask(task)", context);
assert(stages.every(item => item.classes.has("is-complete") && !item.classes.has("is-active")));
assert.equal(detail, "任务已结束。");
assert(stages.every(item => item.iconClasses.has("is-back")));
assert(stages.every(item => item.attributes.get("title") === "返回任务配置"));
console.log("Task display: auto customer path, completed, cleanup failure, active submission and no-cleanup mode passed");
