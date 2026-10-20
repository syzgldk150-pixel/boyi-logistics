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
  const player = document.querySelector("[data-wm-player]");
  const playerTitle = document.querySelector("[data-wm-player-title]");
  const startLabel = document.querySelector("[data-wm-start]");
  const secondsSelect = document.querySelector("[data-wm-seconds]");
  const video = document.querySelector("[data-wm-video]");
  const videoMessage = document.querySelector("[data-wm-video-message]");
  const statusBox = document.querySelector("[data-wm-video-status]");
  const statusText = document.querySelector("[data-wm-video-status-text]");
  const channelButtons = Array.from(document.querySelectorAll("[data-wm-channel]"));

  const PRE_ROLL_SECONDS = 20;
  const STATUS_TEXT = {
    online: "录像服务在线",
    offline: "录像服务离线：仓库笔记本可能关机、休眠或不在仓库网络",
    nvr_unreachable: "录像服务在线，但连不上录像机",
    not_configured: "服务器未配置录像服务",
  };
  const state = { start: null, channel: Number(channelButtons[0] && channelButtons[0].dataset.wmChannel), code: "", objectUrl: "", loadId: 0, controller: null };

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

  function parseLocal(text) {
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/.exec(text || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
  }

  function pad(n) { return String(n).padStart(2, "0"); }
  function formatLocal(date, sep) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + (sep || "T") +
      pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function checkVideoStatus() {
    requestJson("/warehouse-monitor/video-status").then(function (data) {
      statusBox.dataset.state = data.status;
      statusText.textContent = STATUS_TEXT[data.status] || "录像服务状态未知";
    }).catch(function () {
      statusBox.dataset.state = "offline";
      statusText.textContent = "无法检查录像服务状态";
    });
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

  function renderResult(data) {
    const rows = Array.isArray(data.rows) ? data.rows : [];
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
      if (row.scanned_at) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ghost-btn";
        button.textContent = "看录像";
        button.addEventListener("click", function () { openPlayer(row); });
        actionCell.append(button);
      }
      tr.append(timeCell, actionCell);
      rowsBody.append(tr);
    });
    result.hidden = false;
    if (!rows.length) {
      showFeedback("最近 " + (data.lookback_days || 60) + " 天没有找到这个单号的扫描记录。", "info");
    } else if (!timed.length) {
      showFeedback("找到扫描记录，但没有记录具体扫描时间：扫描时间从扫描插件 2.0.3 上线后才开始保存。", "info");
    } else {
      showFeedback("", "info");
      openPlayer(timed.slice().sort(function (a, b) { return a.scanned_at < b.scanned_at ? -1 : 1; })[0]);
    }
  }

  function search(code) {
    state.code = code;
    submitBtn.disabled = true;
    showFeedback("正在查询扫描记录…", "info");
    requestJson("/warehouse-monitor/lookup?code=" + encodeURIComponent(code)).then(function (data) {
      renderResult(data);
      const url = new URL(window.location.href);
      url.searchParams.set("code", code);
      window.history.replaceState(null, "", url);
    }).catch(function (error) {
      result.hidden = true;
      player.hidden = true;
      showFeedback(error.message, "error");
    }).finally(function () {
      submitBtn.disabled = false;
    });
  }

  function openPlayer(row) {
    const scanned = parseLocal(row.scanned_at);
    if (!scanned) return;
    state.start = new Date(scanned.getTime() - PRE_ROLL_SECONDS * 1000);
    playerTitle.textContent = row.raw_code + " · 扫描于 " + row.scanned_at;
    player.hidden = false;
    loadClip();
    player.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function showVideoMessage(text) {
    videoMessage.textContent = text;
    videoMessage.hidden = !text;
  }

  // 逐块读取并显示传输进度：ECS 出口带宽有限，整段下载完之前用户需要知道还在传。
  function readClip(response, loadId) {
    const total = Number(response.headers.get("Content-Length")) || 0;
    if (!response.body || !total) return response.blob();
    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    function pump() {
      return reader.read().then(function (step) {
        if (step.done) return new Blob(chunks, { type: "video/mp4" });
        chunks.push(step.value);
        received += step.value.length;
        if (loadId === state.loadId) {
          showVideoMessage("正在传输录像… " + Math.floor(received * 100 / total) + "%（" +
            (received / 1048576).toFixed(1) + " / " + (total / 1048576).toFixed(1) + " MB）");
        }
        return pump();
      });
    }
    return pump();
  }

  function loadClip() {
    if (!state.start) return;
    const loadId = ++state.loadId;
    // 切换通道或时间时取消上一段，避免旧请求继续占用录像机和服务器带宽。
    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    state.controller = controller;
    startLabel.textContent = formatLocal(state.start, " ");
    channelButtons.forEach(function (button) {
      button.setAttribute("aria-selected", String(Number(button.dataset.wmChannel) === state.channel));
    });
    video.removeAttribute("src");
    video.load();
    showVideoMessage("正在从录像机取录像…");
    const params = new URLSearchParams({ channel: String(state.channel), start: formatLocal(state.start), seconds: secondsSelect.value });
    fetch("/warehouse-monitor/clip?" + params.toString(), { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin", signal: controller.signal })
      .then(function (response) {
        const type = response.headers.get("Content-Type") || "";
        if (response.ok && type.indexOf("video/mp4") === 0) return readClip(response, loadId);
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.message || "录像读取失败（" + response.status + "）");
        });
      })
      .then(function (blob) {
        if (loadId !== state.loadId) return;
        if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
        state.objectUrl = URL.createObjectURL(blob);
        video.src = state.objectUrl;
        showVideoMessage("");
        video.play().catch(function () {});
      })
      .catch(function (error) {
        if (loadId !== state.loadId) return;
        showVideoMessage(error.message.replace(/[。.]+$/, "") + "。也可在 iVMS 中查看该通道 " + formatLocal(state.start, " ") + " 前后的录像。");
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
  channelButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      state.channel = Number(button.dataset.wmChannel);
      loadClip();
    });
  });
  document.querySelectorAll("[data-wm-shift]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!state.start) return;
      const next = new Date(state.start.getTime() + Number(button.dataset.wmShift) * 1000);
      if (next > new Date()) return;
      state.start = next;
      loadClip();
    });
  });
  secondsSelect.addEventListener("change", loadClip);

  checkVideoStatus();
  if (codeInput.value.trim()) form.requestSubmit();
})();
