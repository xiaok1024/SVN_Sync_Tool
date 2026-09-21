// node tests/test_web_task_display.js；无需 npm 依赖。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../web/static/app.js"), "utf8");
const stages = Array.from({ length: 5 }, () => {
  const classes = new Set();
  return { classes, classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); } } };
});
let detail = "";
const context = vm.createContext({
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
context.task = { id: "a".repeat(32), status: "committed", progress: 100, cleanup: { status: "cleaned" } };
vm.runInContext("renderStandardTask(task)", context);
assert(stages.every(item => item.classes.has("is-complete") && !item.classes.has("is-active")));
assert.equal(detail, "任务已结束，服务器临时文件已清理。");
assert(context.standardElements.taskReference.textContent.includes(context.task.id));
context.task.cleanup.status = "failed";
vm.runInContext("renderStandardTask(task)", context);
assert(stages[4].classes.has("is-active"));
assert(detail.includes("清理失败"));
context.task.status = "committing";
vm.runInContext("renderStandardTask(task)", context);
assert(stages[3].classes.has("is-active"));
assert.equal(detail, "任务正在服务器上执行。");
console.log("Task display: completed, cleanup failure and active submission passed");
