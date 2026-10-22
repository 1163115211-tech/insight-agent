/* ==========================================================================
   问数 / Insight Agent — 前端控制器
   事件协议：docs/CONTRACT.md 第 3 节。所有节点事件都是 {node, status, data, ts}。
   ========================================================================== */
(function () {
  "use strict";

  // 时间线节点名（协议里的 node 值 -> 中文）
  var NODE_LABEL = {
    meta: "会话",
    intake: "受理",
    clarify: "确认",
    explore: "看表",
    plan: "计划",
    gen_sql: "写 SQL",
    guard: "校验",
    execute: "执行",
    repair: "修复",
    visualize: "出图",
    summarize: "结论",
  };

  var STATUS_LABEL = {
    start: "开始",
    ok: "完成",
    error: "失败",
    retry: "重试",
    interrupt: "待确认",
    reject: "已拒绝",
    done: "完成",
    running: "运行中",
  };

  // 需要显示成 tag 的状态（ok 太常见，不占位置）
  var TAG_CLASS = {
    error: "tag-err",
    reject: "tag-err",
    retry: "tag-warn",
    interrupt: "tag-wait",
    done: "tag-ok",
    running: "tag-run",
  };

  var CHART_TYPE_LABEL = { bar: "柱状图", line: "折线图", pie: "饼图", scatter: "散点图" };

  var SVG_CHEVRON =
    '<svg class="step-chevron" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
    '<path d="M6 3.5L10.5 8L6 12.5" stroke="currentColor" stroke-width="1.5" fill="none" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };

  var els = {};
  var state = {
    threadId: null,
    runId: null,
    turn: 0,
    question: "",
    received: 0,
    receiving: false,
    terminal: false,
    pending: null,
    pendingStep: null,
    lastSql: "",
    rejected: false,
    startedAt: 0,
    timer: 0,
    finalElapsed: null,
    chart: null,
    themeReady: false,
    es: null,
    stickToBottom: true,
    runningSteps: new Map(),
  };

  // ------------------------------------------------------------------
  // 小工具
  // ------------------------------------------------------------------
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function fmtInt(n) {
    if (n === null || n === undefined || n === "") return "—";
    var num = Number(n);
    if (!isFinite(num)) return String(n);
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(num);
  }

  function fmtNumber(n) {
    var num = Number(n);
    if (!isFinite(num)) return String(n);
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(num);
  }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined || !isFinite(ms)) return "—";
    if (ms < 1000) return Math.round(ms) + " ms";
    return (ms / 1000).toFixed(1) + " s";
  }

  function firstLine(text) {
    if (!text) return "";
    return String(text).split("\n").map(function (s) { return s.trim(); }).filter(Boolean)[0] || "";
  }

  function clamp(text, n) {
    var s = String(text || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function svgFragment(node) {
    var wrap = document.createElement("span");
    wrap.innerHTML = node;
    return wrap.firstElementChild;
  }

  async function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    var res = await fetch(path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
      throw new Error(detail || "HTTP " + res.status);
    }
    return data;
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  function init() {
    els.question = $("#question");
    els.askForm = $("#askForm");
    els.askHint = $("#askHint");
    els.runBtn = $("#runBtn");
    els.modeChip = $("#modeChip");
    els.modeText = $("#modeText");
    els.threadChip = $("#threadChip");
    els.threadId = $("#threadId");
    els.newThreadBtn = $("#newThreadBtn");
    els.pendingBar = $("#pendingBar");
    els.pendingText = $("#pendingText");
    els.pendingBtn = $("#pendingBtn");
    els.traceScroll = $("#traceScroll");
    els.traceEmpty = $("#traceEmpty");
    els.stepList = $("#stepList");
    els.traceCount = $("#traceCount");
    els.traceElapsed = $("#traceElapsed");
    els.liveRow = $("#liveRow");
    els.liveText = $("#liveText");
    els.liveTime = $("#liveTime");
    els.liveStatus = $("#liveStatus");
    els.sheet = $("#sheet");
    els.notice = $("#notice");
    els.noticeText = $("#noticeText");
    els.noticeClose = $("#noticeClose");
    els.emptyState = $("#emptyState");
    els.runner = $("#runner");
    els.runnerLabel = $("#runnerLabel");
    els.answerSection = $("#answerSection");
    els.questionEcho = $("#questionEcho");
    els.answerText = $("#answerText");
    els.sqlSection = $("#sqlSection");
    els.sqlCode = $("#sqlCode");
    els.sqlAttempt = $("#sqlAttempt");
    els.copySql = $("#copySql");
    els.tableSection = $("#tableSection");
    els.resultTable = $("#resultTable");
    els.tableWrap = $("#tableWrap");
    els.tableMeta = $("#tableMeta");
    els.chartSection = $("#chartSection");
    els.chart = $("#chart");
    els.chartMeta = $("#chartMeta");
    els.chartFallback = $("#chartFallback");
    els.metrics = $("#metrics");
    els.mTokens = $("#mTokens");
    els.mCost = $("#mCost");
    els.mLatency = $("#mLatency");
    els.mAttempts = $("#mAttempts");
    els.approvalDialog = $("#approvalDialog");
    els.approvalQuestion = $("#approvalQuestion");
    els.approvalBody = $("#approvalBody");
    els.approvalSql = $("#approvalSql");
    els.approvalActions = $("#approvalActions");
    els.toasts = $("#toasts");

    bind();
    loadHealth();
    updateRunBtn();
    autosize();
    els.question.focus();
  }

  function bind() {
    els.askForm.addEventListener("submit", function (e) {
      e.preventDefault();
      submitQuestion();
    });

    els.question.addEventListener("input", function () {
      autosize();
      updateRunBtn();
    });

    els.question.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        submitQuestion();
      }
    });

    Array.prototype.forEach.call(document.querySelectorAll(".pill"), function (pill) {
      pill.addEventListener("click", function () {
        els.question.value = pill.getAttribute("data-question") || pill.textContent;
        autosize();
        updateRunBtn();
        els.question.focus();
      });
    });

    els.newThreadBtn.addEventListener("click", newSession);
    els.copySql.addEventListener("click", copySql);
    els.noticeClose.addEventListener("click", hideNotice);
    els.pendingBtn.addEventListener("click", function () {
      if (state.pending) reopenApproval();
    });

    // 时间线展开 / 收起（事件委托）
    els.stepList.addEventListener("click", function (e) {
      var btn = e.target.closest(".step-btn");
      if (!btn) return;
      var expanded = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", expanded ? "false" : "true");
      var detail = btn.parentElement.querySelector(".step-detail");
      if (detail) detail.hidden = expanded;
    });

    els.traceScroll.addEventListener("scroll", function () {
      var box = els.traceScroll;
      state.stickToBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 56;
    });

    els.approvalDialog.addEventListener("close", function () {
      if (state.pending) showPendingBar();
    });
    els.approvalDialog.addEventListener("click", function (e) {
      // 点击遮罩关闭：保留 pending，顶部给出「继续处理」
      if (e.target === els.approvalDialog) els.approvalDialog.close();
    });

    window.addEventListener("resize", function () {
      if (state.chart) state.chart.resize();
    });
  }

  // ------------------------------------------------------------------
  // 模式角标 / 健康检查
  // ------------------------------------------------------------------
  async function loadHealth() {
    try {
      var h = await api("/api/health");
      setMode(h.mode, h.reason);
      if (!h.echarts) showChartMissing();
    } catch (e) {
      setMode("error", "无法连接后端：" + e.message);
    }
  }

  function setMode(mode, reason) {
    els.modeChip.setAttribute("data-mode", mode === "real" ? "real" : mode === "mock" ? "mock" : "error");
    els.modeText.textContent = mode === "real" ? "真实服务" : mode === "mock" ? "Mock 模式" : "后端未连接";
    els.modeChip.title = reason || "";
  }

  function showChartMissing() {
    els.chartFallback.hidden = false;
    els.chartFallback.textContent =
      "图表库未加载：离线环境请把 echarts.min.js 放到 web/static/ 目录后刷新（本地 /static/echarts.min.js 优先）。";
  }

  // ------------------------------------------------------------------
  // 输入区
  // ------------------------------------------------------------------
  function autosize() {
    var q = els.question;
    q.style.height = "auto";
    q.style.height = Math.min(q.scrollHeight, 132) + "px";
  }

  function updateRunBtn() {
    var hasText = els.question.value.trim().length > 0;
    els.runBtn.disabled = !hasText || state.receiving;
    els.runBtn.textContent = state.receiving ? "运行中…" : "运行";
  }

  function submitQuestion() {
    if (state.receiving) {
      toast("这一轮还在执行，等它结束再提问", "error");
      return;
    }
    if (state.pending) {
      toast("先处理顶部「待确认」的执行，再提问", "error");
      return;
    }
    var q = els.question.value.trim();
    if (!q) {
      els.question.focus();
      return;
    }
    ask(q);
  }

  // ------------------------------------------------------------------
  // 一轮提问
  // ------------------------------------------------------------------
  async function ask(question) {
    state.turn += 1;
    prepareTurn(question);
    try {
      var payload = { question: question };
      if (state.threadId) payload.thread_id = state.threadId;
      var res = await api("/api/ask", { method: "POST", body: JSON.stringify(payload) });
      state.threadId = res.thread_id;
      state.runId = res.run_id;
      state.received = 0;
      state.terminal = false;
      els.threadId.textContent = res.thread_id;
      els.threadChip.hidden = false;
      els.newThreadBtn.hidden = false;
      setRunning(true);
      connect(res.run_id, 0);
    } catch (e) {
      setRunning(false);
      showNotice("提交失败：" + e.message);
      toast("提交失败：" + e.message, "error");
    }
  }

  function prepareTurn(question) {
    state.question = question;
    state.lastSql = "";
    state.pending = null;
    state.pendingStep = null;
    state.resolvedClarifyStep = null;
    state.rejected = false;
    state.runningSteps.clear();
    hideNotice();
    hideAllSections();
    els.metrics.hidden = true;
    els.emptyState.hidden = true;
    els.runner.hidden = false;
    els.runnerLabel.textContent = "正在执行 · 等待第一个节点";

    if (state.turn === 1) {
      els.stepList.textContent = "";
      els.traceEmpty.hidden = true;
    } else {
      var divider = el("li", "turn-divider");
      divider.appendChild(el("span", "turn-text", "第 " + state.turn + " 轮 · 追问"));
      els.stepList.appendChild(divider);
    }

    els.pendingBar.hidden = true;
    resetTimer();
    stickTrace();
  }

  // ------------------------------------------------------------------
  // SSE
  // ------------------------------------------------------------------
  function connect(runId, cursor) {
    closeStream();
    var es = new EventSource("/api/events/" + encodeURIComponent(runId) + "?cursor=" + cursor);
    state.es = es;

    es.onmessage = function (e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      handleEvent(ev);
    };

    es.addEventListener("end", function (e) {
      var payload = {};
      try { payload = JSON.parse(e.data || "{}"); } catch (err) { payload = {}; }
      closeStream();
      finishRun(payload);
    });

    es.onerror = function () {
      if (state.es !== es) return;
      fallbackToStatus(runId, es);
    };
  }

  function closeStream() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }

  async function fallbackToStatus(runId, es) {
    await sleep(1200);
    if (state.es !== es || !state.receiving) return;
    try {
      var st = await api("/api/runs/" + encodeURIComponent(runId));
      if (st.finished) {
        es.close();
        state.es = null;
        finishRun({ error: st.error || null, finished: true });
      }
    } catch (e) {
      /* 后端可能已退出，保持现状 */
    }
  }

  function finishRun(payload) {
    state.receiving = false;
    setRunning(false);
    stopTimer();
    els.liveRow.hidden = true;

    if (payload && payload.error) {
      showNotice("事件流中断：" + payload.error);
      toast("运行中断：" + payload.error, "error");
      if (!state.pending) els.runner.hidden = true;
    }

    if (state.pending) {
      if (!els.approvalDialog.open) showPendingBar();
    } else if (!state.terminal) {
      showNotice("事件流提前结束，没有收到结束标记。请检查后端日志。", "warn");
    }

    if (!state.terminal && !state.pending) {
      els.runner.hidden = true;
    }
    if (state.finalElapsed !== null) {
      els.traceElapsed.textContent = fmtDuration(state.finalElapsed);
    }
    els.question.focus();
  }

  function handleEvent(ev) {
    // 真实服务恢复后会先回一条 clarify/ok；复用它之前那张「待确认」卡，避免重复
    if (ev.node === "clarify" && ev.status === "ok" && state.resolvedClarifyStep) {
      fillStep(state.resolvedClarifyStep, ev);
      markNew(state.resolvedClarifyStep);
      state.resolvedClarifyStep = null;
      state.received += 1;
      announce(ev);
      updateRunnerHint(ev);
      updateTraceHead();
      return;
    }

    state.received += 1;
    upsertStep(ev);
    updateRunnerHint(ev);
    updateTraceHead();

    var data = ev.data || {};

    if (ev.node === "meta") {
      if (ev.status === "start") {
        if (data.thread_id) {
          state.threadId = String(data.thread_id);
          els.threadId.textContent = state.threadId;
        }
        if (data.resumed) els.runnerLabel.textContent = "已恢复执行 · 等待下一个节点";
      } else if (ev.status === "done") {
        state.terminal = true;
        renderMetrics(data);
        if (data.latency_ms) state.finalElapsed = Number(data.latency_ms);
        els.runner.hidden = true;
        if (!data.interrupted) els.liveRow.hidden = true;
      } else if (ev.status === "error") {
        state.terminal = true;
        showNotice("运行失败：" + (data.error || "未知错误"));
        els.runner.hidden = true;
        els.liveRow.hidden = true;
      }
      return;
    }

    if (ev.node === "intake" && ev.status === "reject") {
      // 拒绝时后面的 summarize 会带上正式答复，这里不重复提示
      state.rejected = true;
      return;
    }

    if (ev.node === "guard" && ev.status === "error") {
      showNotice("SQL 校验未通过：" + (data.reason || "存在不允许的语句") + "，正在尝试修复", "warn");
      return;
    }

    if (ev.node === "gen_sql" && ev.status === "ok" && data.sql) {
      state.lastSql = String(data.sql);
      // 重试成功后，上一轮「执行失败」的提示条就不该继续挂着
      hideNotice();
      renderSql(state.lastSql, data.attempt);
      return;
    }

    if (ev.node === "execute") {
      if (ev.status === "error") {
        showNotice("SQL 执行失败：" + (data.error || "未知错误") + "，正在自动修复", "warn");
      } else if (ev.status === "ok") {
        hideNotice();
        renderTable(data);
      }
      return;
    }

    if (ev.node === "visualize" && ev.status === "ok" && data.chart) {
      renderChart(data);
      return;
    }

    if (ev.node === "summarize" && ev.status === "ok" && typeof data.answer === "string") {
      if (data.rejected || state.rejected) {
        // 只读门禁拒绝：不进入答案区，用一条提示条承载答复
        showNotice(String(data.answer), "warn");
      } else {
        renderAnswer(data.answer);
      }
      return;
    }

    if (ev.node === "clarify" && ev.status === "interrupt") {
      state.terminal = true;
      els.runner.hidden = true;
      state.pendingStep = findLastStep("clarify");
      openApproval(ev);
      return;
    }
  }

  function findLastStep(node) {
    var nodes = els.stepList.querySelectorAll('.step[data-node="' + node + '"]');
    return nodes.length ? nodes[nodes.length - 1] : null;
  }

  // ------------------------------------------------------------------
  // 执行轨迹
  //
  // 真实服务会为部分节点先发一条 status="start" 的进度事件，再发终态事件。
  // 这里把同一个 node 的 start 卡片原地升级为终态，保证"实时点亮"且不出现重复卡。
  // ------------------------------------------------------------------
  function upsertStep(ev) {
    if (ev.status === "start" && ev.node !== "meta") {
      var active = state.runningSteps.get(ev.node);
      var runningEv = Object.assign({}, ev, { status: "running" });
      if (active) {
        fillStep(active, runningEv);
      } else {
        var pending = createStep(runningEv);
        state.runningSteps.set(ev.node, pending);
        attachStep(pending, true);
      }
      announce(ev);
      return;
    }

    var live = state.runningSteps.get(ev.node);
    if (live) {
      state.runningSteps.delete(ev.node);
      fillStep(live, ev);
      markNew(live);
      announce(ev);
      return;
    }

    attachStep(createStep(ev), true);
    announce(ev);
  }

  function createStep(ev) {
    var li = el("li", "step");
    li.setAttribute("data-node", ev.node);
    var dot = el("span", "step-dot");
    dot.setAttribute("aria-hidden", "true");
    var btn = el("button", "step-btn");
    btn.type = "button";
    btn.setAttribute("aria-expanded", "false");
    var detail = el("div", "step-detail");
    detail.hidden = true;
    li.appendChild(dot);
    li.appendChild(btn);
    li.appendChild(detail);
    fillStep(li, ev);
    return li;
  }

  function fillStep(li, ev) {
    li.setAttribute("data-status", ev.status);
    var btn = li.querySelector(".step-btn");
    var detail = li.querySelector(".step-detail");
    var expanded = btn.getAttribute("aria-expanded") === "true";

    btn.textContent = "";
    btn.setAttribute("aria-label", (NODE_LABEL[ev.node] || ev.node) + " · " + (STATUS_LABEL[ev.status] || ev.status));

    var top = el("span", "step-top");
    top.appendChild(el("span", "step-name", NODE_LABEL[ev.node] || ev.node));

    var tagText = TAG_CLASS[ev.status] ? STATUS_LABEL[ev.status] : "";
    if (tagText) top.appendChild(el("span", "tag " + TAG_CLASS[ev.status], tagText));
    if (ev.status === "start" && ev.data && ev.data.resumed) {
      top.appendChild(el("span", "tag tag-run", "继续"));
    }

    top.appendChild(el("span", "step-time", timeLabel(ev)));
    top.appendChild(svgFragment(SVG_CHEVRON));
    btn.appendChild(top);

    var sum = summary(ev);
    if (sum) btn.appendChild(el("span", "step-sum", sum));

    detail.textContent = "";
    detail.hidden = !expanded;
    buildDetail(detail, ev);
  }

  function attachStep(li, isNew) {
    els.stepList.appendChild(li);
    if (isNew) markNew(li);
    if (state.stickToBottom) stickTrace();
  }

  function markNew(li) {
    li.classList.add("is-new");
    window.setTimeout(function () { li.classList.remove("is-new"); }, 700);
  }

  function timeLabel(ev) {
    if (!ev.ts) return "";
    var d = new Date(ev.ts * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0");
  }

  function summary(ev) {
    var d = ev.data || {};
    switch (ev.node) {
      case "meta":
        if (ev.status === "done") {
          return "总耗时 " + fmtDuration(d.latency_ms) + " · 尝试 " + fmtInt(d.attempts) + " 次";
        }
        if (ev.status === "error") return clamp(d.error || "运行失败", 90);
        if (d.resumed) return "已带着选择恢复到中断点";
        return d.thread_id ? "thread " + d.thread_id : "会话开始";
      case "intake":
        return clamp(d.reason || (d.blocked ? "拦截：" + d.blocked : ""), 90);
      case "explore": {
        var tables = Array.isArray(d.tables) ? d.tables.join(" · ") : "";
        var ddl = d.ddl_chars ? " · DDL " + fmtInt(d.ddl_chars) + " 字符" : "";
        return clamp(tables + ddl, 96);
      }
      case "plan": {
        var steps = Array.isArray(d.steps) ? d.steps : [];
        if (!steps.length) return "";
        return clamp(steps[0] + (steps.length > 1 ? " 等 " + steps.length + " 步" : ""), 96);
      }
      case "gen_sql":
        return "第 " + (d.attempt || 1) + " 次 · " + firstLine(d.sql);
      case "guard": {
        if (d.reason) return clamp(d.reason, 96);
        var checks = Array.isArray(d.checks) ? d.checks : [];
        return checks.length ? clamp(checks.join(" / "), 96) : "校验通过";
      }
      case "execute":
        if (d.error) return clamp(d.error, 96);
        return fmtInt(d.row_count) + " 行 · " + fmtDuration(d.elapsed_ms);
      case "repair":
        return clamp(d.strategy || d.error || "", 96);
      case "visualize": {
        var opt = d.chart || {};
        var series = opt.series && opt.series[0];
        var type = series && series.type ? CHART_TYPE_LABEL[series.type] || series.type : "图表";
        return type + " · ECharts option 已内联";
      }
      case "summarize":
        return clamp(d.answer || "", 96);
      case "clarify":
        if (ev.status === "interrupt") return clamp(d.question || "等待确认", 96);
        return clamp(d.answer || "已选择，继续执行", 96);
      default:
        return "";
    }
  }

  function buildDetail(container, ev) {
    var d = ev.data || {};
    var kvPairs = [];
    var lists = [];

    switch (ev.node) {
      case "meta":
        kvPairs.push(["thread", d.thread_id || state.threadId || "—"]);
        if (d.resumed) kvPairs.push(["恢复", "是（从中断点继续）"]);
        if (d.latency_ms !== undefined) kvPairs.push(["耗时", fmtDuration(d.latency_ms)]);
        if (d.attempts !== undefined) kvPairs.push(["尝试", fmtInt(d.attempts) + " 次"]);
        if (d.usage) {
          kvPairs.push(["Token", fmtInt(d.usage.prompt_tokens) + " 输入 / " + fmtInt(d.usage.completion_tokens) + " 输出"]);
          kvPairs.push(["成本", "¥" + Number(d.usage.cost_cny || 0).toFixed(4)]);
          kvPairs.push(["模型调用", fmtInt(d.usage.calls) + " 次"]);
        }
        if (ev.status === "error") kvPairs.push(["错误", d.error || "—"]);
        break;
      case "intake":
        if (d.reason) kvPairs.push(["判定", d.reason]);
        if (d.blocked) kvPairs.push(["拦截", d.blocked]);
        break;
      case "explore":
        kvPairs.push(["表", Array.isArray(d.tables) ? d.tables.join("、") : "—"]);
        if (d.ddl_chars !== undefined) kvPairs.push(["DDL 体量", fmtInt(d.ddl_chars) + " 字符"]);
        break;
      case "plan":
        if (Array.isArray(d.steps)) lists.push(d.steps);
        break;
      case "gen_sql":
        kvPairs.push(["尝试", "第 " + (d.attempt || 1) + " 次"]);
        if (d.sql) addCode(container, d.sql);
        break;
      case "guard":
        if (Array.isArray(d.checks)) lists.push(d.checks);
        if (d.reason) kvPairs.push(["结论", d.reason]);
        if (ev.status === "error") kvPairs.push(["状态", "未通过"]);
        break;
      case "execute":
        if (d.error) {
          kvPairs.push(["错误", d.error]);
        } else {
          kvPairs.push(["行数", fmtInt(d.row_count)]);
          kvPairs.push(["列数", fmtInt((d.columns || []).length)]);
          kvPairs.push(["执行耗时", fmtDuration(d.elapsed_ms)]);
        }
        break;
      case "repair":
        kvPairs.push(["第几次", "第 " + (d.attempt || 2) + " 次"]);
        if (d.error) kvPairs.push(["触发原因", d.error]);
        if (d.strategy) kvPairs.push(["修复策略", d.strategy]);
        break;
      case "visualize": {
        var opt = d.chart || {};
        var series = (opt.series && opt.series[0]) || {};
        if (series.type) kvPairs.push(["图形", CHART_TYPE_LABEL[series.type] || series.type]);
        if (Array.isArray(opt.series)) kvPairs.push(["系列数", fmtInt(opt.series.length)]);
        break;
      }
      case "summarize":
        if (d.answer) {
          var p = el("p", "detail-answer", d.answer);
          container.appendChild(p);
        }
        break;
      case "clarify":
        if (d.question) kvPairs.push(["问题", d.question]);
        if (Array.isArray(d.options) && d.options.length) kvPairs.push(["选项", d.options.join(" · ")]);
        if (d.answer) kvPairs.push(["你的选择", d.answer]);
        break;
      default:
        break;
    }

    if (kvPairs.length || lists.length) {
      var dl = el("dl", "kv");
      kvPairs.forEach(function (pair) {
        dl.appendChild(el("dt", null, pair[0]));
        dl.appendChild(el("dd", null, pair[1]));
      });
      container.appendChild(dl);
      lists.forEach(function (items) {
        var ol = el("ol");
        items.forEach(function (item) { ol.appendChild(el("li", null, String(item))); });
        container.appendChild(ol);
      });
    }

    var raw = el("details", "raw");
    raw.appendChild(el("summary", null, "原始事件"));
    var pre = el("pre");
    pre.textContent = JSON.stringify(ev, null, 2);
    raw.appendChild(pre);
    container.appendChild(raw);
  }

  function addCode(container, sql) {
    var wrap = el("div", "code-wrap");
    var pre = el("pre", "code code-sm");
    var code = el("code");
    code.appendChild(highlightSql(sql));
    pre.appendChild(code);
    wrap.appendChild(pre);
    container.appendChild(wrap);
  }

  function updateRunnerHint(ev) {
    if (els.runner.hidden) return;
    var label = NODE_LABEL[ev.node] || ev.node;
    els.runnerLabel.textContent = "正在执行 · 已完成「" + label + "」，等待下一个节点";
  }

  function updateTraceHead() {
    var steps = els.stepList.querySelectorAll(".step").length;
    els.traceCount.textContent = steps + " 步";
  }

  function announce(ev) {
    var label = NODE_LABEL[ev.node] || ev.node;
    els.liveStatus.textContent = label + " " + (STATUS_LABEL[ev.status] || ev.status);
  }

  function stickTrace() {
    els.traceScroll.scrollTop = els.traceScroll.scrollHeight;
  }

  // ------------------------------------------------------------------
  // SQL 高亮
  // ------------------------------------------------------------------
  var SQL_KEYWORDS = new Set(
    ("SELECT FROM WHERE GROUP BY ORDER HAVING LIMIT OFFSET JOIN LEFT RIGHT INNER OUTER FULL ON AS " +
      "AND OR NOT IN IS NULL WITH UNION ALL DISTINCT CASE WHEN THEN ELSE END CAST ROUND SUM COUNT AVG " +
      "MIN MAX ASC DESC BETWEEN LIKE EXISTS OVER PARTITION ROW_NUMBER RANK DENSE_RANK " +
      "COALESCE IFNULL NULLIF STRFTIME DATE SUBSTR LOWER UPPER ABS").split(" ")
  );

  var TOKEN_RE =
    /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([(),;.*+\-/%<>=!|]+)|(\s+)/g;

  function highlightSql(sql) {
    var frag = document.createDocumentFragment();
    var text = String(sql || "");
    var last = 0;
    var m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      var token = m[0];
      var cls = "";
      if (m[1]) cls = "tok-com";
      else if (m[2]) cls = "tok-str";
      else if (m[3]) cls = "tok-num";
      else if (m[4]) {
        if (SQL_KEYWORDS.has(m[4].toUpperCase())) cls = "tok-kw";
        else if (text.charAt(TOKEN_RE.lastIndex) === "(") cls = "tok-fn";
      } else if (m[5]) cls = "tok-op";
      if (cls) {
        frag.appendChild(el("span", cls, token));
      } else {
        frag.appendChild(document.createTextNode(token));
      }
      last = TOKEN_RE.lastIndex;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }

  // ------------------------------------------------------------------
  // 结果区渲染
  // ------------------------------------------------------------------
  function hideAllSections() {
    els.answerSection.hidden = true;
    els.sqlSection.hidden = true;
    els.tableSection.hidden = true;
    els.chartSection.hidden = true;
    els.chartFallback.hidden = true;
  }

  function renderSql(sql, attempt) {
    els.runner.hidden = true;
    els.sqlSection.hidden = false;
    els.sqlCode.textContent = "";
    els.sqlCode.appendChild(highlightSql(sql));
    if (attempt) {
      els.sqlAttempt.hidden = false;
      els.sqlAttempt.textContent = "第 " + attempt + " 次";
    } else {
      els.sqlAttempt.hidden = true;
    }
  }

  function renderTable(data) {
    var columns = Array.isArray(data.columns) ? data.columns : [];
    var rows = Array.isArray(data.rows) ? data.rows : [];

    els.runner.hidden = true;
    els.tableSection.hidden = false;

    var table = els.resultTable;
    table.textContent = "";
    var caption = el("caption", "sr-only", "查询结果");
    table.appendChild(caption);

    var thead = el("thead");
    var headRow = el("tr");
    var numericCols = columns.map(function (_, i) {
      var seen = 0;
      var numeric = 0;
      rows.forEach(function (row) {
        var v = Array.isArray(row) ? row[i] : row && typeof row === "object" ? row[columns[i]] : row;
        if (v === null || v === undefined || v === "") return;
        seen += 1;
        if (typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v))) numeric += 1;
      });
      return seen > 0 && numeric === seen;
    });
    columns.forEach(function (col, i) {
      var th = el("th", numericCols[i] ? "is-num" : null, String(col));
      th.setAttribute("scope", "col");
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr");
      columns.forEach(function (col, i) {
        var value;
        if (Array.isArray(row)) value = row[i];
        else if (row && typeof row === "object") value = row[col];
        else value = row;
        var td = el("td");
        if (value === null || value === undefined || value === "") {
          td.textContent = "—";
          td.className = "is-null";
        } else if (typeof value === "number") {
          td.textContent = fmtNumber(value);
          td.className = "is-num";
        } else if (typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value) && value.length < 16) {
          td.textContent = fmtNumber(value);
          td.className = "is-num";
        } else {
          td.textContent = String(value);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    els.tableMeta.textContent =
      fmtInt(rows.length) + " 行 × " + fmtInt(columns.length) + " 列" +
      (data.elapsed_ms !== undefined ? " · " + fmtDuration(data.elapsed_ms) : "") +
      (data.truncated ? " · 已截断" : "");
  }

  function renderAnswer(answer) {
    els.runner.hidden = true;
    els.answerSection.hidden = false;
    els.questionEcho.textContent = state.question;
    els.answerText.textContent = answer;
  }

  function renderMetrics(data) {
    var usage = data.usage || {};
    els.metrics.hidden = false;
    els.mTokens.textContent =
      fmtInt(usage.prompt_tokens || 0) + " / " + fmtInt(usage.completion_tokens || 0);
    els.mTokens.title = "输入 / 输出 token";
    els.mCost.textContent = "¥" + Number(usage.cost_cny || 0).toFixed(4);
    if (usage.calls !== undefined) els.mCost.title = "模型调用 " + usage.calls + " 次";
    els.mLatency.textContent = fmtDuration(data.latency_ms);
    els.mAttempts.textContent = fmtInt(data.attempts) + " 次";
  }

  // ------------------------------------------------------------------
  // ECharts
  // ------------------------------------------------------------------
  function registerChartTheme() {
    if (!window.echarts || state.themeReady) return;
    window.echarts.registerTheme("insight-dark", {
      color: ["#4c8dff", "#3fb950", "#dba33e", "#b79cff", "#5ac8fa", "#f2555a"],
      backgroundColor: "transparent",
      textStyle: { fontFamily: '"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif', color: "#a0a9b5" },
      title: { textStyle: { color: "#e8ebef", fontSize: 12, fontWeight: 500 } },
      legend: { textStyle: { color: "#a0a9b5" }, itemWidth: 8, itemHeight: 8, icon: "roundRect" },
      tooltip: {
        backgroundColor: "#151a20",
        borderColor: "#2a323b",
        borderWidth: 1,
        textStyle: { color: "#e8ebef", fontSize: 12 },
        axisPointer: { lineStyle: { color: "#3a4551" }, crossStyle: { color: "#3a4551" } },
      },
      categoryAxis: {
        axisLine: { lineStyle: { color: "#252c35" } },
        axisTick: { show: false },
        axisLabel: { color: "#7c8794", fontSize: 11 },
        splitLine: { show: false },
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: "#7c8794", fontSize: 11 },
        splitLine: { lineStyle: { color: "#1b2027" } },
      },
    });
    state.themeReady = true;
  }

  // 真实服务会把 ECharts option 里的 formatter 函数 JSON 序列化成字符串，
  // ECharts 只接受函数本体。这里做一次受控还原：只接受形如 function(){...}
  // 的字符串，并且拒绝任何触及 DOM/网络/全局对象的函数体；不允许的直接丢弃。
  var FN_STR_RE = /^function\s*\([^)]*\)\s*\{[\s\S]*\}$/;
  // 注意：不能把 function 关键字本身列进黑名单，否则所有 formatter 都会被误杀。
  var FN_UNSAFE_RE =
    /\b(document|window|globalThis|self|top|parent|location|fetch|XMLHttpRequest|WebSocket|eval|import|require|localStorage|sessionStorage|cookie|postMessage|constructor|prototype|__proto__|setTimeout|setInterval)\b|\bnew\s+Function\b/i;

  function reviveFormatters(value, depth) {
    if (!value || typeof value !== "object" || depth > 8) return value;
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i += 1) reviveFormatters(value[i], depth + 1);
      return value;
    }
    Object.keys(value).forEach(function (key) {
      var v = value[key];
      if (typeof v === "string") {
        var s = v.trim();
        if (FN_STR_RE.test(s)) {
          if (FN_UNSAFE_RE.test(s)) {
            delete value[key];
            return;
          }
          try {
            value[key] = new Function("return (" + s + ")")();
          } catch (e) {
            delete value[key];
          }
        }
      } else if (v && typeof v === "object") {
        reviveFormatters(v, depth + 1);
      }
    });
    return value;
  }

  function ensureChart() {
    if (state.chart) return state.chart;
    registerChartTheme();
    if (!els.chart.clientHeight) els.chart.style.height = "320px";
    state.chart = window.echarts.init(els.chart, "insight-dark", { renderer: "canvas" });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { if (state.chart) state.chart.resize(); }).observe(els.chart);
    }
    return state.chart;
  }

  function renderChart(data) {
    var option = data.chart;
    if (option) reviveFormatters(option, 0);
    var series = (option && option.series && option.series[0]) || {};
    var type = series.type || (data.chart_type ? String(data.chart_type) : "");
    els.chartMeta.textContent = (CHART_TYPE_LABEL[type] || type || "图表") +
      (Array.isArray(option && option.series) ? " · " + option.series.length + " 个系列" : "");
    els.chartSection.hidden = false;
    els.runner.hidden = true;

    if (!window.echarts) {
      showChartMissing();
      return;
    }

    try {
      // 同步初始化：不给 background tab 的 rAF 节流留失败空间
      var chart = ensureChart();
      chart.setOption(option, true);
      chart.resize();
    } catch (e) {
      showChartMissing();
      els.chartFallback.hidden = false;
      els.chartFallback.textContent = "图表渲染失败：" + e.message;
      return;
    }
    els.chart.setAttribute("aria-label", (CHART_TYPE_LABEL[type] || "数据") + "图表");
  }

  // ------------------------------------------------------------------
  // 审批中断
  // ------------------------------------------------------------------
  function openApproval(ev) {
    state.pending = ev;
    var d = ev.data || {};
    els.approvalQuestion.textContent = d.question || "继续执行？";

    if (state.lastSql && state.lastSql.trim()) {
      els.approvalBody.hidden = false;
      els.approvalSql.textContent = "";
      els.approvalSql.appendChild(highlightSql(state.lastSql));
    } else {
      els.approvalBody.hidden = true;
    }

    var options = Array.isArray(d.options) && d.options.length ? d.options : ["继续执行", "取消"];
    els.approvalActions.textContent = "";
    options.forEach(function (label, index) {
      var btn = el("button", "btn " + (index === 0 ? "btn-primary" : "btn-quiet"), String(label));
      btn.type = "button";
      btn.addEventListener("click", function () { decide(String(label)); });
      els.approvalActions.appendChild(btn);
    });

    if (d.thread_id) {
      state.threadId = String(d.thread_id);
      els.threadId.textContent = state.threadId;
    }
    els.pendingBar.hidden = true;
    els.liveRow.hidden = true;
    stopTimer();
    els.approvalDialog.setAttribute("tabindex", "-1");
    if (!els.approvalDialog.open) els.approvalDialog.showModal();
    // 焦点落在对话框本身（不是按钮），避免主按钮出现"双环"焦点样式；Tab 仍能到按钮
    els.approvalDialog.focus();
    announce({ node: "clarify", status: "interrupt" });
  }

  function reopenApproval() {
    if (!state.pending) return;
    els.pendingBar.hidden = true;
    if (!els.approvalDialog.open) els.approvalDialog.showModal();
    els.approvalDialog.focus();
  }

  function showPendingBar() {
    var d = (state.pending && state.pending.data) || {};
    els.pendingText.textContent = clamp(d.question || "有一个执行确认在等你处理", 110);
    els.pendingBar.hidden = false;
  }

  async function decide(value) {
    var pending = state.pending;
    if (!pending) return;
    var threadId = state.threadId || (pending.data && pending.data.thread_id);
    state.pending = null;

    // 把时间线上那张「待确认」卡原地改成已选择，保持轨迹与事实一致
    if (state.pendingStep) {
      fillStep(
        state.pendingStep,
        Object.assign({}, pending, {
          status: "ok",
          data: Object.assign({}, pending.data, { answer: value }),
        })
      );
      state.resolvedClarifyStep = state.pendingStep;
      state.pendingStep = null;
    }

    if (els.approvalDialog.open) els.approvalDialog.close();
    els.pendingBar.hidden = true;
    if (!threadId) {
      toast("缺少 thread_id，无法继续", "error");
      return;
    }

    state.received = 0;
    state.terminal = false;
    state.finalElapsed = null;
    els.runner.hidden = true;
    els.liveRow.hidden = false;
    resetTimer();
    setRunning(true);

    try {
      var res = await api("/api/resume", {
        method: "POST",
        body: JSON.stringify({ thread_id: threadId, value: value }),
      });
      state.runId = res.run_id;
      if (res.thread_id) {
        state.threadId = res.thread_id;
        els.threadId.textContent = state.threadId;
      }
      toast("已选择「" + clamp(value, 24) + "」，继续执行", "ok");
      connect(res.run_id, 0);
    } catch (e) {
      setRunning(false);
      stopTimer();
      showNotice("继续执行失败：" + e.message);
      toast("继续执行失败：" + e.message, "error");
    }
  }

  // ------------------------------------------------------------------
  // 运行状态 / 计时 / 提示
  // ------------------------------------------------------------------
  function setRunning(running) {
    state.receiving = running;
    els.liveRow.hidden = !running;
    els.liveText.textContent = "正在执行 · 已收到 " + state.received + " 个节点";
    updateRunBtn();
    if (running) {
      state.startedAt = Date.now();
      els.traceElapsed.textContent = "0.0s";
      state.timer = window.setInterval(tick, 100);
    } else {
      stopTimer();
    }
  }

  function tick() {
    var ms = Date.now() - state.startedAt;
    els.traceElapsed.textContent = fmtDuration(ms);
    els.liveTime.textContent = fmtDuration(ms);
    els.liveText.textContent = "正在执行 · 已收到 " + state.received + " 个节点";
  }

  function resetTimer() {
    stopTimer();
    state.finalElapsed = null;
    els.traceElapsed.textContent = "0.0s";
    els.liveTime.textContent = "";
  }

  function stopTimer() {
    if (state.timer) {
      window.clearInterval(state.timer);
      state.timer = 0;
    }
  }

  function showNotice(text, tone) {
    els.notice.hidden = false;
    els.notice.setAttribute("data-tone", tone === "warn" ? "warn" : "error");
    els.noticeText.textContent = text;
  }

  function hideNotice() {
    els.notice.hidden = true;
  }

  function toast(text, tone) {
    var node = el("div", "toast");
    node.setAttribute("data-tone", tone === "error" ? "error" : tone === "ok" ? "ok" : "info");
    node.appendChild(el("span", "toast-dot"));
    node.appendChild(el("span", "toast-text", text));
    els.toasts.appendChild(node);
    window.setTimeout(function () {
      node.style.opacity = "0";
      node.style.transform = "translateY(4px)";
      node.style.transition = "opacity .18s ease, transform .18s ease";
      window.setTimeout(function () { node.remove(); }, 220);
    }, tone === "error" ? 5200 : 3200);
  }

  // ------------------------------------------------------------------
  // 复制 / 新会话
  // ------------------------------------------------------------------
  async function copySql() {
    var text = els.sqlCode.textContent || "";
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      var label = els.copySql.querySelector("span");
      if (label) {
        label.textContent = "已复制";
        window.setTimeout(function () { label.textContent = "复制"; }, 1300);
      }
    } catch (e) {
      toast("复制失败，请手动选中 SQL", "error");
    }
  }

  function newSession() {
    closeStream();
    stopTimer();
    state.threadId = null;
    state.runId = null;
    state.turn = 0;
    state.received = 0;
    state.receiving = false;
    state.terminal = false;
    state.pending = null;
    state.pendingStep = null;
    state.resolvedClarifyStep = null;
    state.lastSql = "";
    state.question = "";
    state.finalElapsed = null;
    state.rejected = false;
    state.runningSteps.clear();

    if (els.approvalDialog.open) els.approvalDialog.close();
    els.pendingBar.hidden = true;
    els.stepList.textContent = "";
    els.traceEmpty.hidden = false;
    els.liveRow.hidden = true;
    els.traceCount.textContent = "0 步";
    els.traceElapsed.textContent = "—";
    els.threadChip.hidden = true;
    els.newThreadBtn.hidden = true;
    els.emptyState.hidden = false;
    els.runner.hidden = true;
    hideAllSections();
    hideNotice();
    els.metrics.hidden = true;
    els.chartFallback.hidden = true;
    if (state.chart) {
      state.chart.clear();
    }
    els.question.value = "";
    autosize();
    updateRunBtn();
    els.question.focus();
    toast("已开始新会话", "ok");
  }

  init();
})();
