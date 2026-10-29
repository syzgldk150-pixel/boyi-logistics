(() => {
  "use strict";
  const root = document.querySelector("[data-cp-detail-page]");
  const section = root?.querySelector("[data-unknown-write-section]");
  if (!section) return;
  const list = section.querySelector("[data-unknown-write-list]");

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
        item.append(text("p", "历史写入范围缺失；不表示已成功。"));
      }
      item.append(text("p", row.recovery_unavailable_reason || "历史写入记录只读保留，不再核验或重放。"));
      list.append(item);
    });
  }
  root.addEventListener("work-item-detail-loaded", (event) => render(event.detail?.unknown_write_recoveries));
  render(root.unknownWriteRecoveries);
})();
