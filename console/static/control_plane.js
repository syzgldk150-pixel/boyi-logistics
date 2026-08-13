(() => {
  "use strict";

  const STATUS_LABELS = {
    OPEN: "待处理",
    IN_PROGRESS: "处理中",
    WAITING_APPROVAL: "待审批",
    RESOLVED: "已完成",
    CLOSED: "已关闭",
    RECEIVED: "已接收",
    CONTEXT_READY: "上下文就绪",
    PLANNED: "已生成计划",
    VALIDATED: "已校验",
    RUNNING: "执行中",
    VERIFYING: "验证中",
    COMPLETED: "已完成",
    NEEDS_CLARIFICATION: "待补充信息",
    BLOCKED_LOGIN: "登录阻塞",
    BLOCKED_DATA: "数据阻塞",
    PARTIAL: "部分完成",
    FAILED_RETRYABLE: "可重试失败",
    FAILED_TERMINAL: "执行失败",
    CANCELLED: "已取消",
    BLOCKED: "已阻塞",
  };
  const PRIORITY_LABELS = {
    URGENT: "紧急",
    HIGH: "高",
    NORMAL: "普通",
    LOW: "低",
  };
  const RISK_LABELS = {
    LOW: "低风险",
    MEDIUM: "中风险",
    HIGH: "高风险",
    EXTREME: "极高风险（禁用）",
  };
  const TERMINAL_RUN_STATES = new Set([
    "COMPLETED",
    "FAILED_TERMINAL",
    "CANCELLED",
    "PARTIAL",
  ]);
  const SLOW_POLL_STATES = new Set([
    "WAITING_APPROVAL",
    "NEEDS_CLARIFICATION",
    "BLOCKED_LOGIN",
    "BLOCKED_DATA",
    "FAILED_RETRYABLE",
  ]);
  const LIST_QUERY_FIELDS = ["q", "status", "priority", "type", "owner", "sla"];
  const DATE_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  class ControlPlaneApiError extends Error {
    constructor(message, { code = "REQUEST_FAILED", status = 0, data = null } = {}) {
      super(message);
      this.name = "ControlPlaneApiError";
      this.code = code;
      this.status = status;
      this.data = data;
    }
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    headers.set("X-Requested-With", "XMLHttpRequest");
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json; charset=utf-8");
    }
    const response = await fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const raw = await response.text();
    let payload = null;
    try {
      payload = raw ? JSON.parse(raw) : null;
    } catch (_error) {
      throw new ControlPlaneApiError("服务返回了无法读取的响应。", {
        code: "INVALID_CONSOLE_RESPONSE",
        status: response.status,
      });
    }
    if (!response.ok || !payload || payload.ok !== true) {
      const error = payload && payload.error && typeof payload.error === "object"
        ? payload.error
        : {};
      throw new ControlPlaneApiError(
        String(error.message || payload?.message || "控制平面请求失败。"),
        {
          code: String(error.code || payload?.error_code || "REQUEST_FAILED"),
          status: response.status,
          data: payload?.data ?? null,
        },
      );
    }
    return payload.data;
  }

  function createElement(tag, className = "", value = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== "" && (typeof value !== "object" || value === null)) {
      node.textContent = String(value);
    }
    return node;
  }

  function appendText(parent, tag, className, value) {
    const node = createElement(tag, className, value);
    parent.append(node);
    return node;
  }

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function asList(value) {
    return Array.isArray(value) ? value : [];
  }

  function textValue(value, empty = "未提供") {
    if (value === null || value === undefined || value === "") return empty;
    if (typeof value === "object") return empty;
    return String(value);
  }

  function normalizedState(value) {
    return String(value || "").trim().replaceAll("-", "_").toUpperCase();
  }

  function statusLabel(value) {
    const normalized = normalizedState(value);
    return STATUS_LABELS[normalized] || textValue(value, "状态未提供");
  }

  function statusTone(value) {
    const normalized = normalizedState(value);
    if (["COMPLETED", "RESOLVED", "CLOSED"].includes(normalized)) return "complete";
    if (normalized.startsWith("FAILED")) return "failed";
    if (normalized.startsWith("BLOCKED") || normalized === "CANCELLED") return "blocked";
    if (["WAITING_APPROVAL", "NEEDS_CLARIFICATION", "PARTIAL"].includes(normalized)) {
      return "waiting";
    }
    if (["IN_PROGRESS", "RUNNING", "VERIFYING", "PLANNED", "VALIDATED"].includes(normalized)) {
      return "active";
    }
    return "neutral";
  }

  function priorityLabel(value) {
    const normalized = normalizedState(value);
    return PRIORITY_LABELS[normalized] || textValue(value, "未设置");
  }

  function riskLabel(value) {
    const normalized = normalizedState(value);
    return RISK_LABELS[normalized] || textValue(value, "未提供");
  }

  function formatDateTime(value) {
    if (!value) return "未提供";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return DATE_FORMATTER.format(date).replaceAll("/", "-");
  }

  function actorLabel(value) {
    if (typeof value === "string") return textValue(value);
    const actor = asObject(value);
    return textValue(actor.display_name || actor.name || actor.username || actor.actor_id || actor.id);
  }

  function ownerLabel(value) {
    return actorLabel(value);
  }

  function refreshIcons(scope = document) {
    if (window.feather && typeof window.feather.replace === "function") {
      window.feather.replace();
    }
  }

  function showFeedback(node, message, tone = "error") {
    if (!node) return;
    node.textContent = message || "";
    if (message) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  function makeStatus(value) {
    const badge = createElement("span", "cp-status", statusLabel(value));
    badge.dataset.tone = statusTone(value);
    return badge;
  }

  function makeDefinition(term, description) {
    const wrapper = createElement("div");
    appendText(wrapper, "dt", "", term);
    appendText(wrapper, "dd", "", textValue(description));
    return wrapper;
  }

  function safePositiveInteger(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
  }

  function setButtonBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function initListPage(root) {
    const form = root.querySelector("[data-cp-filter-form]");
    const body = root.querySelector("[data-cp-list-body]");
    const tableWrap = root.querySelector("[data-cp-table-wrap]");
    const feedback = root.querySelector("[data-cp-list-feedback]");
    const count = root.querySelector("[data-cp-list-count]");
    const updated = root.querySelector("[data-cp-list-updated]");
    const pagination = root.querySelector("[data-cp-pagination]");
    const pageLabel = root.querySelector("[data-cp-page-label]");
    const previous = root.querySelector("[data-cp-page-previous]");
    const next = root.querySelector("[data-cp-page-next]");
    const refreshButton = document.querySelector("[data-cp-list-refresh]");
    const resetButton = root.querySelector("[data-cp-filter-reset]");
    let currentPage = 1;
    let requestSequence = 0;

    const initial = new URLSearchParams(window.location.search);
    for (const name of LIST_QUERY_FIELDS) {
      const control = form.elements.namedItem(name);
      if (control && initial.has(name)) control.value = initial.get(name) || "";
    }
    currentPage = safePositiveInteger(initial.get("page"), 1);

    function queryForPage(page) {
      const query = new URLSearchParams();
      for (const name of LIST_QUERY_FIELDS) {
        const control = form.elements.namedItem(name);
        const value = String(control?.value || "").trim();
        if (value) query.set(name, value);
      }
      query.set("page", String(page));
      query.set("page_size", "25");
      return query;
    }

    function syncLocation(query) {
      const visible = new URLSearchParams(query);
      if (visible.get("page") === "1") visible.delete("page");
      visible.delete("page_size");
      const suffix = visible.toString();
      history.replaceState(null, "", `${window.location.pathname}${suffix ? `?${suffix}` : ""}`);
    }

    function renderEmpty(message) {
      body.replaceChildren();
      const row = createElement("tr", "cp-empty-row");
      const cell = createElement("td", "", message);
      cell.colSpan = 8;
      row.append(cell);
      body.append(row);
    }

    function renderRows(items) {
      body.replaceChildren();
      if (!items.length) {
        renderEmpty("当前筛选条件下没有事项。可调整条件后重新查询。");
        return;
      }
      const fragment = document.createDocumentFragment();
      for (const itemValue of items) {
        const item = asObject(itemValue);
        const row = createElement("tr");

        const statusCell = createElement("td");
        statusCell.dataset.label = "状态";
        statusCell.append(makeStatus(item.status));
        row.append(statusCell);

        const titleCell = createElement("td");
        titleCell.dataset.label = "事项";
        appendText(titleCell, "span", "cp-item-title", textValue(item.title, "未提供标题"));
        appendText(titleCell, "span", "cp-item-id", textValue(item.work_item_id || item.id));
        row.append(titleCell);

        const typeCell = createElement("td");
        typeCell.dataset.label = "类型 / 来源";
        appendText(typeCell, "span", "", textValue(item.type));
        appendText(typeCell, "span", "cp-cell-note", textValue(item.source));
        row.append(typeCell);

        const priorityCell = createElement("td");
        priorityCell.dataset.label = "优先级";
        const priority = createElement("span", "cp-priority", priorityLabel(item.priority));
        priority.dataset.tone = normalizedState(item.priority).toLowerCase();
        priorityCell.append(priority);
        row.append(priorityCell);

        const ownerCell = createElement("td", "", ownerLabel(item.owner || item.owner_id));
        ownerCell.dataset.label = "责任人";
        row.append(ownerCell);

        const slaCell = createElement("td", "", formatDateTime(item.sla_deadline));
        slaCell.dataset.label = "SLA 截止";
        row.append(slaCell);

        const updatedCell = createElement("td", "", formatDateTime(item.updated_at));
        updatedCell.dataset.label = "更新时间";
        row.append(updatedCell);

        const actionCell = createElement("td");
        actionCell.dataset.label = "操作";
        const itemId = String(item.work_item_id || item.id || "").trim();
        if (itemId) {
          const link = createElement("a", "cp-row-link");
          link.href = `/work-items/${encodeURIComponent(itemId)}`;
          link.setAttribute("aria-label", `查看事项 ${itemId}`);
          link.title = "查看详情";
          const icon = createElement("i");
          icon.dataset.feather = "arrow-right";
          link.append(icon);
          actionCell.append(link);
        }
        row.append(actionCell);
        fragment.append(row);
      }
      body.append(fragment);
      refreshIcons(body);
    }

    function renderPagination(data, requestedPage) {
      const paginationData = asObject(data.pagination);
      const source = Object.keys(paginationData).length ? paginationData : data;
      const page = safePositiveInteger(source.page, requestedPage);
      const pageSize = safePositiveInteger(source.page_size, 0);
      const total = Number(source.total);
      const hasAuthoritativeTotal = Number.isSafeInteger(total) && total >= 0 && pageSize > 0;
      const hasPrevious = typeof source.has_previous === "boolean"
        ? source.has_previous
        : page > 1;
      const hasNext = typeof source.has_next === "boolean"
        ? source.has_next
        : hasAuthoritativeTotal && page * pageSize < total;
      const hasPaginationData = "page" in source
        || "has_previous" in source
        || "has_next" in source
        || hasAuthoritativeTotal;
      pagination.hidden = !hasPaginationData;
      if (!hasPaginationData) return;
      currentPage = page;
      previous.disabled = !hasPrevious;
      next.disabled = !hasNext;
      pageLabel.textContent = hasAuthoritativeTotal
        ? `第 ${page} 页，共 ${Math.max(1, Math.ceil(total / pageSize))} 页`
        : `第 ${page} 页`;
    }

    async function loadList({ page = currentPage, announce = false } = {}) {
      const sequence = ++requestSequence;
      const query = queryForPage(page);
      syncLocation(query);
      tableWrap.setAttribute("aria-busy", "true");
      setButtonBusy(refreshButton, true);
      showFeedback(feedback, "");
      if (announce) count.textContent = "正在刷新事项…";
      try {
        const data = asObject(await request(`/control-plane/work-items?${query.toString()}`));
        if (sequence !== requestSequence) return;
        const items = asList(data.items);
        renderRows(items);
        count.textContent = `当前页 ${items.length} 项`;
        updated.textContent = `更新于 ${DATE_FORMATTER.format(new Date()).replaceAll("/", "-")}`;
        renderPagination(data, page);
      } catch (error) {
        if (sequence !== requestSequence) return;
        renderEmpty("事项读取失败。请稍后重试。 ");
        count.textContent = "未能读取事项";
        showFeedback(feedback, error.message || "事项读取失败。 ");
      } finally {
        if (sequence === requestSequence) {
          tableWrap.setAttribute("aria-busy", "false");
          setButtonBusy(refreshButton, false);
        }
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      currentPage = 1;
      loadList({ page: 1, announce: true });
    });
    resetButton.addEventListener("click", () => {
      form.reset();
      currentPage = 1;
      loadList({ page: 1, announce: true });
    });
    refreshButton?.addEventListener("click", () => loadList({ announce: true }));
    previous.addEventListener("click", () => loadList({ page: Math.max(1, currentPage - 1) }));
    next.addEventListener("click", () => loadList({ page: currentPage + 1 }));
    loadList({ page: currentPage });
  }

  function initDetailPage(root) {
    const workItemId = String(root.dataset.workItemId || "").trim();
    const feedback = root.querySelector("[data-cp-detail-feedback]");
    const header = root.querySelector("[data-cp-detail-header]");
    const statusNode = root.querySelector("[data-cp-detail-status]");
    const idNode = root.querySelector("[data-cp-detail-id]");
    const titleNode = root.querySelector("[data-cp-detail-title]");
    const reasonNode = root.querySelector("[data-cp-detail-reason]");
    const metaNode = root.querySelector("[data-cp-detail-meta]");
    const actionBar = root.querySelector("[data-cp-action-bar]");
    const planContent = root.querySelector("[data-cp-plan-content]");
    const planHashNode = root.querySelector("[data-cp-plan-hash]");
    const evidenceList = root.querySelector("[data-cp-evidence-list]");
    const evidenceCount = root.querySelector("[data-cp-evidence-count]");
    const timelineList = root.querySelector("[data-cp-timeline-list]");
    const timelineCount = root.querySelector("[data-cp-timeline-count]");
    const runContent = root.querySelector("[data-cp-run-content]");
    const pollState = root.querySelector("[data-cp-poll-state]");
    const approvalDialog = root.querySelector("[data-cp-approval-dialog]");
    const approvalForm = root.querySelector("[data-cp-approval-form]");
    const approvalTitle = root.querySelector("[data-cp-approval-title]");
    const approvalPlanHash = root.querySelector("[data-cp-approval-plan-hash]");
    const approvalComment = root.querySelector("[data-cp-approval-comment]");
    const approvalSubmit = root.querySelector("[data-cp-approval-submit]");
    const runActionDialog = root.querySelector("[data-cp-run-action-dialog]");
    const runActionForm = root.querySelector("[data-cp-run-action-form]");
    const runActionTitle = root.querySelector("[data-cp-run-action-title]");
    const runActionLabel = root.querySelector("[data-cp-run-action-label]");
    const runActionValue = root.querySelector("[data-cp-run-action-value]");
    const clarificationFields = root.querySelector("[data-cp-clarification-fields]");
    const clarificationAccountId = root.querySelector("[data-cp-clarification-account-id]");
    const clarificationArguments = root.querySelector("[data-cp-clarification-arguments]");
    const runActionSubmit = root.querySelector("[data-cp-run-action-submit]");
    const assignDialog = root.querySelector("[data-cp-assign-dialog]");
    const assignForm = root.querySelector("[data-cp-assign-form]");
    const assignOwner = root.querySelector("[data-cp-assign-owner]");

    const state = {
      detail: {},
      workItem: {},
      plan: {},
      approval: {},
      run: {},
      allowedActions: new Set(),
      runId: "",
      pollTimer: 0,
      polling: false,
      pendingApprovalDecision: "",
      pendingRunAction: "",
      lastRunStatus: "",
    };

    function normalizeAllowedActions(value) {
      if (Array.isArray(value)) {
        return new Set(value.map((item) => {
          if (typeof item === "string") return item.toLowerCase();
          return String(asObject(item).action || asObject(item).name || "").toLowerCase();
        }).filter(Boolean));
      }
      const source = asObject(value);
      return new Set(Object.entries(source)
        .filter(([, enabled]) => enabled === true)
        .map(([name]) => name.toLowerCase()));
    }

    function actionAllowed(name) {
      return state.allowedActions.has(name)
        || state.allowedActions.has(`run.${name}`)
        || state.allowedActions.has(`approval.${name}`)
        || state.allowedActions.has(`work_item.${name}`);
    }

    function renderHeader() {
      const item = state.workItem;
      statusNode.textContent = statusLabel(item.status);
      statusNode.dataset.tone = statusTone(item.status);
      idNode.textContent = textValue(item.work_item_id || item.id || workItemId);
      titleNode.textContent = textValue(item.title, "未提供标题");
      reasonNode.textContent = textValue(
        item.current_reason_summary || item.current_reason || item.summary || item.description,
        "",
      );
      metaNode.replaceChildren(
        makeDefinition("事项类型", item.type),
        makeDefinition("优先级", priorityLabel(item.priority)),
        makeDefinition("责任人", ownerLabel(item.owner || item.owner_id)),
        makeDefinition("来源", item.source),
        makeDefinition("SLA 截止", formatDateTime(item.sla_deadline)),
        makeDefinition("更新时间", formatDateTime(item.updated_at)),
      );
      header.setAttribute("aria-busy", "false");
    }

    function renderPlan() {
      const plan = state.plan;
      planContent.replaceChildren();
      planContent.setAttribute("aria-busy", "false");
      const planHash = String(plan.plan_hash || state.approval.plan_hash || "").trim();
      planHashNode.textContent = planHash;
      planHashNode.title = planHash;
      if (!Object.keys(plan).length) {
        appendText(planContent, "p", "cp-muted", "当前事项没有可展示的执行计划。 ");
        return;
      }

      const steps = asList(plan.steps);
      const riskOrder = { LOW: 0, MEDIUM: 1, HIGH: 2, EXTREME: 3 };
      const highestRisk = steps.reduce((current, stepValue) => {
        const candidate = normalizedState(asObject(stepValue).risk_level);
        return (riskOrder[candidate] ?? -1) > (riskOrder[current] ?? -1)
          ? candidate
          : current;
      }, "");
      const approvalRequired = steps.some(
        (stepValue) => asObject(stepValue).requires_approval === true,
      );
      const summary = createElement("div", "cp-plan-summary");
      const summaryValues = [
        ["风险等级", riskLabel(highestRisk || plan.risk_level)],
        ["人工审批", approvalRequired ? "需要" : "不需要"],
        ["计划步骤", String(steps.length)],
      ];
      for (const [label, value] of summaryValues) {
        const wrapper = createElement("div");
        appendText(wrapper, "span", "", label);
        appendText(wrapper, "strong", "", value);
        summary.append(wrapper);
      }
      planContent.append(summary);

      const objective = plan.objective || plan.intent || plan.command_type;
      if (objective) appendText(planContent, "p", "", String(objective));
      if (!steps.length) {
        appendText(planContent, "p", "cp-muted", "计划未提供步骤。 ");
        return;
      }
      const list = createElement("ol", "cp-step-list");
      for (const stepValue of steps) {
        const step = asObject(stepValue);
        const item = createElement("li");
        appendText(
          item,
          "strong",
          "",
          textValue(step.title || step.tool_name || step.step_key || step.operation_type, "计划步骤"),
        );
        const details = [
          `版本：${textValue(step.tool_version)}`,
          `操作：${textValue(step.operation_type)}`,
          `账号：${textValue(step.account_id)}`,
          `风险：${riskLabel(step.risk_level)}`,
          `审批：${step.requires_approval === true ? "需要" : "不需要"}`,
        ];
        appendText(item, "p", "cp-muted", details.join(" · "));
        list.append(item);
      }
      planContent.append(list);

      const impact = asObject(plan.impact);
      if (Object.keys(impact).length) {
        const impactSection = createElement("section", "cp-plan-impact");
        appendText(impactSection, "h4", "", "影响范围");
        const impactMeta = createElement("dl", "cp-run-grid");
        impactMeta.append(
          makeDefinition("工具", impact.tool_name),
          makeDefinition("操作类型", impact.operation_type),
          makeDefinition("账号", impact.account_id),
        );
        impactSection.append(impactMeta);

        const entities = asList(impact.entities);
        if (entities.length) {
          const entityList = createElement("ul", "cp-impact-list");
          for (const entityValue of entities) {
            const entity = asObject(entityValue);
            appendText(
              entityList,
              "li",
              "",
              `${textValue(entity.entity_type)}：${textValue(entity.entity_id)}`,
            );
          }
          impactSection.append(entityList);
        }

        const amounts = asObject(impact.amounts);
        const amountEntries = Object.entries(amounts);
        if (amountEntries.length) {
          const amountList = createElement("ul", "cp-impact-list");
          for (const [field, amount] of amountEntries) {
            appendText(amountList, "li", "", `${field}：${textValue(amount)}`);
          }
          impactSection.append(amountList);
        }
        planContent.append(impactSection);
      }
    }

    function renderEvidence(items) {
      evidenceList.replaceChildren();
      evidenceList.setAttribute("aria-busy", "false");
      evidenceCount.textContent = `${items.length} 条`;
      if (!items.length) {
        appendText(evidenceList, "p", "cp-muted cp-section-content", "当前没有可展示的证据。 ");
        return;
      }
      for (const value of items) {
        const evidence = asObject(value);
        const record = createElement("article", "cp-record");
        const heading = createElement("div", "cp-record-heading");
        appendText(
          heading,
          "strong",
          "",
          textValue(evidence.title || evidence.summary || evidence.evidence_type, "证据记录"),
        );
        appendText(heading, "span", "cp-muted", formatDateTime(evidence.observed_at));
        record.append(heading);
        if (typeof evidence.summary === "string" && evidence.title) {
          appendText(record, "p", "", evidence.summary);
        }
        const meta = createElement("div", "cp-record-meta");
        appendText(meta, "span", "", `来源：${textValue(evidence.source_system)}`);
        appendText(
          meta,
          "span",
          "",
          `记录：${textValue(evidence.source_record_id || evidence.record_id)}`,
        );
        if (typeof evidence.pagination_complete === "boolean") {
          appendText(
            meta,
            "span",
            "",
            evidence.pagination_complete ? "分页完整" : "分页未完整",
          );
        }
        if (evidence.completeness_status) {
          appendText(meta, "span", "", `完整性：${textValue(evidence.completeness_status)}`);
        }
        record.append(meta);
        evidenceList.append(record);
      }
    }

    function renderTimeline(items) {
      timelineList.replaceChildren();
      timelineList.setAttribute("aria-busy", "false");
      timelineCount.textContent = `${items.length} 条`;
      if (!items.length) {
        appendText(timelineList, "li", "cp-muted", "当前没有时间线记录。 ");
        return;
      }
      for (const value of items) {
        const event = asObject(value);
        const item = createElement("li");
        appendText(
          item,
          "strong",
          "",
          statusLabel(event.to_status || event.status || event.event_type || event.type),
        );
        const message = event.message || event.summary || event.reason;
        if (message) appendText(item, "p", "", message);
        const meta = createElement("p", "cp-muted");
        meta.textContent = `${actorLabel(event.actor)} · ${formatDateTime(event.occurred_at || event.created_at)}`;
        item.append(meta);
        timelineList.append(item);
      }
    }

    function renderRun() {
      const run = state.run;
      runContent.replaceChildren();
      runContent.setAttribute("aria-busy", "false");
      if (!Object.keys(run).length) {
        appendText(runContent, "p", "cp-muted", "当前事项没有关联执行。 ");
        pollState.textContent = "";
        return;
      }
      const grid = createElement("dl", "cp-run-grid");
      const statusWrapper = createElement("div");
      appendText(statusWrapper, "dt", "", "状态");
      const statusDescription = createElement("dd");
      statusDescription.append(makeStatus(run.status));
      statusWrapper.append(statusDescription);
      grid.append(
        statusWrapper,
        makeDefinition("Run ID", run.run_id || state.runId),
        makeDefinition("开始时间", formatDateTime(run.started_at || run.created_at)),
        makeDefinition("更新时间", formatDateTime(run.updated_at)),
        makeDefinition("当前步骤", run.current_step || run.current_step_key),
        makeDefinition(
          "尝试次数",
          run.attempt ?? run.attempt_count ?? run.execution_attempt_count,
        ),
      );
      runContent.append(grid);
      const error = asObject(run.error);
      const reason = run.error_summary || run.current_reason || run.reason || error.message;
      if (reason) appendText(runContent, "p", "cp-muted", reason);
    }

    function addAction(label, handler, { primary = false, danger = false } = {}) {
      const button = createElement("button", primary ? "primary-btn" : "ghost-btn", label);
      button.type = "button";
      if (danger) button.classList.add("cp-danger-button");
      button.addEventListener("click", handler);
      actionBar.append(button);
    }

    function renderActions() {
      actionBar.replaceChildren();
      const approvalId = String(state.approval.approval_id || "").trim();
      const planHash = String(state.approval.plan_hash || state.plan.plan_hash || "").trim();
      if (approvalId && planHash && actionAllowed("approve")) {
        addAction("批准计划", () => openApproval("approve"), { primary: true });
      }
      if (approvalId && planHash && actionAllowed("reject")) {
        addAction("驳回计划", () => openApproval("reject"), { danger: true });
      }
      if (state.runId && actionAllowed("clarify")) {
        addAction("补充信息", () => openRunAction("clarify"));
      }
      if (state.runId && actionAllowed("retry")) {
        addAction("重试执行", () => openRunAction("retry"));
      }
      if (state.runId && actionAllowed("cancel")) {
        addAction("取消执行", () => openRunAction("cancel"), { danger: true });
      }
      if (actionAllowed("assign")) {
        addAction("分配责任人", openAssignment);
      }
    }

    function openApproval(decision) {
      state.pendingApprovalDecision = decision;
      const planHash = String(state.approval.plan_hash || state.plan.plan_hash || "");
      approvalPlanHash.value = planHash;
      approvalComment.value = "";
      approvalTitle.textContent = decision === "approve" ? "批准当前计划" : "驳回当前计划";
      approvalSubmit.textContent = decision === "approve" ? "确认批准" : "确认驳回";
      approvalSubmit.classList.toggle("cp-danger-button", decision === "reject");
      approvalDialog.showModal();
      approvalComment.focus();
    }

    function openRunAction(action) {
      state.pendingRunAction = action;
      runActionValue.value = "";
      clarificationAccountId.value = "";
      clarificationArguments.value = "";
      clarificationFields.hidden = action !== "clarify";
      const labels = {
        cancel: ["取消当前执行", "取消说明", "确认取消"],
        retry: ["重试当前执行", "重试原因", "确认重试"],
        clarify: ["补充执行信息", "补充信息", "提交信息"],
      };
      const [title, label, submit] = labels[action];
      runActionTitle.textContent = title;
      runActionLabel.textContent = label;
      runActionSubmit.textContent = submit;
      runActionSubmit.classList.toggle("cp-danger-button", action === "cancel");
      runActionValue.required = false;
      runActionDialog.showModal();
      runActionValue.focus();
    }

    function openAssignment() {
      assignOwner.value = String(state.workItem.owner_id || "").trim();
      assignDialog.showModal();
      assignOwner.focus();
    }

    function updateStateFromDetail(data) {
      state.detail = asObject(data);
      state.workItem = asObject(state.detail.work_item);
      state.plan = asObject(
        state.detail.plan || state.workItem.current_plan || state.workItem.plan,
      );
      state.approval = asObject(
        state.detail.approval || state.workItem.pending_approval || state.workItem.approval,
      );
      const detailRun = asObject(
        state.detail.run || state.workItem.current_run || state.workItem.run,
      );
      if (Object.keys(detailRun).length) state.run = detailRun;
      state.runId = String(
        state.run.run_id
        || state.detail.current_run_id
        || state.workItem.current_run_id
        || state.workItem.run_id
        || "",
      ).trim();
      state.allowedActions = normalizeAllowedActions(
        state.detail.allowed_actions
        || state.workItem.allowed_actions
        || state.run.allowed_actions,
      );
    }

    async function refreshCollections() {
      const encodedId = encodeURIComponent(workItemId);
      const [timelineResult, evidenceResult] = await Promise.allSettled([
        request(`/control-plane/work-items/${encodedId}/timeline`),
        request(`/control-plane/work-items/${encodedId}/evidence`),
      ]);
      if (timelineResult.status === "fulfilled") {
        renderTimeline(asList(asObject(timelineResult.value).items));
      } else {
        timelineList.setAttribute("aria-busy", "false");
        timelineList.replaceChildren(createElement("li", "cp-muted", "时间线暂时无法读取。 "));
      }
      if (evidenceResult.status === "fulfilled") {
        renderEvidence(asList(asObject(evidenceResult.value).items));
      } else {
        evidenceList.setAttribute("aria-busy", "false");
        evidenceList.replaceChildren(createElement("p", "cp-muted cp-section-content", "证据暂时无法读取。 "));
      }
    }

    function clearPollTimer() {
      if (state.pollTimer) window.clearTimeout(state.pollTimer);
      state.pollTimer = 0;
    }

    function pollingDelay(run) {
      const requested = Number(run.next_poll_after_ms);
      const bounded = Number.isFinite(requested) ? Math.min(15000, Math.max(1000, requested)) : 3000;
      return SLOW_POLL_STATES.has(normalizedState(run.status)) ? Math.max(10000, bounded) : bounded;
    }

    function schedulePoll() {
      clearPollTimer();
      if (!state.runId || TERMINAL_RUN_STATES.has(normalizedState(state.run.status))) {
        pollState.textContent = state.runId ? "已停止轮询" : "";
        return;
      }
      if (document.hidden) {
        pollState.textContent = "页面隐藏，轮询已暂停";
        return;
      }
      pollState.textContent = "状态自动更新中";
      state.pollTimer = window.setTimeout(pollRun, pollingDelay(state.run));
    }

    async function pollRun() {
      clearPollTimer();
      if (state.polling || !state.runId || document.hidden) {
        schedulePoll();
        return;
      }
      state.polling = true;
      try {
        const data = asObject(await request(`/control-plane/runs/${encodeURIComponent(state.runId)}`));
        const run = asObject(data.run);
        const previousStatus = normalizedState(state.run.status);
        state.run = {
          ...run,
          next_poll_after_ms: run.next_poll_after_ms ?? data.next_poll_after_ms,
        };
        state.runId = String(run.run_id || state.runId);
        state.allowedActions = normalizeAllowedActions(
          data.allowed_actions || run.allowed_actions || [...state.allowedActions],
        );
        renderRun();
        renderActions();
        const nextStatus = normalizedState(run.status);
        if (nextStatus !== previousStatus) await refreshCollections();
      } catch (error) {
        pollState.textContent = `状态更新暂停：${error.message}`;
      } finally {
        state.polling = false;
        schedulePoll();
      }
    }

    async function loadDetail({ announce = false } = {}) {
      clearPollTimer();
      if (announce) showFeedback(feedback, "正在更新事项…", "success");
      else showFeedback(feedback, "");
      try {
        const encodedId = encodeURIComponent(workItemId);
        const [detailData] = await Promise.all([
          request(`/control-plane/work-items/${encodedId}`),
          refreshCollections(),
        ]);
        updateStateFromDetail(detailData);
        if (state.runId && !Object.keys(state.run).length) {
          const runData = asObject(await request(`/control-plane/runs/${encodeURIComponent(state.runId)}`));
          state.run = asObject(runData.run);
        }
        renderHeader();
        renderPlan();
        renderRun();
        renderActions();
        if (announce) showFeedback(feedback, "事项已更新。", "success");
        schedulePoll();
      } catch (error) {
        header.setAttribute("aria-busy", "false");
        planContent.setAttribute("aria-busy", "false");
        runContent.setAttribute("aria-busy", "false");
        showFeedback(feedback, error.message || "事项详情读取失败。 ");
      }
    }

    async function submitAction(button, path, body) {
      setButtonBusy(button, true);
      showFeedback(feedback, "");
      try {
        const data = asObject(await request(path, { method: "POST", body }));
        if (data.run && typeof data.run === "object") state.run = data.run;
        showFeedback(feedback, "操作已提交。", "success");
        await loadDetail();
        return true;
      } catch (error) {
        showFeedback(feedback, error.message || "操作提交失败。 ");
        return false;
      } finally {
        setButtonBusy(button, false);
      }
    }

    approvalForm.addEventListener("submit", async (event) => {
      if (event.submitter?.value === "cancel") return;
      event.preventDefault();
      const decision = state.pendingApprovalDecision;
      const approvalId = String(state.approval.approval_id || "").trim();
      if (!approvalId || !["approve", "reject"].includes(decision)) return;
      const successful = await submitAction(
        approvalSubmit,
        `/control-plane/approvals/${encodeURIComponent(approvalId)}/${decision}`,
        {
          plan_hash: approvalPlanHash.value,
          comment: approvalComment.value.trim(),
        },
      );
      if (successful) approvalDialog.close();
    });

    runActionForm.addEventListener("submit", async (event) => {
      if (event.submitter?.value === "cancel") return;
      event.preventDefault();
      const action = state.pendingRunAction;
      if (!state.runId || !["cancel", "retry", "clarify"].includes(action)) return;
      const value = runActionValue.value.trim();
      let clarification = value;
      if (action === "clarify") {
        const accountId = clarificationAccountId.value.trim();
        const updatesText = clarificationArguments.value.trim();
        const structured = {};
        if (value) structured.note = value;
        if (accountId) structured.account_id = accountId;
        if (updatesText) {
          let updates;
          try {
            updates = JSON.parse(updatesText);
          } catch (_error) {
            showFeedback(feedback, "参数更新必须是有效的 JSON 对象。");
            return;
          }
          if (!updates || Array.isArray(updates) || typeof updates !== "object") {
            showFeedback(feedback, "参数更新必须是 JSON 对象。");
            return;
          }
          structured.argument_updates = updates;
        }
        if (!Object.keys(structured).length) {
          showFeedback(feedback, "请填写补充说明、账号 ID 或参数更新。");
          return;
        }
        clarification = structured;
      }
      const bodies = {
        cancel: { comment: value },
        retry: { reason: value },
        clarify: { clarification },
      };
      const successful = await submitAction(
        runActionSubmit,
        `/control-plane/runs/${encodeURIComponent(state.runId)}/${action}`,
        bodies[action],
      );
      if (successful) runActionDialog.close();
    });

    assignForm.addEventListener("submit", async (event) => {
      if (event.submitter?.value === "cancel") return;
      event.preventDefault();
      const submitter = event.submitter;
      const successful = await submitAction(
        submitter,
        `/control-plane/work-items/${encodeURIComponent(workItemId)}/assign`,
        { owner_id: assignOwner.value },
      );
      if (successful) assignDialog.close();
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearPollTimer();
        if (state.runId) pollState.textContent = "页面隐藏，轮询已暂停";
        return;
      }
      if (state.runId && !TERMINAL_RUN_STATES.has(normalizedState(state.run.status))) {
        pollRun();
      }
    });
    window.addEventListener("beforeunload", clearPollTimer, { once: true });
    loadDetail();
  }

  const listPage = document.querySelector("[data-cp-list-page]");
  if (listPage) initListPage(listPage);
  const detailPage = document.querySelector("[data-cp-detail-page]");
  if (detailPage) initDetailPage(detailPage);

  window.ControlPlaneApi = Object.freeze({
    request,
    submitCommand(command) {
      const requestId = crypto.randomUUID();
      const body = { ...command };
      delete body.idempotency_key;
      return request("/control-plane/commands", {
        method: "POST",
        headers: { "X-Browser-Request-UUID": requestId },
        body,
      });
    },
  });
})();
