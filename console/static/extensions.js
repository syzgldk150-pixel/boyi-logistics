(() => {
  "use strict";

  const MAX_NAME_LENGTH = 120;
  const SAFE_ENTRYPOINT = /^[a-z][a-z0-9_.-]{0,127}$/;
  const SAFE_KINDS = new Set([
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
    "harness",
    "module_slots",
  ]);

  const secureRequestId = () => window.crypto?.randomUUID?.() || "";

  const setFeedback = (box, message = "", kind = "") => {
    if (!(box instanceof HTMLElement)) return;
    box.textContent = message;
    box.hidden = !message;
    if (kind) box.dataset.kind = kind;
    else delete box.dataset.kind;
  };

  const responseMessage = (payload, fallback) => (
    payload?.error?.message || payload?.message || fallback
  );

  const createNode = (tag, className = "", text = undefined) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };

  const clearNode = (node) => {
    if (!(node instanceof Node)) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  };

  const safeText = (value, fallback = "") => {
    if (typeof value === "string") return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return fallback;
  };

  const safeEntryPoint = (value) => {
    const id = safeText(value);
    return SAFE_ENTRYPOINT.test(id) ? id : "";
  };

  const safeProjection = (raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const pluginId = safeText(raw.plugin_id);
    const version = safeText(raw.version);
    const name = safeText(raw.name, pluginId);
    if (!pluginId || !version || !name) return null;

    const hostApi = raw.host_api && typeof raw.host_api === "object" && !Array.isArray(raw.host_api)
      ? {
          minimum: safeText(raw.host_api.minimum),
          maximum_exclusive: safeText(raw.host_api.maximum_exclusive),
        }
      : {minimum: "", maximum_exclusive: ""};

    const permissions = Array.isArray(raw.permissions)
      ? raw.permissions
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          name: safeText(item.name),
          operations: Array.isArray(item.operations)
            ? item.operations.map((value) => safeEntryPoint(value)).filter(Boolean).slice(0, 64)
            : [],
          account_role: safeRole(item.account_role),
          resource_role: safeRole(item.resource_role),
        }))
        .filter((item) => item.name)
        .slice(0, 128)
      : [];

    const contributions = Array.isArray(raw.contributions)
      ? raw.contributions
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          id: safeEntryPoint(item.id),
          kind: SAFE_KINDS.has(safeText(item.kind)) ? safeText(item.kind) : "",
          title: safeText(item.title),
          effect: safeText(item.effect),
        }))
        .filter((item) => item.id && item.kind)
        .slice(0, 128)
      : [];

    const warnings = Array.isArray(raw.warnings)
      ? raw.warnings
        .map((item) => safeText(item).slice(0, 512))
        .filter(Boolean)
        .slice(0, 16)
      : [];

    return {
      plugin_id: pluginId,
      name,
      version,
      host_api: hostApi,
      permissions,
      contributions,
      warnings,
    };
  };

  const fileIsZip = (file) => file instanceof File && /\.zip$/i.test(file.name || "");

  const filenameFallback = (file) => {
    if (!fileIsZip(file)) return "";
    return String(file.name || "")
      .replace(/\.zip$/i, "")
      .replace(/[\u0000-\u001f\u007f]/g, "")
      .trim()
      .slice(0, MAX_NAME_LENGTH);
  };

  const renderPermissions = (container, permissions) => {
    clearNode(container);
    if (!permissions.length) {
      container.appendChild(createNode("p", "extension-empty-copy", "此扩展未声明额外权限。"));
      return;
    }
    permissions.forEach((permission) => {
      const item = createNode("div", "extension-permission-item");
      item.appendChild(createNode("strong", "extension-permission-name", permission.name));
      item.appendChild(createNode("span", "extension-permission-scope", "仅在扩展运行时使用，并保留操作记录。"));
      container.appendChild(item);
    });
  };

  const renderSummary = (container, projection) => {
    clearNode(container);
    const rows = [
      ["扩展名称", projection.name],
      ["版本", projection.version],
      ["包含功能", `${projection.contributions.length} 项`],
      ["需要授权", `${projection.permissions.length} 项`],
    ];
    rows.forEach(([term, value]) => {
      const wrapper = createNode("div");
      wrapper.appendChild(createNode("dt", "", term));
      wrapper.appendChild(createNode("dd", "", value || "未提供"));
      container.appendChild(wrapper);
    });
  };

  const renderWarnings = (container, warnings) => {
    clearNode(container);
    if (!warnings.length) return;
    warnings.forEach((warning) => {
      container.appendChild(createNode("p", "extension-wizard-warning", warning));
    });
  };

  const setupInstallWizard = () => {
    const wizard = document.querySelector("[data-extension-wizard]");
    const dialog = document.querySelector("[data-extension-dialog]");
    const inspectForm = wizard?.querySelector("[data-extension-inspect-form]");
    const finalForm = wizard?.querySelector("[data-extension-final-form]");
    if (!(wizard instanceof HTMLElement)
      || !(dialog instanceof HTMLDialogElement)
      || !(inspectForm instanceof HTMLFormElement)
      || !(finalForm instanceof HTMLFormElement)) return;

    const fileInput = inspectForm.querySelector('input[name="package"]');
    const nameInput = wizard.querySelector("[data-extension-instance-name]");
    const inspectButton = inspectForm.querySelector("[data-extension-inspect-submit]");
    const inspectFeedback = inspectForm.querySelector("[data-extension-feedback]");
    const inspection = wizard.querySelector("[data-extension-inspection]");
    const finalButton = finalForm.querySelector("[data-extension-install-submit]");
    const finalFeedback = finalForm.querySelector("[data-extension-final-feedback]");
    const resetButton = wizard.querySelector("[data-extension-reset]");
    const dropzone = wizard.querySelector("[data-extension-dropzone]");
    const fileName = wizard.querySelector("[data-extension-file-name]");
    const inspectionName = wizard.querySelector("[data-extension-inspection-name]");
    const state = {requestId: "", packageFile: null, projection: null, finalSent: false, frozen: null, sending: false};

    const lockAfterFinalSend = () => {
      wizard.querySelectorAll("input, select, textarea, button").forEach((control) => {
        if (control === finalButton) return;
        control.disabled = true;
      });
    };

    const resetInspection = () => {
      if (state.finalSent) return;
      state.projection = null;
      state.requestId = "";
      state.packageFile = null;
      clearNode(wizard.querySelector("[data-extension-inspection-summary]"));
      clearNode(wizard.querySelector("[data-extension-permissions]"));
      clearNode(wizard.querySelector("[data-extension-inspection-warnings]"));
      const permissionConfirmation = wizard.querySelector("[data-extension-permissions-confirmed]");
      if (permissionConfirmation instanceof HTMLInputElement) permissionConfirmation.checked = false;
      if (inspection instanceof HTMLElement) inspection.hidden = true;
      if (finalButton instanceof HTMLButtonElement) finalButton.disabled = true;
      if (fileName instanceof HTMLElement) fileName.textContent = "仅支持 .zip 文件";
    };

    const renderInspection = (projection) => {
      renderSummary(wizard.querySelector("[data-extension-inspection-summary]"), projection);
      renderWarnings(wizard.querySelector("[data-extension-inspection-warnings]"), projection.warnings);
      renderPermissions(wizard.querySelector("[data-extension-permissions]"), projection.permissions);
      const derivedName = filenameFallback(state.packageFile);
      if (nameInput instanceof HTMLInputElement && !nameInput.value.trim() && derivedName) nameInput.value = derivedName;
      if (inspectionName instanceof HTMLElement) inspectionName.textContent = `${projection.name} 已通过检查，请确认设置。`;
      if (inspection instanceof HTMLElement) inspection.hidden = false;
      inspection.scrollIntoView({block: "start", behavior: "smooth"});
    };

    const buildIntent = () => ({
      instance_name: String(nameInput?.value || "").trim(),
      permissions_confirmed: wizard.querySelector("[data-extension-permissions-confirmed]")?.checked === true,
    });

    const validateIntent = (intent) => {
      if (!intent.instance_name || intent.instance_name.length > MAX_NAME_LENGTH) return "请填写 1 至 120 个字符的项目名称。";
      if (!state.packageFile || !fileIsZip(state.packageFile)) return "请保留已检查的扩展压缩包。";
      if (!intent.permissions_confirmed) return "请先确认扩展权限。";
      return "";
    };

    const sendFinal = async () => {
      if (state.sending || !state.frozen) return;
      state.sending = true;
      if (finalButton instanceof HTMLButtonElement) {
        finalButton.disabled = true;
        finalButton.textContent = "提交中…";
        finalButton.setAttribute("aria-busy", "true");
      }
      try {
        const body = new FormData();
        body.set("package", state.frozen.packageFile);
        body.set("request_id", state.frozen.requestId);
        body.set("intent", state.frozen.serializedIntent);
        const response = await fetch(finalForm.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-Browser-Request-UUID": state.frozen.requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body,
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok || payload?.ok !== true) throw new Error(responseMessage(payload, "安装失败，请重试相同请求。"));
        setFeedback(finalFeedback, "扩展安装请求已提交。", "success");
        if (finalButton instanceof HTMLButtonElement) finalButton.textContent = "已提交";
        window.location.assign("/automations");
      } catch (error) {
        setFeedback(finalFeedback, error instanceof Error ? error.message : "安装失败，请重试相同请求。", "error");
        if (finalButton instanceof HTMLButtonElement) {
          finalButton.disabled = false;
          finalButton.textContent = "重试相同安装";
          finalButton.removeAttribute("aria-busy");
        }
      } finally {
        state.sending = false;
      }
    };

    inspectForm.addEventListener("input", (event) => {
      if (state.finalSent) return;
      if (event.target === fileInput) resetInspection();
      setFeedback(inspectFeedback);
    });
    fileInput?.addEventListener("change", () => {
      if (state.finalSent) return;
      state.packageFile = fileInput.files?.[0] || null;
      resetInspection();
      state.packageFile = fileInput.files?.[0] || null;
      if (!state.packageFile) return;
      if (fileName instanceof HTMLElement) fileName.textContent = state.packageFile.name;
      if (!fileIsZip(state.packageFile)) {
        setFeedback(inspectFeedback, "请选择一个扩展压缩包。", "error");
        return;
      }
      window.requestAnimationFrame(() => inspectForm.requestSubmit());
    });

    if (dropzone instanceof HTMLElement && fileInput instanceof HTMLInputElement) {
      ["dragenter", "dragover"].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
          event.preventDefault();
          if (!state.finalSent) dropzone.classList.add("is-dragging");
        });
      });
      ["dragleave", "drop"].forEach((eventName) => {
        dropzone.addEventListener(eventName, () => dropzone.classList.remove("is-dragging"));
      });
      dropzone.addEventListener("drop", (event) => {
        event.preventDefault();
        if (state.finalSent || !event.dataTransfer?.files?.length) return;
        const transfer = new DataTransfer();
        transfer.items.add(event.dataTransfer.files[0]);
        fileInput.files = transfer.files;
        fileInput.dispatchEvent(new Event("change", {bubbles: true}));
      });
    }

    inspectForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.finalSent) return;
      const file = fileInput?.files?.[0];
      if (!fileIsZip(file)) {
        setFeedback(inspectFeedback, "请选择一个扩展压缩包。", "error");
        return;
      }
      state.packageFile = file;
      if (!state.requestId) state.requestId = secureRequestId();
      if (!state.requestId) {
        setFeedback(inspectFeedback, "浏览器未提供安全请求标识，未提交检查。", "error");
        return;
      }
      const inspectedFile = file;
      const inspectedRequestId = state.requestId;
      const body = new FormData();
      body.set("package", file);
      body.set("request_id", inspectedRequestId);
      if (inspectButton instanceof HTMLButtonElement) {
        inspectButton.disabled = true;
        inspectButton.textContent = "检查中…";
        inspectButton.setAttribute("aria-busy", "true");
      }
      setFeedback(inspectFeedback, "正在检查扩展压缩包，尚未安装项目。", "warning");
      try {
        const response = await fetch(inspectForm.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-Browser-Request-UUID": inspectedRequestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body,
        });
        const payload = await response.json().catch(() => null);
        if (state.packageFile !== inspectedFile || state.requestId !== inspectedRequestId) return;
        if (!response.ok || payload?.ok !== true) throw new Error(responseMessage(payload, "扩展压缩包检查失败，请重试。"));
        const projection = safeProjection(payload.data);
        if (!projection) throw new Error("智能服务返回的检查结果无效，未进入安装流程。");
        state.projection = projection;
        renderInspection(projection);
        setFeedback(inspectFeedback, "扩展压缩包检查完成，请继续确认。", "success");
      } catch (error) {
        if (state.packageFile !== inspectedFile || state.requestId !== inspectedRequestId) return;
        state.projection = null;
        if (inspection instanceof HTMLElement) inspection.hidden = true;
        setFeedback(inspectFeedback, error instanceof Error ? error.message : "扩展压缩包检查失败，请重试。", "error");
      } finally {
        if (inspectButton instanceof HTMLButtonElement) {
          inspectButton.disabled = state.finalSent;
          inspectButton.textContent = "检查扩展";
          inspectButton.removeAttribute("aria-busy");
        }
      }
    });

    wizard.addEventListener("input", () => {
      if (state.finalSent) return;
      if (state.projection && finalButton instanceof HTMLButtonElement) finalButton.disabled = false;
      setFeedback(finalFeedback);
    });

    finalForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.finalSent) {
        sendFinal();
        return;
      }
      if (!state.projection) {
        setFeedback(finalFeedback, "请先完成扩展检查。", "error");
        return;
      }
      const intent = buildIntent();
      const validationError = validateIntent(intent);
      if (validationError) {
        setFeedback(finalFeedback, validationError, "error");
        return;
      }
      let serializedIntent;
      try {
        serializedIntent = JSON.stringify(intent);
      } catch (_error) {
        setFeedback(finalFeedback, "安装意图无法序列化，未提交安装。", "error");
        return;
      }
      state.frozen = Object.freeze({
        packageFile: state.packageFile,
        requestId: state.requestId,
        serializedIntent,
      });
      state.finalSent = true;
      lockAfterFinalSend();
      sendFinal();
    });

    resetButton?.addEventListener("click", () => {
      if (state.finalSent) return;
      resetInspection();
      if (fileInput instanceof HTMLInputElement) fileInput.value = "";
      if (nameInput instanceof HTMLInputElement) nameInput.value = "";
      setFeedback(inspectFeedback);
      setFeedback(finalFeedback);
      fileInput?.focus();
    });

    document.querySelectorAll("[data-extension-open]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!dialog.open) dialog.showModal();
        window.requestAnimationFrame(() => fileInput?.focus());
      });
    });
    wizard.querySelectorAll("[data-extension-close]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!state.finalSent) dialog.close();
      });
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog && !state.finalSent) dialog.close();
    });
  };

  document.querySelectorAll("[data-extension-instance]").forEach((card) => {
    const feedback = card.querySelector("[data-extension-feedback]");
    const automationId = encodeURIComponent(card.dataset.automationId || "");
    const recordVersion = Number(card.dataset.recordVersion || "0");
    const upgradeInput = card.querySelector("[data-extension-upgrade]");
    const upgradeButton = card.querySelector('[data-extension-action="upgrade"]');

    upgradeInput?.addEventListener("change", () => {
      if (upgradeButton instanceof HTMLButtonElement) delete upgradeButton.dataset.requestId;
      setFeedback(feedback);
    });

    card.querySelectorAll("[data-extension-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!(button instanceof HTMLButtonElement)) return;
        const action = button.dataset.extensionAction || "";
        if (
          !["upgrade", "enable", "disable", "uninstall"].includes(action)
          || !automationId
          || !Number.isInteger(recordVersion)
          || recordVersion < 1
        ) {
          setFeedback(feedback, "项目状态已失效，请刷新后重试。", "error");
          return;
        }
        if (
          action === "uninstall"
          && !window.confirm(
            "确认卸载这个扩展项目？只会卸载当前项目，不影响使用同一扩展的其他项目。当前项目会停止接收新任务，已在外部系统产生的结果不会被删除。",
          )
        ) return;

        const requestId = button.dataset.requestId || secureRequestId();
        if (!requestId) {
          setFeedback(feedback, "浏览器未提供安全请求标识，操作未提交。", "error");
          return;
        }
        button.dataset.requestId = requestId;
        let body;
        let headers;
        if (action === "upgrade") {
          const packageFile = upgradeInput?.files?.[0];
          if (!(packageFile instanceof File) || !/\.zip$/i.test(packageFile.name || "")) {
            delete button.dataset.requestId;
            setFeedback(feedback, "请先选择扩展压缩包。", "error");
            return;
          }
          body = new FormData();
          body.set("package", packageFile);
          body.set("request_id", requestId);
          body.set("expected_record_version", String(recordVersion));
          headers = {
            Accept: "application/json",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          };
        } else {
          body = JSON.stringify(action === "uninstall"
            ? {
                request_id: requestId,
                expected_record_version: recordVersion,
                current_version: card.dataset.currentVersion || "",
                confirm: true,
              }
            : {request_id: requestId, expected_record_version: recordVersion});
          headers = {
            Accept: "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          };
        }

        button.disabled = true;
        try {
          const response = await fetch(`/automations/plugins/${automationId}/${action}`, {
            method: "POST",
            credentials: "same-origin",
            headers,
            body,
          });
          const payload = await response.json().catch(() => null);
          if (!response.ok || payload?.ok !== true) {
            throw new Error(responseMessage(payload, "操作失败，请重试。"));
          }
          delete button.dataset.requestId;
          window.location.reload();
        } catch (error) {
          setFeedback(
            feedback,
            error instanceof Error ? error.message : "操作失败，请重试。",
            "error",
          );
          button.disabled = false;
        }
      });
    });
  });

  if (window.__EXTENSION_WIZARD_TEST__ === true) {
    window.__extensionWizardTest = {safeProjection};
  }

  setupInstallWizard();
})();
