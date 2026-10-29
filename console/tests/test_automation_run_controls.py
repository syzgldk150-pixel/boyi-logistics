import shutil
import subprocess
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class AutomationRunControlsTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.template = cls.env.get_template("automation.html")

    def _render(self, task: dict) -> str:
        return self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            settings={},
            scheduled_tasks=[task],
            scheduled_task_count=1,
            enabled_task_count=0,
            automation_db_warning="",
            automation_account_warning="",
            automation_approval_policy_warning="",
            automation_plugin_warning="",
            automation_plugin_packages=[],
            unsupported_automation_ids=[],
            can_manage_plugins=True,
            can_manage_approval_policies=True,
        )

    def test_template_renders_single_run_toggle_control(self):
        html = self._render(
            {
                "task_id": "demo_task",
                "task_mode": "manual",
                "name_value": "测试任务",
                "tool_name_value": "demo_tool",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "测试任务 demo_task demo_tool",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": False,
                "schedule_supported": False,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
            }
        )

        self.assertIn('data-run-now', html)
        self.assertIn('data-run-icon', html)
        self.assertIn('data-run-label', html)
        self.assertIn('data-run-mode="start"', html)
        self.assertNotIn('data-run-cancel', html)
        self.assertIn("/automations/tasks/cancel", html)

    def test_exact_scan_project_renders_inline_preview_confirmation(self):
        html = self._render(
            {
                "task_id": "scan_codes",
                "task_mode": "manual",
                "name_value": "扫描",
                "tool_name_value": "automation.scan_codes.run",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "扫描 scan_codes",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": False,
                "schedule_supported": False,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "resource_bindings": [],
                "account_role_bindings": [],
                "plugin": {
                    "plugin_id": "sync_scan_codes_v2",
                    "runtime_model": "SERVICE_V2",
                    "record_version": 1,
                    "project_configuration_version": 1,
                    "version": "1.0.22",
                    "state": "ENABLED",
                    "status_label": "已启用",
                    "platform_label": "服务器",
                    "execution_platform": "server",
                    "can_schedule": False,
                    "enabled": True,
                    "lifecycle_actions_allowed": False,
                    "config_fields": [],
                    "resource_roles": [],
                    "entrypoints": [],
                },
            }
        )

        self.assertIn('data-scan-preview-workflow="true"', html)
        self.assertIn("生成预览", html)
        self.assertIn("data-scan-preview-panel", html)
        self.assertIn("data-scan-preview-confirm", html)
        self.assertIn("data-scan-preview-regenerate", html)
        self.assertIn("待扫描", html)
        self.assertNotIn("selection_sha256", html)
        self.assertNotIn("source_evidence_refs", html)

    def test_scheduled_task_without_schedule_still_renders_toggle_and_settings(self):
        html = self._render(
            {
                "task_id": "arrive_list",
                "task_mode": "scheduled",
                "name_value": "arrive-list",
                "tool_name_value": "sync_arrive_list",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "arrive-list sync_arrive_list",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": True,
                "schedule_supported": True,
                "schedule_editable": True,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
            }
        )

        self.assertIn('data-automation-toggle', html)
        self.assertIn('data-settings-toggle', html)
        self.assertIn('data-schedule-stack', html)
        self.assertNotIn('data-code-toggle', html)

    def test_implicit_form_submit_neither_saves_nor_runs(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        guard = source.split('form.addEventListener("submit", event => {', 1)[1].split("});", 1)[0]
        self.assertIn('if (!event.submitter?.getAttribute("formaction"))', guard)
        self.assertIn("event.preventDefault();", guard)
        self.assertNotIn("/automations/tasks/save", source)
        self.assertIn("function closeAutomationTaskPanels(form)", source)
        self.assertNotIn("data-code-toggle", source)
        self.assertNotIn("data-json-editor", source)

    def test_terminal_output_polling_uses_ajax_json_headers(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        terminal_fetch_index = source.index("fetch(`/automations/tasks/output?")
        terminal_fetch_block = source[terminal_fetch_index : terminal_fetch_index + 400]
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', terminal_fetch_block)
        self.assertIn('"Accept": "application/json"', terminal_fetch_block)

    def test_terminal_output_polling_retries_after_transient_failure(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        catch_index = source.index('termStatus.textContent = "状态读取失败";')
        catch_block = source[catch_index : catch_index + 500]
        self.assertIn("setTimeout(pollOutput, document.hidden ? 5000 : 3000)", catch_block)
        self.assertIn("if (trackingRun)", catch_block)

    def test_terminal_poll_lifecycle_executes_in_node(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )
        node = shutil.which("node") or shutil.which("node.exe")
        if node is None:
            self.skipTest("Node.js is unavailable")
        script = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync(0, "utf8");
const controls = source.slice(source.indexOf("function syncRunButtonVisual()"), source.indexOf("    form.addEventListener(\"submit\", event => {"));
const polling = source.slice(source.indexOf("let resetTerminalOutput = function() {};"), source.indexOf("    // 反馈关闭按钮"))
  .replace("// Invocation history initialization is complete.", "globalThis.pollTest = {startPolling, pollOutput};");
const submit = source.slice(source.indexOf('    if (runBtn) {\n      runBtn.addEventListener("click"'),
  source.indexOf('\n  });\n\n  /* ── 列表排序'));

async function exercise(failure, conflict = false) {
  const timers = new Map();
  const intervals = new Map();
  const responses = [];
  const requests = [];
  const feedback = [];
  const handlers = {};
  let timerId = 0;
  let runtime = null;
  let feedbackHidden = false;
  class HTMLElement {}
  const runLabel = {textContent: ""};
  const runBtn = Object.assign(new HTMLElement(), {
    dataset: {}, classList: {toggle() {}}, hasAttribute: () => false,
    querySelector: selector => selector === "[data-run-label]" ? runLabel : null,
    getAttribute: () => "/automations/tasks/run-now",
    addEventListener: (_name, callback) => {handlers.runClick = callback;},
  });
  const termStatus = {textContent: "", className: ""};
  const termBody = {textContent: "", scrollTop: 0, scrollHeight: 0};
  const termBtn = {
    expanded: "false",
    getAttribute() { return this.expanded; },
    addEventListener(name, callback) { handlers[name] = callback; },
  };
  const context = {
    URLSearchParams, AbortController, HTMLElement, runBtn, termBtn,
    FormData: class { forEach(callback) {callback("daily_sign", "task_id");} },
    termDrawer: {
      dataset: {toolName: "automation.daily_sign.run", taskId: "daily_sign", invocationId: conflict ? "historical-run" : "accepted-run"},
      querySelector: selector => ({"[data-terminal-body]": termBody, "[data-terminal-status]": termStatus}[selector] || null),
    },
    form: {querySelector: () => null}, document: {hidden: false}, feather: {replace() {}},
    isScanPreviewWorkflow: false, isSelectionPreviewWorkflow: false,
    scanConfirmationRetryOnly: false, scanPreviewTerminalBlocked: false,
    selectionConfirmationRetryOnly: false, termAttentionLatched: false,
    runTimer: null, fetchOutputState: null,
    runUiState: {running: false, awaitingApproval: false, pendingRun: !conflict, invocationId: conflict ? "" : "accepted-run"},
    renderFeedback: (_form, value) => {feedback.push(value); feedbackHidden = false;},
    syncRuntimeFeedback: (_form, value) => {runtime = value;},
    hideRunFeedback: () => {feedbackHidden = true;},
    hideLegacyFeedback() {}, serializeForm() {}, setButtonLoading() {},
    fmtStamp: () => "2026-09-08T01:36:04Z", crypto: {randomUUID: () => "browser-request"},
    requestTaskCancel: () => {throw new Error("existing Run must never be cancelled implicitly");},
    location: {reload: () => {context.reloadCount = (context.reloadCount || 0) + 1;}},
    togglePanel: button => {button.expanded = button.expanded === "true" ? "false" : "true";},
    fetch: async (url, options = {}) => {
      requests.push({url, method: options.method || "GET"});
      assert.ok(responses.length, "unexpected extra status request");
      return responses.shift();
    },
    setTimeout: (callback, delay) => {const id = ++timerId; timers.set(id, {callback, delay}); return id;},
    clearTimeout: id => timers.delete(id),
    setInterval: callback => {const id = ++timerId; intervals.set(id, callback); return id;},
    clearInterval: id => intervals.delete(id),
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(controls + polling + submit, context);
  const queue = {
    invocation_id: "accepted-run", automation_id: "daily_sign", status: "RECEIVED", pending: true, queued: true, running: false,
    started_at: "2026-09-08T01:36:04Z", lines: [], total: 0,
    stage_description: "任务已受理，等待开始执行", next_poll_after_ms: 1000,
  };
  const response = (payload, ok = true) => ({ok, json: async () => payload});
  const flush = () => new Promise(resolve => setImmediate(resolve));
  async function advance() {
    assert.equal(timers.size, 1, "only the existing task poll may remain scheduled");
    const [id, task] = [...timers.entries()][0];
    timers.delete(id);
    await task.callback();
    await flush();
  }

  const missingIdentity = String(conflict).includes("missing");
  const staleRequest = String(conflict).includes("stale");
  let resolveHistorical;
  if (staleRequest) {
    responses.push({ok: true, json: () => new Promise(resolve => {resolveHistorical = resolve;})});
    handlers.click();
    await flush();
    assert.equal(requests.length, 1);
    assert.match(requests[0].url, /invocation_id=historical-run/);
  }
  if (conflict) {
    responses.push(response({ok: false, pending: false, error_code: "AUTOMATION_ALREADY_RUNNING",
      title: "正在执行", message: "已有任务", existing_run: missingIdentity ? null : {
        invocation_id: "accepted-run", status: "RECEIVED", blocking_kind: "ACTIVE"}}, false));
    if (!missingIdentity) responses.push(response(queue));
    await handlers.runClick({preventDefault() {}});
  } else {
    responses.push(response(queue));
    context.pollTest.startPolling();
  }
  await flush();
  if (staleRequest) {
    assert.equal(runBtn.dataset.runMode, "refresh");
    assert.equal(context.runUiState.invocationId, missingIdentity ? "" : "accepted-run");
    resolveHistorical({...queue, invocation_id: "historical-run", status: "COMPLETED",
      pending: false, queued: false, runtime: {ok: true, title: "历史完成"}});
    await flush();
    assert.equal(runBtn.dataset.runMode, "refresh", "old terminal output must not restore submit");
    assert.equal(context.runUiState.invocationId, missingIdentity ? "" : "accepted-run");
    assert.equal(runtime, null, "old runtime must not replace the new tracking state");
    handlers.click(); // Close the historical output drawer before the remaining lifecycle.
  }
  const oldRequests = staleRequest ? 1 : 0;
  if (missingIdentity) {
    assert.equal(context.runUiState.invocationId, "");
    assert.equal(runBtn.dataset.runMode, "refresh");
    assert.equal(runLabel.textContent, "刷新状态");
    assert.match(feedback.at(-1).message, /刷新/);
    assert.equal(requests.length, 1 + oldRequests, "missing identity must not query old tool output");
    const warning = feedback.at(-1);
    handlers.click(); // Opening output must not fall back to a generic tool-output GET.
    await flush();
    assert.equal(requests.length, 1 + oldRequests);
    assert.equal(feedback.at(-1), warning);
    await handlers.runClick({preventDefault() {}});
    assert.equal(context.reloadCount, 1);
    assert.equal(requests.length, 1 + oldRequests, "refresh cannot resubmit or cancel");
    return;
  }
  assert.equal(feedback.at(-1).title, "等待执行");
  assert.equal(feedback.at(-1).message, queue.stage_description);
  assert.equal(timers.size, 1);

  let resolveQueued;
  responses.push({ok: true, json: () => new Promise(resolve => {resolveQueued = resolve;})});
  handlers.click(); // Open the output drawer using its actual event handler.
  await flush();
  assert.equal(termBtn.expanded, "true");
  handlers.click(); // Closing the drawer must not stop accepted Run tracking.
  handlers.click(); // Reopening during the same request must not create another poll.
  assert.equal(requests.length, (conflict ? 3 : 2) + oldRequests);
  handlers.click();
  resolveQueued(queue);
  await flush();
  assert.equal(termBtn.expanded, "false");
  assert.equal(timers.size, 1);

  responses.push(failure(response));
  if (conflict) {
    await handlers.runClick({preventDefault() {}}); // Explicit refresh only reads the exact Run.
    await flush();
  } else {
    await advance();
  }
  assert.equal(feedback.at(-1).title, "状态暂不可用");
  assert.equal(termStatus.textContent, "状态读取失败");
  assert.equal(context.runUiState.invocationId, "accepted-run");
  assert.equal(context.runUiState.pendingRun, true);
  assert.equal(runBtn.dataset.runMode, conflict ? "refresh" : "cancel", "failed polling must not expose another submit action");
  assert.equal([...timers.values()][0].delay, 3000);

  responses.push(response({...queue, status: "RUNNING", queued: false, running: true,
    execution_phase: "source_read", stage_description: "正在读取数据"}));
  await advance();
  assert.equal(termBtn.expanded, "false");
  assert.equal(feedback.at(-1).message, "正在读取数据");
  assert.equal(context.runUiState.invocationId, "accepted-run");

  responses.push(response({...queue, status: "COMPLETED", pending: false, queued: false,
    next_poll_after_ms: 0, runtime: {ok: true, title: "已完成"}}));
  await advance();
  assert.equal(runtime.ok, true);
  assert.equal(feedbackHidden, true);
  assert.equal(termStatus.textContent, "已完成");
  assert.equal(timers.size, 0, "a terminal Run must stop polling");
  assert.equal(intervals.size, 0, "a terminal Run must stop its progress timer");
  assert.equal(runBtn.dataset.runMode, "start");
  assert.ok(requests.slice(oldRequests).filter(request => request.method === "GET").every(request =>
    new URL(request.url, "https://console.invalid").searchParams.get("invocation_id") === "accepted-run"));
  assert.equal(requests.filter(request => request.method === "POST").length, conflict ? 1 : 0);
  if (!conflict) {
    // A new submission must not inherit the previous attempt's error/output.
    termStatus.textContent = "状态读取失败";
    termStatus.className = "auto-terminal-status auto-terminal-status--error";
    termBody.textContent = "previous output";
    let resolveSubmit, resolveFreshStatus;
    responses.push(new Promise(resolve => {resolveSubmit = resolve;}));
    responses.push({ok: true, json: () => new Promise(resolve => {resolveFreshStatus = resolve;})});
    const submission = handlers.runClick({preventDefault() {}});
    assert.equal(termStatus.textContent, "正在提交");
    assert.equal(termStatus.className, "auto-terminal-status");
    assert.equal(termBody.textContent, "");
    resolveSubmit(response({ok: true, pending: true, invocation_id: "fresh-run"}));
    await submission;
    await flush();
    assert.equal(termStatus.textContent, "读取状态中");
    assert.equal(termStatus.className, "auto-terminal-status");
    assert.equal(context.runUiState.invocationId, "fresh-run");
    resolveFreshStatus({...queue, invocation_id: "fresh-run", running: true, queued: false});
    await flush();
    assert.equal(termStatus.textContent, "运行中");
  }
}
(async () => {
  await exercise(response => response({running: false, error: "unavailable"}, false));
  await exercise(response => response({error: "unavailable"}));
  await exercise(() => ({ok: true, json: async () => {throw new SyntaxError("invalid response");}}));
  await exercise(response => response({error: "unavailable"}, false), "existing");
  await exercise(null, "missing");
  await exercise(response => response({error: "unavailable"}, false), "stale-existing");
  await exercise(null, "stale-missing");
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(
            [node, "--input-type=commonjs", "-e", script], input=source,
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_submit_recovery_never_infers_running_from_historical_output(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        recovery_start = source.index("let outputState = null;")
        recovery_end = source.index("} finally {", recovery_start)
        recovery_block = source[recovery_start:recovery_end]
        self.assertIn("if (outputState?.running)", recovery_block)
        self.assertIn("renderPendingRun(outputState)", recovery_block)
        self.assertNotIn("(outputState.total || 0) > 0", recovery_block)

    def test_terminal_output_polling_has_browser_side_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        terminal_fetch_index = source.index("fetch(`/automations/tasks/output?")
        terminal_fetch_end = source.index("function renderRunSourceLinks", terminal_fetch_index)
        terminal_fetch_block = source[terminal_fetch_index - 300 : terminal_fetch_end]
        self.assertIn("AbortController", terminal_fetch_block)
        self.assertIn("signal: controller.signal", terminal_fetch_block)
        self.assertIn("clearTimeout(timeoutId)", terminal_fetch_block)

    def test_run_feedback_hides_internal_invocation_ids_and_technical_statuses(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        attention_start = source.index("function renderAttentionRun(data)")
        attention_block = source[attention_start : attention_start + 1800]
        self.assertNotIn("Run：${invocationId}", attention_block)
        self.assertNotIn("状态：${status}", attention_block)
        self.assertIn('body: ""', attention_block)
        self.assertIn("执行前检查未通过", attention_block)

    def test_scan_confirmation_posts_each_current_instance_in_node(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(encoding="utf-8")
        node = shutil.which("node") or shutil.which("node.exe")
        if node is None:
            self.skipTest("Node.js is unavailable")
        script = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync(0, "utf8");
const handler = source.slice(source.indexOf('    if (scanPreviewConfirm) {\n      scanPreviewConfirm.addEventListener("click"'),
  source.indexOf("    if (selectionPreviewDismiss) {"));
async function exercise(taskId) {
  const requests = [];
  let click, generated = 0, accepted;
  const context = {
    URLSearchParams,
    activeScanPreviewId: "57c866fd-1927-4d9f-ba83-46a01c79dc71",
    activeScanConfirmationRequestId: "",
    scanPreviewConfirm: {hasAttribute: () => false, addEventListener: (_, fn) => {click = fn;}},
    form: {querySelector: selector => {
      assert.match(selector, /input\[name=['"]task_id['"]\]/);
      return {value: taskId};
    }},
    window: {crypto: {randomUUID: () => {generated += 1; return "c4b6f842-1b98-448e-ac1a-f8065c875cb5";}}},
    setButtonLoading() {}, setScanConfirmationRetryOnly() {}, renderFeedback() {},
    renderScanPreviewError() {}, clearScanPreview() {}, startPolling() {},
    setRunUiState: state => {accepted = state;}, termDrawer: {dataset: {}},
    fetch: async (url, options) => {
      requests.push({url, options});
      return requests.length === 1
        ? {ok: false, status: 503, json: async () => ({ok: false})}
        : {ok: true, status: 202, json: async () => ({ok: true, invocation_id: "accepted-call"})};
    },
  };
  vm.runInNewContext(handler, context);
  await click();
  await click();
  assert.equal(generated, 1);
  assert.equal(requests.length, 2);
  for (const {url, options} of requests) {
    assert.equal(url, "/automations/tasks/confirm-scan-preview");
    assert.deepEqual(Object.fromEntries(new URLSearchParams(options.body)), {
      task_id: taskId, preview_invocation_id: context.activeScanPreviewId,
    });
    assert.equal(options.headers["X-Browser-Request-UUID"], context.activeScanConfirmationRequestId);
  }
  assert.equal(accepted.invocationId, "accepted-call");
}
(async () => {
  for (const id of ["scan_codes", "9d9f96b5-7b45-4741-837f-92ff9b8ab365", "9d77e873-fb76-41cb-9e84-b1ae7bad619e"]) await exercise(id);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([node, "--input-type=commonjs", "-e", script], input=source, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scan_confirmation_uses_stable_request_uuid_and_only_public_invocation_id(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        confirm_index = source.index(
            'fetch("/automations/tasks/confirm-scan-preview"'
        )
        confirm_block = source[confirm_index - 240 : confirm_index + 700]
        self.assertIn("task_id: taskId", confirm_block)
        self.assertNotIn('task_id: "scan_codes"', confirm_block)
        self.assertIn("preview_invocation_id: previewInvocationId", confirm_block)
        self.assertIn('"X-Browser-Request-UUID": confirmationRequestId', confirm_block)
        self.assertNotIn("dry_run", confirm_block)
        self.assertNotIn("selection_sha256", confirm_block)
        self.assertIn(
            "activeScanConfirmationRequestId || window.crypto.randomUUID()",
            source,
        )
        self.assertIn(
            "activeScanConfirmationRequestId = confirmationRequestId",
            source,
        )
        self.assertIn(
            "页面会复用原请求标识，不会创建第二次正式请求",
            source,
        )
        self.assertIn('"SCAN_PREVIEW_ALREADY_CONSUMED"', source)
        fetch_index = source.index(
            'fetch("/automations/tasks/confirm-scan-preview"'
        )
        confirm_handler_index = source.rindex(
            'scanPreviewConfirm.addEventListener("click"',
            0,
            fetch_index,
        )
        self.assertLess(
            source.index(
                "setScanConfirmationRetryOnly(true)",
                confirm_handler_index,
            ),
            fetch_index,
        )
        self.assertNotIn("if (isScanPreviewWorkflow && scanConfirmationRetryOnly)", source)
        self.assertNotIn("if (isScanPreviewWorkflow && scanPreviewTerminalBlocked)", source)
        self.assertNotIn("|| (isScanPreviewWorkflow && scanConfirmationRetryOnly)", source)
        self.assertNotIn("|| (isScanPreviewWorkflow && scanPreviewTerminalBlocked)", source)
        self.assertIn("if (isScanPreviewWorkflow) clearScanPreview({ force: true });", source)
        self.assertNotIn("当前页面不会生成新的扫描预览", source)
        render_preview_index = source.index("function renderScanPreview(preview)")
        next_preview_index = source.index(
            "const nextPreviewId",
            render_preview_index,
        )
        self.assertLess(
            source.index(
                "if (scanPreviewTerminalBlocked) return;",
                render_preview_index,
            ),
            next_preview_index,
        )
        render_preview_block = source[render_preview_index:next_preview_index]
        self.assertNotIn("setScanPreviewTerminalBlocked(false)", render_preview_block)
        self.assertIn("clearScanPreview({ force: true })", source)
        self.assertIn('termDrawer.dataset.scanPhase = "formal"', source)
        self.assertIn('termDrawer.dataset.scanPhase = "preview"', source)

    def test_blocked_runs_render_attention_and_do_not_keep_polling(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("function renderAttentionRun(data)", source)
        self.assertIn('"执行前检查未通过"', source)
        self.assertIn('"登录已失效"', source)
        self.assertIn("if (data.attention)", source)
        self.assertIn("pendingRun && !data.attention", source)

    def test_retired_approval_batches_cannot_start_or_block_live_invocations(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(encoding="utf-8")
        self.assertNotIn("approvedBatch", source)
        self.assertNotIn("automation:approved-runs", source)
        self.assertNotIn("WAITING_APPROVAL", source)
        self.assertNotIn("awaitingApproval", source)

    def test_attention_latch_survives_poll_and_request_error_recovery(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("let termAttentionLatched = false;", source)
        self.assertIn("termAttentionLatched = true;", source)
        self.assertIn("if (outputState?.attention)", source)
        self.assertIn("if (termAttentionLatched) return;", source)
        self.assertIn("const trackingRun = runUiState.running || runUiState.pendingRun;", source)
        self.assertIn(
            'options.invocationId || termDrawer.dataset.invocationId || runUiState.invocationId || ""',
            source,
        )
        self.assertNotIn("options.invocationId ?? runUiState.invocationId", source)


if __name__ == "__main__":
    unittest.main()
