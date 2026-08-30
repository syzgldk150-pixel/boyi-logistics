(() => {
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

  document.querySelectorAll("[data-extension-install-form]").forEach((form) => {
    const feedback = form.querySelector("[data-extension-feedback]");
    const submit = form.querySelector('button[type="submit"]');

    form.addEventListener("input", () => {
      if (submit instanceof HTMLButtonElement) delete submit.dataset.requestId;
      setFeedback(feedback);
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!(submit instanceof HTMLButtonElement)) return;
      const body = new FormData(form);
      const packageFile = body.get("package");
      if (!(packageFile instanceof File) || !/\.zip$/i.test(packageFile.name || "")) {
        setFeedback(feedback, "请选择一个 ZIP 扩展包。", "error");
        return;
      }
      const requestedName = String(body.get("instance_name") || "").trim();
      const generatedName = packageFile.name.replace(/\.zip$/i, "").trim().slice(0, 120);
      const instanceName = requestedName || generatedName;
      if (!instanceName) {
        setFeedback(feedback, "无法从 ZIP 文件名生成项目名称，请先填写项目名称。", "error");
        return;
      }
      const requestId = submit.dataset.requestId || secureRequestId();
      if (!requestId) {
        setFeedback(feedback, "浏览器未提供安全请求标识，未提交安装。", "error");
        return;
      }
      submit.dataset.requestId = requestId;
      body.set("instance_name", instanceName);
      body.set("request_id", requestId);
      submit.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          },
          body,
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok || payload?.ok !== true) {
          throw new Error(responseMessage(payload, "安装失败，请重试。"));
        }
        delete submit.dataset.requestId;
        window.location.assign("/extensions");
      } catch (error) {
        setFeedback(
          feedback,
          error instanceof Error ? error.message : "安装失败，请重试。",
          "error",
        );
        submit.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-extension-instance]").forEach((card) => {
    const feedback = card.querySelector("[data-extension-feedback]");
    const automationId = encodeURIComponent(card.dataset.automationId || "");
    const recordVersion = Number(card.dataset.recordVersion || "0");
    const upgradeInput = card.querySelector("[data-extension-upgrade]");
    const upgradeButton = card.querySelector('[data-extension-action="upgrade"]');

    upgradeInput?.addEventListener("change", () => {
      if (upgradeButton instanceof HTMLButtonElement) {
        delete upgradeButton.dataset.requestId;
      }
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
            "确认卸载此 Service v2 项目？系统会撤销项目权限并停止接收新任务，只删除本应用自有数据；外部系统中已经产生的结果无法撤销。",
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
            setFeedback(feedback, "请先选择 ZIP 扩展包。", "error");
            return;
          }
          body = new FormData();
          body.set("package", packageFile);
          body.set("request_id", requestId);
          body.set("expected_record_version", String(recordVersion));
          headers = {
            "Accept": "application/json",
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
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Browser-Request-UUID": requestId,
            "X-Requested-With": "XMLHttpRequest",
          };
        }

        button.disabled = true;
        try {
          const response = await fetch(`/extensions/${automationId}/${action}`, {
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
})();
