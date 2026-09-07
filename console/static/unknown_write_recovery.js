(() => {
  "use strict";
  const root = document.querySelector("[data-cp-detail-page]");
  const section = root?.querySelector("[data-unknown-write-section]");
  if (!section) return;
  const list = section.querySelector("[data-unknown-write-list]");
  const feedback = section.querySelector("[data-unknown-write-feedback]");
  const requests = new Map();
  let busy = false;

  function text(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }

  function render(records) {
    if (!Array.isArray(records)) return;
    section.hidden = records.length === 0;
    list.replaceChildren();
    records.forEach((row, index) => {
      const item = document.createElement("article");
      item.className = "cp-record";
      item.append(text("strong", `记录 ${index + 1} · 历史版本 ${row.generation}`));
      const meta = text("p", `执行记录：${row.run_id}`);
      meta.className = "cp-record-meta";
      item.append(meta);
      if (row.legacy_scope_unavailable === true) {
        item.append(text("p", "历史写入范围缺失，保留核验记录；不表示已成功。"));
      }
      if (row.identity_valid !== true) {
        item.append(text("p", "历史记录身份不完整，暂不能自动核验。"));
      } else if (row.recovery_supported !== true) {
        item.append(text("p", row.recovery_unavailable_reason || "该插件尚无已审核的证据核验入口，保留历史记录待处理。"));
      } else if (root.dataset.canVerifyUnknownWrite === "true") {
        const button = text("button", "检查已保存证据");
        button.type = "button";
        button.className = "ghost-btn";
        button.addEventListener("click", async () => {
          if (busy) return;
          busy = true;
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
          feedback.textContent = "正在检查选中记录已保存的证据…";
          const key = `${row.run_id}:${row.lease_id}`;
          if (!requests.has(key)) requests.set(key, crypto.randomUUID());
          try {
            const result = await window.ControlPlaneApi.request(
              `/control-plane/work-items/${encodeURIComponent(root.dataset.workItemId)}/verify-unknown-write`,
              { method: "POST", body: { run_id: row.run_id, lease_id: row.lease_id, request_id: requests.get(key) } },
            );
            feedback.textContent = result.message;
            requests.delete(key);
            if (result.recovery_status !== "UNKNOWN") {
              button.remove();
              item.append(text("p", "该条核验已记录；刷新事项可查看更新后的审计记录。"));
            }
          } catch (error) {
            feedback.textContent = error.message || "核验请求结果尚未确认。可重试同一条记录。";
          } finally {
            busy = false;
            button.disabled = false;
            button.setAttribute("aria-busy", "false");
          }
        });
        item.append(button);
      } else {
        item.append(text("p", "请由超级管理员核验此记录。"));
      }
      list.append(item);
    });
  }
  root.addEventListener("work-item-detail-loaded", (event) => render(event.detail?.unknown_write_recoveries));
  render(root.unknownWriteRecoveries);
})();
