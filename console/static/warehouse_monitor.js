(function () {
  "use strict";

  const form = document.querySelector("[data-wm-form]");
  if (!form) return;
  const codeInput = document.querySelector("[data-wm-code]");
  const submitBtn = document.querySelector("[data-wm-submit]");
  const feedback = document.querySelector("[data-wm-feedback]");
  const result = document.querySelector("[data-wm-result]");
  const summary = document.querySelector("[data-wm-summary]");
  const locationText = document.querySelector("[data-wm-location]");
  const rowsBody = document.querySelector("[data-wm-rows]");

  function requestJson(url) {
    return fetch(url, { headers: { Accept: "application/json", "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok || body.ok === false) throw new Error(body.message || "请求失败（" + response.status + "）");
          return body.data || {};
        });
      });
  }

  function showFeedback(text, kind) {
    feedback.textContent = text;
    feedback.dataset.kind = kind || "info";
    feedback.hidden = !text;
  }

  function summaryItem(label, value) {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    wrap.append(dt, dd);
    return wrap;
  }

  // 录像在仓库局域网内播放：服务器只给出签名入口，这里追加单号与扫码时间。
  function videoAnchor(link, row) {
    const anchor = document.createElement("a");
    anchor.className = "ghost-btn";
    anchor.href = link + "&" + new URLSearchParams({ code: row.raw_code, scan: row.scanned_at }).toString();
    anchor.target = "warehouse-video";
    anchor.rel = "noopener";
    anchor.textContent = "看录像";
    return anchor;
  }

  function renderResult(data) {
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const link = typeof data.video_link === "string" ? data.video_link : "";
    const timed = rows.filter(function (row) { return row.scanned_at; });
    const destinations = Array.from(new Set(rows.map(function (row) { return row.destination; }).filter(Boolean)));
    const earliest = timed.map(function (row) { return row.scanned_at; }).sort()[0] || "";
    const list = document.createElement("dl");
    list.className = "wm-summary";
    list.append(
      summaryItem("主单号", data.main_tracking || "—"),
      summaryItem("扫描条码数", String(rows.length)),
      summaryItem("目的网点", destinations.join("、") || "—"),
      summaryItem("最早扫描时间", earliest || "未记录")
    );
    summary.replaceChildren(list);
    locationText.textContent = "目的网点：" + (destinations.join("、") || "—") + "。库区对应关系尚未配置，暂不推定库区。";

    rowsBody.replaceChildren();
    rows.forEach(function (row) {
      const tr = document.createElement("tr");
      [row.raw_code, row.code_type === "child" ? "子单" : "主单", row.destination || "—", row.snapshot_date].forEach(function (value) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      });
      const timeCell = document.createElement("td");
      timeCell.textContent = row.scanned_at || "未记录";
      if (!row.scanned_at) timeCell.className = "wm-missing";
      const actionCell = document.createElement("td");
      if (row.scanned_at && link) actionCell.append(videoAnchor(link, row));
      tr.append(timeCell, actionCell);
      rowsBody.append(tr);
    });
    result.hidden = false;
    if (!rows.length) {
      showFeedback("最近 " + (data.lookback_days || 60) + " 天没有找到这个单号的扫描记录。", "info");
    } else if (!timed.length) {
      showFeedback("找到扫描记录，但没有记录具体扫描时间：扫描时间从扫描插件 2.0.3 上线后才开始保存。", "info");
    } else if (!link) {
      showFeedback("服务器未配置仓库录像链接，只能查看扫描时间。", "info");
    } else {
      showFeedback("", "info");
    }
  }

  function search(code) {
    submitBtn.disabled = true;
    showFeedback("正在查询扫描记录…", "info");
    requestJson("/warehouse-monitor/lookup?code=" + encodeURIComponent(code)).then(function (data) {
      renderResult(data);
      const url = new URL(window.location.href);
      url.searchParams.set("code", code);
      window.history.replaceState(null, "", url);
    }).catch(function (error) {
      result.hidden = true;
      showFeedback(error.message, "error");
    }).finally(function () {
      submitBtn.disabled = false;
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    const code = codeInput.value.trim();
    if (!/^[0-9A-Za-z]{6,32}$/.test(code)) {
      showFeedback("请输入 6–32 位字母或数字的单号。", "error");
      codeInput.focus();
      return;
    }
    search(code);
  });

  if (codeInput.value.trim()) form.requestSubmit();
})();
