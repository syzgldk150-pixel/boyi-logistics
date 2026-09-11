(() => {
  "use strict";
  const root = document.querySelector("[data-plugin-maintenance]");
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = "true";
  async function submit(scope, action, payload) {
    if (scope.dataset.busy) return;
    const feedback = scope.querySelector("[data-maintenance-feedback]");
    const buttons = [...scope.querySelectorAll("button")];
    scope.dataset.busy = "true";
    buttons.forEach(button => { button.disabled = true; });
    feedback.textContent = "正在提交并核验，请稍候。";
    feedback.dataset.error = "false";
    // Keep the same id and immutable payload after an uncertain response.
    const key = JSON.stringify({action, payload});
    if (scope.dataset.requestKey !== key) {
      scope.dataset.requestKey = key;
      scope.dataset.requestId = crypto.randomUUID();
    }
    const requestId = scope.dataset.requestId;
    try {
      const response = await fetch(`/automations/plugin-migrations/${action}`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Browser-Request-UUID": requestId},
        body: JSON.stringify({...payload, request_id: requestId}),
      });
      const result = await response.json();
      if (!response.ok || result.ok !== true) {
        throw new Error(result.error?.message || result.message || "操作未完成，请刷新状态后核对。");
      }
      feedback.textContent = result.message || "操作已提交，请刷新状态核对。";
      if (result.data?.state === "PREPARING") {
        feedback.dataset.error = "true";
        return;
      }
      window.location.assign("/automations/maintenance");
    } catch (error) {
      feedback.dataset.error = "true";
      feedback.textContent = error.message || "连接失败，请刷新状态核对。";
    } finally {
      delete scope.dataset.busy;
      buttons.forEach(button => { button.disabled = false; });
    }
  }
  root.querySelector("[data-maintenance-create]")?.addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    submit(form.parentElement, "create", {
      source_automation_id: form.elements.source_automation_id.value,
      target_automation_id: form.elements.target_automation_id.value,
      business_key_fields: ["__host_business_date"], business_key_namespace: null,
    });
  });
  root.querySelectorAll("[data-maintenance-action]").forEach(button => {
    button.addEventListener("click", () => {
      const row = button.closest("[data-maintenance-pair]");
      submit(row, `${encodeURIComponent(row.dataset.maintenancePair)}/${button.dataset.maintenanceAction}`, {
        expected_record_version: Number(row.dataset.recordVersion), confirm: true,
      });
    });
  });
})();
