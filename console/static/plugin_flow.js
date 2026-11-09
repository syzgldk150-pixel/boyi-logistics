(() => {
  "use strict";

  // Plugin flow page (AI phase 5): view, ask AI for a proposal, dry run, save.
  // The Agent validates every proposal, dry run and save; this page only displays and forwards.
  const EFFECT_TEXT = { read: "只读", compute: "计算", internal_write: "内部写入", external_write: "外部写入" };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function init() {
    const root = document.querySelector("[data-plugin-flow]");
    if (!root || root.dataset.bound === "true") return;
    root.dataset.bound = "true";
    const automationId = root.dataset.automationId;
    const base = `/automations/${encodeURIComponent(automationId)}/flow`;
    const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const $ = (selector) => root.querySelector(selector);
    const state = { data: null, proposal: null, busy: false };

    function status(text, tone = "") {
      const node = $("[data-plugin-flow-status]");
      node.textContent = text || "";
      node.dataset.tone = tone;
    }

    async function request(url, payload) {
      const options = payload === undefined ? { headers: { Accept: "application/json" } } : {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Browser-Request-UUID": window.crypto.randomUUID() },
        body: JSON.stringify(payload),
      };
      const response = await pageFetch(url, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error?.message || `HTTP ${response.status}`);
      return data.data || {};
    }

    function stepsHtml(steps, compare) {
      return steps.map((step, index) => {
        const changed = compare && JSON.stringify(compare[index] || null) !== JSON.stringify({ block: step.block, params: step.params, condition: step.condition });
        const params = Object.entries(step.params || {}).map(([key, value]) => `${escapeHtml(key)}：${escapeHtml(value)}`).join("；");
        return `<li class="plugin-flow__step${step.locked ? " is-locked" : ""}${changed ? " is-changed" : ""}">
          <span class="plugin-flow__index">${index + 1}</span>
          <div>
            <div class="plugin-flow__step-head"><strong>${escapeHtml(step.title)}</strong>
              ${step.locked ? '<span class="plugin-flow__badge"><i data-feather="lock"></i>系统保护</span>' : ""}
              ${step.effect === "external_write" ? '<span class="plugin-flow__badge is-write">外部写入</span>' : `<span class="plugin-flow__badge">${escapeHtml(EFFECT_TEXT[step.effect] || step.effect)}</span>`}
              ${changed ? '<span class="plugin-flow__badge is-changed">有改动</span>' : ""}</div>
            ${step.description ? `<p>${escapeHtml(step.description)}</p>` : ""}
            ${step.condition ? `<p class="plugin-flow__condition">条件：${escapeHtml(step.condition)}</p>` : ""}
            ${params ? `<p class="plugin-flow__params">参数：${params}</p>` : ""}
          </div></li>`;
      }).join("");
    }

    function signature(steps) {
      return steps.map((step) => ({ block: step.block, params: step.params, condition: step.condition }));
    }

    function renderCurrent() {
      const data = state.data;
      $("[data-plugin-flow-current]").innerHTML = stepsHtml(data.steps || []);
      $("[data-plugin-flow-version]").textContent = `${data.is_default ? "使用插件默认流程" : "已调整"} · 配置版本 ${data.config_version}`;
      if (window.feather) window.feather.replace();
    }

    async function load() {
      status("正在读取……");
      try {
        state.data = await request(`${base}/data`);
        renderCurrent();
        status("");
      } catch (error) {
        status(error.message || "处理流程暂时无法读取", "error");
      }
    }

    function renderProposal(result) {
      const node = $("[data-plugin-flow-proposal]");
      node.hidden = false;
      if (result.status === "out_of_scope") {
        const requirement = `【插件流程开发需求】\n插件实例：${automationId}\n需求：${$("[data-plugin-flow-instruction]").value.trim()}\n需要新增：${result.missing_capability}\nAI 说明：${result.explanation}`;
        node.innerHTML = `<div class="plugin-flow__callout is-warn"><strong>现有积木做不到</strong><p>${escapeHtml(result.missing_capability)}</p><p>${escapeHtml(result.explanation)}</p>
          <p>这需要开发新增积木，作为插件升级发布。可以把下面的需求发给开发：</p><textarea readonly rows="5" data-plugin-flow-requirement>${escapeHtml(requirement)}</textarea>
          <div class="plugin-flow__actions"><button class="ghost-btn" type="button" data-plugin-flow-copy>复制开发需求</button></div></div>`;
        return;
      }
      node.innerHTML = `<div class="plugin-flow__callout"><strong>AI 的调整方案（改动 ${result.change_count} 处，尚未保存）</strong><p>${escapeHtml(result.explanation)}</p>
          <ul>${(result.changes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
        <ol class="plugin-flow__steps">${stepsHtml(result.steps || [], signature(state.data.steps || []))}</ol>
        <div data-plugin-flow-dryrun></div>
        <div class="plugin-flow__actions">
          ${state.data.dry_run_available ? '<button class="ghost-btn" type="button" data-plugin-flow-dry><i data-feather="play-circle"></i><span>用最近的历史记录试一下</span></button>' : ""}
          <button class="primary-btn" type="button" data-plugin-flow-save><i data-feather="check"></i><span>保存为新版本</span></button>
          <button class="ghost-btn" type="button" data-plugin-flow-discard>放弃这个方案</button>
        </div>`;
      if (window.feather) window.feather.replace();
    }

    async function ask() {
      const instruction = $("[data-plugin-flow-instruction]").value.trim();
      if (!instruction) { status("请先描述想怎么调整", "warning"); return; }
      if (state.busy) return;
      state.busy = true;
      const button = $("[data-plugin-flow-ask]");
      button.disabled = true;
      status("AI 正在按积木规则调整流程……");
      try {
        state.proposal = await request(`${base}/ai-edit`, { instruction });
        renderProposal(state.proposal);
        status(state.proposal.status === "ok" ? "已生成调整方案，确认无误后再保存" : "现有积木无法满足这个要求", state.proposal.status === "ok" ? "success" : "warning");
      } catch (error) {
        status(error.message || "AI 暂时无法生成流程", "error");
      } finally {
        state.busy = false;
        button.disabled = false;
      }
    }

    async function dryRun() {
      const node = $("[data-plugin-flow-dryrun]");
      node.innerHTML = '<p class="plugin-flow__meta" aria-busy="true">正在用最近的历史记录试运行……（只计算筛选结果，不执行任何步骤）</p>';
      try {
        const result = await request(`${base}/dry-run`, { flow: state.proposal.flow });
        if (!result.available) { node.innerHTML = `<p class="plugin-flow__meta">${escapeHtml(result.message || "暂无可试运行的历史记录")}</p>`; return; }
        const rows = (result.proposed.steps || []).map((step, index) => `<tr><td>${index + 1}. ${escapeHtml((result.steps[index] || {}).title || step.block)}</td><td class="num">${step.remaining}</td></tr>`).join("");
        node.innerHTML = `<div class="plugin-flow__callout"><strong>试运行（最近 ${result.proposed.total} 条记录）</strong>
          <p>现在的流程能走到最后一步：${result.current.reach_end} 条；调整后：${result.proposed.reach_end} 条。</p>
          <table class="plugin-flow__table"><thead><tr><th>步骤</th><th>经过这一步后剩余</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      } catch (error) {
        node.innerHTML = `<p class="plugin-flow__meta">${escapeHtml(error.message || "试运行失败")}</p>`;
      }
    }

    async function save() {
      if (!window.confirm("保存后立即生效（下一次运行起按新流程处理）。可以在插件“设置”页的修改记录中恢复旧版本。确定保存？")) return;
      try {
        await request(`${base}/save`, { flow: state.proposal.flow, expected_project_configuration_version: state.data.config_version });
        $("[data-plugin-flow-proposal]").hidden = true;
        state.proposal = null;
        await load();
        status("新流程已保存并生效", "success");
      } catch (error) {
        status(error.message || "保存失败", "error");
      }
    }

    root.addEventListener("click", (event) => {
      const example = event.target.closest(".plugin-flow__example");
      if (example) $("[data-plugin-flow-instruction]").value = example.textContent.trim();
      if (event.target.closest("[data-plugin-flow-ask]")) ask();
      if (event.target.closest("[data-plugin-flow-dry]")) dryRun();
      if (event.target.closest("[data-plugin-flow-save]")) save();
      if (event.target.closest("[data-plugin-flow-discard]")) { $("[data-plugin-flow-proposal]").hidden = true; state.proposal = null; status(""); }
      if (event.target.closest("[data-plugin-flow-copy]")) {
        const text = $("[data-plugin-flow-requirement]")?.value || "";
        navigator.clipboard?.writeText(text).then(() => status("开发需求已复制", "success"), () => status("复制失败，请手动选择文字复制", "warning"));
      }
    });
    load();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
