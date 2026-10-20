"use strict";
(() => {
  let applyCurrentFilter = () => {};
  let pendingRefresh = false;
  let refreshing = false;
  let polling = false;
  let writes = 0;
  window.addEventListener("popstate", () => applyCurrentFilter());
  const hasDraft = () => Array.from(document.querySelectorAll(".review-form")).some(form =>
    !form.querySelector("[data-review-fields]").hidden &&
    ["defect_url", "note"].some(name => form.elements.namedItem(name).value.length > 0));
  const deferRefresh = () => {
    const notice = document.getElementById("refresh-message");
    if (notice) {
      notice.textContent = "有新数据，提交或取消后刷新";
      notice.hidden = false;
    }
  };
  const refreshPage = async () => {
    if (!pendingRefresh || refreshing) return;
    if (hasDraft() || writes) {deferRefresh(); return;}
    refreshing = true;
    try {
      const response = await fetch(window.location.href, {cache: "no-store"});
      if (!response.ok) return;
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = parsed.querySelector("main[data-state-url]");
      if (!next) return;
      if (hasDraft() || writes) {deferRefresh(); return;}
      const opened = Array.from(document.querySelectorAll("details[data-disclosure][open]"), node => node.dataset.disclosure);
      next.querySelectorAll("details[data-disclosure]").forEach(node => {
        if (opened.includes(node.dataset.disclosure)) node.setAttribute("open", "");
      });
      document.querySelector("main").replaceWith(next);
      pendingRefresh = false;
      initialize();
    } catch {
      // Keep the current page and retry on the next poll.
    } finally {refreshing = false;}
  };
  const initialize = () => {
    applyCurrentFilter = () => {};
    const jiraBase = document.querySelector("[data-jira-url]")?.dataset.jiraUrl || "";
    const hydrateJira = root => {
      if (!jiraBase) return;
      const prefix = jiraBase + "/browse/";
      root.querySelectorAll("a[href]").forEach(async link => {
        const href = link.getAttribute("href");
        if (!href.startsWith(prefix) || link.dataset.jiraLoaded) return;
        const match = href.slice(prefix.length).match(/^([A-Z][A-Z0-9_]*-[0-9]+)(?:[?#].*)?$/);
        if (!match) return;
        link.dataset.jiraLoaded = "true";
        const summary = document.createElement("span");
        summary.setAttribute("data-jira-summary", match[1]);
        summary.setAttribute("aria-live", "polite");
        summary.dataset.state = "loading";
        link.after(summary);
        try {
          const response = await fetch("/api/v1/jira/" + match[1], {headers: {Accept: "application/json"}});
          const data = await response.json();
          if (!response.ok || !data.ok || typeof data.summary !== "string" || typeof data.status !== "string") throw new Error();
          summary.textContent = " " + data.summary + " · " + data.status;
          summary.dataset.state = "success";
        } catch {
          summary.textContent = " Jira 读取失败";
          summary.dataset.state = "error";
        }
      });
    };
    hydrateJira(document);
    const identity = document.getElementById("review-identity");
    if (!identity) return;
    const marker = identity.querySelector("[name='marker']");
    const token = identity.querySelector("[name='token']");
    const adminToken = identity.querySelector("[name='admin-token']");
    const focusIdentity = field => {
      identity.querySelector("details")?.setAttribute("open", "");
      field?.focus();
    };
    const show = (element, value, state = "success") => {
      element.textContent = value;
      element.dataset.state = state;
    };
    const saveIdentity = () => {
      localStorage.setItem("sop-board.marker", marker.value.trim());
      localStorage.setItem("sop-board.token", token.value);
      if (adminToken) localStorage.setItem("sop-board.admin-token", adminToken.value);
    };
    marker.value = localStorage.getItem("sop-board.marker") || "";
    token.value = localStorage.getItem("sop-board.token") || "";
    if (adminToken) adminToken.value = localStorage.getItem("sop-board.admin-token") || "";
    for (const event of ["input", "change"]) {
      marker.addEventListener(event, saveIdentity);
      token.addEventListener(event, saveIdentity);
      if (adminToken) adminToken.addEventListener(event, saveIdentity);
    }
    document.getElementById("save-identity").addEventListener("click", () => {
      saveIdentity();
      show(document.getElementById("identity-message"), "标记人与令牌已保存，可随时更换。");
    });
    document.querySelectorAll("[data-developer-action]").forEach(button => {
      button.addEventListener("click", async () => {
        const message = document.getElementById("developer-message");
        if (!adminToken?.value.trim()) {
          show(message, "请先在身份栏填写开发者令牌", "error");
          focusIdentity(adminToken);
          return;
        }
        saveIdentity();
        button.disabled = true;
        try {
          const response = await fetch(button.dataset.developerAction, {
            method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + adminToken.value},
            body: button.dataset.developerNote !== undefined ? JSON.stringify({note: document.getElementById("developer-note")?.value || ""}) : "{}",
          });
          const data = await response.json();
          if (!response.ok || !data.ok) {
            show(message, (data.error_code || "操作失败") + "：" + (data.problems || []).map(p => p.problem).join("；"), "error");
          } else if (typeof data.paused === "boolean") {
            const notice = document.getElementById("automation-state");
            notice.textContent = data.paused ? "自动任务已暂停" : "";
            notice.hidden = !data.paused;
            button.dataset.developerAction = "/api/v1/automation/" + (data.paused ? "resume" : "pause");
            button.textContent = data.paused ? "恢复自动任务" : "暂停自动任务";
            [...document.querySelectorAll("[data-agent-state]"), ...document.querySelectorAll("#agent-state")].forEach(state => {
              state.textContent = state.textContent.replace(/ · 已暂停$/, "") + (data.paused ? " · 已暂停" : "");
            });
            show(message, data.paused ? "自动任务已暂停" : "自动任务已恢复");
          } else {
            show(message, "任务 #" + data.task.id + "：" + data.task.status + "。刷新页面可查看最新状态。");
          }
        } catch {
          show(message, "连接失败，请重试。", "error");
        } finally {button.disabled = false;}
      });
    });
    const filterLinks = document.querySelectorAll("[data-filter]");
    const applyFilter = () => {
      const requested = new URL(window.location.href).searchParams.get("filter") || "all";
      const active = Array.from(filterLinks).some(link => link.dataset.filter === requested) ? requested : "all";
      document.querySelectorAll(".scenario-card").forEach(card => {
        card.hidden = active === "unreviewed" ? card.dataset.unreviewed !== "true" :
          active === "agent-pass" ? card.dataset.agentPass !== "true" :
          ["fail", "blocked"].includes(active) ? card.dataset.status !== active : false;
      });
      document.querySelectorAll(".scenario-node").forEach(node => {
        node.hidden = !Array.from(node.querySelectorAll(".scenario-card")).some(card => !card.hidden);
      });
      filterLinks.forEach(link => {
        if (link.dataset.filter === active) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
    };
    if (filterLinks.length) {
      filterLinks.forEach(link => link.addEventListener("click", event => {
        event.preventDefault();
        const url = new URL(window.location.href);
        url.searchParams.set("filter", link.dataset.filter);
        window.history.pushState(null, "", url);
        applyFilter();
      }));
      applyCurrentFilter = applyFilter;
      applyFilter();
    }
    const statusNames = {pass: "通过", fail: "不通过（失败）", blocked: "不通过（受阻）", skip: "跳过"};
    const queueNames = {pending: "待执行", issues: "问题", retest: "复测", reviewed: "已复核通过"};
    const element = (tag, value) => {
      const node = document.createElement(tag);
      if (value !== undefined) node.textContent = value;
      return node;
    };
    const uploadForm = document.getElementById("cases-upload");
    if (uploadForm) {
      const fileInput = uploadForm.elements.namedItem("file");
      const message = uploadForm.querySelector("[data-cases-message]");
      const problems = uploadForm.querySelector("[data-cases-problems]");
      const submit = uploadForm.querySelector("button[type='submit']");
      let pending = false;
      uploadForm.addEventListener("submit", async event => {
        event.preventDefault();
        if (pending) return;
        problems.replaceChildren();
        if (!marker.value.trim()) {
          show(message, "请先在身份栏填写标记人", "error");
          focusIdentity(marker);
          return;
        }
        if (!token.value.trim()) {
          show(message, "请先在身份栏填写写入令牌", "error");
          focusIdentity(token);
          return;
        }
        const file = fileInput.files[0];
        if (!file) {
          show(message, "请选择用例 YAML 文件", "error");
          fileInput.focus();
          return;
        }
        pending = true;
        submit.disabled = true;
        saveIdentity();
        try {
          const content = await file.text();
          const response = await fetch(uploadForm.action, {method: "POST",
            headers: {"Content-Type": "application/json", Authorization: "Bearer " + token.value},
            body: JSON.stringify({filename: file.name, content, uploaded_by: marker.value.trim()})});
          const data = await response.json();
          if (response.ok && data.ok) {
            show(message, data.unchanged_upload ? "原文未变，保留已有上传。" : "用例上传成功。");
            window.location.reload();
          } else {
            show(message, data.error_code || "上传失败", "error");
            for (const problem of data.problems || []) {
              const position = problem.line == null ? "文件" : "第 " + problem.line + " 行";
              problems.appendChild(element("li", problem.field + " · " + position + "：" + problem.problem));
            }
          }
        } catch {
          show(message, "文件读取或网络请求失败，请核对上传记录后重试。", "error");
        } finally {
          pending = false;
          submit.disabled = false;
        }
      });
    }
    const resultTime = result => {
      const node = element("time", result.executed_at_display);
      node.setAttribute("datetime", result.executed_at);
      return node;
    };
    const manualContent = result => {
      const container = element("div");
      const heading = element("p", "人工 · " + statusNames[result.status] + " · 标记人：" + result.executor + " · ");
      heading.appendChild(resultTime(result));
      container.appendChild(heading);
      if (result.finding) {
        const link = element("a", result.finding);
        link.setAttribute("href", result.finding);
        container.appendChild(link);
      }
      if (result.note) container.appendChild(element("p", "备注：" + result.note));
      return container;
    };
    const updateScenario = (card, item, filters) => {
      card.dataset.status = item.status || "";
      card.dataset.unreviewed = String(!item.latest_manual);
      card.dataset.agentPass = String(!!item.latest_agent && item.latest_agent.status === "pass");
      card.querySelector("[data-latest-manual]").replaceChildren(manualContent(item.latest_manual));
      card.querySelector("[data-queue]").textContent = "所在队列：" + queueNames[item.queue];
      filterLinks.forEach(link => {link.querySelector("span").textContent = String(filters[link.dataset.filter]);});
      if (filterLinks.length) applyFilter();
      // Only details have a result-history container; list cards keep their compact summary.
      const group = document.querySelector("section[data-fingerprint='" + item.fingerprint + "']");
      const history = group && group.querySelector("[data-result-history]");
      if (history) {
        const result = item.latest_manual;
        const article = element("article");
        article.className = "result";
        article.appendChild(element("h3", "第 " + result.number + " 轮 · 人工 · " + statusNames[result.status]));
        article.appendChild(manualContent(result));
        article.appendChild(element("p", "实际结果：" + result.actual));
        if (result.submitted_at_display) article.appendChild(element("p", "提交时间：" + result.submitted_at_display));
        (result.evidence || []).forEach(evidence => {
          const link = element("a", "下载 " + evidence.name);
          link.setAttribute("href", "/api/v1/evidence/" + evidence.sha256);
          link.setAttribute("download", evidence.name);
          article.appendChild(link);
        });
        const empty = history.querySelector("[data-no-results]");
        if (empty) empty.remove();
        history.appendChild(article);
        hydrateJira(history);
      }
      hydrateJira(card);
    };
    document.querySelectorAll(".review-form").forEach(form => {
      const field = name => form.elements.namedItem(name);
      const message = form.querySelector("[data-review-message]");
      const fields = form.querySelector("[data-review-fields]");
      const buttons = form.querySelectorAll("[data-mark-status]");
      let selected = null;
      let pending = false;
      const checkIdentity = () => {
        if (marker.value.trim() && token.value.trim()) return true;
        show(message, "请先在身份栏填写标记人与写入令牌", "error");
        focusIdentity(marker.value.trim() ? token : marker);
        return false;
      };
      const collapse = () => {
        selected = null;
        fields.hidden = true;
        field("defect_url").value = "";
        field("note").value = "";
        buttons.forEach(button => button.setAttribute("aria-expanded", "false"));
      };
      const submitMark = async status => {
        if (pending || !checkIdentity()) return;
        if (["fail", "blocked"].includes(status) && !field("defect_url").value.trim()) {
          show(message, "请填写缺陷链接", "error");
          field("defect_url").focus();
          return;
        }
        if (!["pass", "fail", "blocked"].includes(status)) return;
        saveIdentity();
        const payload = {scenario: field("scenario").value, fingerprint: field("fingerprint").value,
          status, marker: marker.value.trim()};
        if (status !== "pass") {
          payload.defect_url = field("defect_url").value.trim();
          if (field("note").value) payload.note = field("note").value;
        }
        pending = true;
        writes += 1;
        const controls = form.querySelectorAll("button");
        controls.forEach(button => {button.disabled = true;});
        try {
          const response = await fetch(form.action, {method: "POST",
            headers: {"Content-Type": "application/json", Authorization: "Bearer " + token.value},
            body: JSON.stringify(payload)});
          const data = await response.json();
          if (response.ok && data.ok) {
            updateScenario(form.closest(".scenario-card"), data.scenario, data.filters);
            collapse();
            show(message, "标记已保存为第 " + data.round + " 轮。");
          } else {
            const unauthorized = response.status === 401 || data.error_code === "unauthorized";
            const code = data.error_code || (unauthorized ? "unauthorized" : "");
            const reasons = (data.problems || []).map(problem => problem.problem);
            const explanation = [code, ...reasons].filter(Boolean).join("；");
            if (unauthorized) {
              show(message, explanation + "；令牌无效，请重新填写写入令牌。已填内容已保留。", "error");
              focusIdentity(token);
            } else {
              show(message, explanation || "标记未保存，请核对填写内容。", "error");
            }
          }
        } catch {
          show(message, "网络请求失败，内容已保留。请先核对是否已保存，再决定是否重试。", "error");
        } finally {
          pending = false;
          writes -= 1;
          controls.forEach(button => {button.disabled = false;});
          await refreshPage();
        }
      };
      buttons.forEach(button => button.addEventListener("click", () => {
        if (pending || !checkIdentity()) return;
        selected = button.dataset.markStatus;
        if (selected === "pass") return submitMark("pass");
        if (selected !== "pass") {
          fields.hidden = false;
          buttons.forEach(item => item.setAttribute("aria-expanded", String(item === button)));
          show(message, "");
          field("defect_url").focus();
        }
      }));
      form.querySelector("[data-review-cancel]").addEventListener("click", async () => {
        collapse();
        show(message, "");
        await refreshPage();
      });
      form.addEventListener("submit", event => {
        event.preventDefault();
        return submitMark(selected);
      });
    });
  };
  initialize();
  if (document.querySelector("main[data-state-url]")) setInterval(async () => {
    if (polling || refreshing) return;
    polling = true;
    try {
      const main = document.querySelector("main[data-state-url]");
      const response = await fetch(main.dataset.stateUrl, {
        cache: "no-store", headers: {"If-None-Match": main.dataset.etag, Accept: "application/json"}});
      if (response.status !== 304 && response.ok) {
        const state = await response.json();
        if (state.ok && state.etag !== main.dataset.etag) pendingRefresh = true;
      }
      await refreshPage();
    } catch {
      // Transient network failures leave the page usable; the next poll retries.
    } finally {polling = false;}
  }, 3000);
})();
