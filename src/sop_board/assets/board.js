"use strict";
(() => {
  let applyCurrentFilter = () => {};
  let pendingRefresh = false;
  let refreshing = false;
  let polling = false;
  let executionPolling = false;
  let writes = 0;
  const executionMessages = new Map();
  const reviewMessages = new Map();
  const sopBases = new Map();
  const renderWaiting = (container, review) => {
    container.replaceChildren(...review.waiting.map(reason => {
      const message = document.createElement("p");
      message.className = "notice";
      message.textContent = reason;
      return message;
    }));
  };
  const updateReview = review => {
    document.querySelectorAll("[data-review-complete]").forEach(controls => {
      const button = controls.querySelector("[data-review-complete-button]");
      button.disabled = !review.manual_count;
      button.textContent = `复核完成，开始修复（${review.manual_count} 条人工不通过）`;
      controls.querySelector("[data-review-complete-empty]").textContent =
        review.manual_count ? "" : "没有待修复的人工不通过";
      renderWaiting(controls.querySelector("[data-fix-waiting]"), review);
    });
  };
  const defectLink = (value, isJira) => {
    const container = document.createElement("span");
    container.className = "defect-link";
    let clickable = false;
    try {clickable = ["http:", "https:"].includes(new URL(value).protocol);} catch {}
    const content = document.createElement(clickable ? "a" : "span");
    content.textContent = value;
    if (clickable) {
      content.setAttribute("href", value);
      content.setAttribute("rel", "noopener noreferrer");
      if (isJira) content.dataset.jiraLink = "";
    }
    container.appendChild(content);
    return container;
  };
  const renderExecutionMessage = controls => {
    const saved = executionMessages.get(controls.dataset.executionUrl);
    if (!saved) return;
    const message = controls.querySelector("[data-execution-message]");
    message.textContent = saved.text;
    message.dataset.state = saved.state;
  };
  window.addEventListener("popstate", () => applyCurrentFilter());
  const hasDraft = () => Array.from(document.querySelectorAll(".review-form")).some(form =>
    !form.querySelector("[data-review-fields]").hidden &&
    ["defect_url", "note"].some(name => form.elements.namedItem(name).value.length > 0)) ||
    Array.from(document.querySelectorAll("[data-sop-upload]")).some(form => {
      const field = name => form.elements.namedItem(name);
      return (!field("batch").readOnly && field("batch").value.length > 0) ||
        field("confirmed_by").value.length > 0 || field("file").files.length > 0;
    }) || Array.from(document.querySelectorAll("[data-draft-form] textarea")).some(field => field.value.length > 0) ||
    Boolean(document.querySelector("#draft-confirm-dialog[open]"));
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
      const disclosures = Array.from(document.querySelectorAll("details[data-disclosure][open]"), item => item.dataset.disclosure);
      next.querySelectorAll("details[data-disclosure]").forEach(item => {item.open = disclosures.includes(item.dataset.disclosure);});
      document.querySelector("main").replaceWith(next);
      pendingRefresh = false;
      initialize();
    } catch {
      // Keep the current page and retry on the next poll.
    } finally {refreshing = false;}
  };
  const initialize = () => {
    applyCurrentFilter = () => {};
    document.querySelectorAll("[data-execution-url]").forEach(renderExecutionMessage);
    const hydrateJira = root => {
      root.querySelectorAll("a[data-jira-link]").forEach(async link => {
        const href = link.getAttribute("href");
        if (link.dataset.jiraLoaded) return;
        link.dataset.jiraLoaded = "true";
        const summary = document.createElement("span");
        summary.setAttribute("data-jira-summary", href);
        summary.setAttribute("aria-live", "polite");
        summary.dataset.state = "loading";
        link.after(summary);
        try {
          const response = await fetch("/api/v1/jira/summary?url=" + encodeURIComponent(href), {headers: {Accept: "application/json"}});
          const data = await response.json();
          if (!response.ok || !data.ok) {
            summary.textContent = " " + ((data.problems || []).map(p => p.problem).join("；") || "Jira 读取失败");
            summary.dataset.state = "error";
            return;
          }
          if (typeof data.summary !== "string" || typeof data.status !== "string") throw new Error();
          summary.textContent = " " + data.summary + " · " + data.status;
          summary.dataset.state = "success";
        } catch {
          summary.textContent = " Jira 读取失败";
          summary.dataset.state = "error";
        }
      });
    };
    hydrateJira(document);
    document.querySelectorAll("[data-environment-action]").forEach(button => {
      button.addEventListener("click", async () => {
        const message = document.getElementById("environment-message");
        button.disabled = true;
        try {
          const response = await fetch(button.dataset.environmentAction, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
          const data = await response.json();
          message.textContent = data.ok ? (data.checking ? "自检已开始" : "环境" + data.status) : (data.problems || []).map(p => p.problem).join("；");
        } catch {message.textContent = "请求失败，请查看环境状态后重试。";}
        finally {button.disabled = false;}
      });
    });
    const identity = document.getElementById("review-identity");
    if (!identity) return;
    const marker = identity.querySelector("[name='marker']");
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
    };
    marker.value = localStorage.getItem("sop-board.marker") || "";
    for (const event of ["input", "change"]) {
      marker.addEventListener(event, saveIdentity);
    }
    document.getElementById("save-identity").addEventListener("click", () => {
      saveIdentity();
      show(document.getElementById("identity-message"), "标记人已保存，可随时更换。");
    });
    document.querySelectorAll("[data-review-complete]").forEach(controls => {
      const button = controls.querySelector("[data-review-complete-button]");
      const message = controls.querySelector("[data-review-complete-message]");
      const saved = reviewMessages.get(controls.dataset.reviewComplete);
      if (saved) show(message, saved.text, saved.state);
      button.addEventListener("click", async () => {
        if (!marker.value.trim()) {
          show(message, "请先在身份栏填写标记人", "error");
          focusIdentity(marker);
          return;
        }
        saveIdentity();
        writes += 1;
        button.disabled = true;
        try {
          const response = await fetch(controls.dataset.reviewComplete, {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify({marker: marker.value.trim()})});
          const data = await response.json();
          if (response.ok && data.ok) {
            updateReview(data.review);
            show(message, "已纳入：" + data.scenarios.join("、") + "；开始时机：" + data.start);
            pendingRefresh = true;
          } else {
            show(message, (data.problems || []).map(p => p.problem).join("；") || "复核完成登记失败", "error");
            button.disabled = data.error_code === "nothing-to-fix";
          }
        } catch {
          show(message, "连接失败，请核对是否已登记后重试。", "error");
          button.disabled = false;
        } finally {
          reviewMessages.set(controls.dataset.reviewComplete, {text: message.textContent, state: message.dataset.state});
          writes -= 1;
          await refreshPage();
        }
      });
    });
    document.querySelectorAll("[data-developer-action]").forEach(button => {
      button.addEventListener("click", async () => {
        const controls = button.closest("[data-execution-url]");
        const message = controls?.querySelector("[data-execution-message]") || document.getElementById("developer-message");
        if (controls) delete message.dataset.taskId;
        saveIdentity();
        writes += 1;
        if (controls) controls.dataset.busy = "true";
        button.disabled = true;
        try {
          const response = await fetch(button.dataset.developerAction, {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: button.dataset.executionScope ? JSON.stringify({scope: button.dataset.executionScope}) :
              button.dataset.developerNote !== undefined ? JSON.stringify({note: document.getElementById("developer-note")?.value || ""}) : "{}",
          });
          const data = await response.json();
          if (!response.ok || !data.ok) {
            const hint = response.status === 409 && data.error_code === "nothing-to-execute" ?
              "；可改用“重新验证全部场景”。" : "";
            show(message, (data.error_code || "操作失败") + "：" + (data.problems || []).map(p => p.problem).join("；") + hint, "error");
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
            show(message, "任务 #" + data.task.id + "：" + (data.task.status === "等待合并" ? data.task.error : data.task.status));
            if (controls) message.dataset.taskId = String(data.task.id);
            if (button.dataset.caseFlow !== undefined) {
              button.textContent = "已指定单独成场景";
              button.dataset.choiceSaved = "true";
            }
          }
        } catch {
          show(message, "连接失败，请重试。", "error");
        } finally {
          writes -= 1;
          if (controls) {
            delete controls.dataset.busy;
            executionMessages.set(controls.dataset.executionUrl, {
              text: message.textContent, state: message.dataset.state, taskId: message.dataset.taskId,
            });
            button.disabled = Boolean(controls.querySelector(`[data-execution-reason="${button.dataset.executionScope}"]`).textContent);
          } else button.disabled = button.dataset.choiceSaved === "true";
        }
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
          const batch = uploadForm.elements.namedItem("batch")?.value.trim();
          if (uploadForm.dataset.workspaceKey && !batch) {
            show(message, "请填写批次名称", "error");
            return;
          }
          const target = uploadForm.dataset.workspaceKey ? "/api/v1/workspaces/" + encodeURIComponent(uploadForm.dataset.workspaceKey) + "/batches/" + encodeURIComponent(batch) + "/cases" : uploadForm.action;
          const response = await fetch(target, {method: "POST",
            headers: {"Content-Type": "application/json"},
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
    document.querySelectorAll("[data-sop-upload]").forEach(form => {
      const key = form.dataset.workspaceKey;
      if (!sopBases.has(key)) sopBases.set(key, new Map(Object.entries(JSON.parse(form.dataset.sopHashes))));
      const bases = sopBases.get(key);
      const field = name => form.elements.namedItem(name);
      const message = form.querySelector("[data-sop-message]");
      const problems = form.querySelector("[data-sop-problems]");
      const submit = form.querySelector("button[type='submit']");
      let pending = false;
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (pending) return;
        problems.replaceChildren();
        if (!marker.value.trim()) {
          show(message, "请先在身份栏填写标记人", "error");
          focusIdentity(marker);
          return;
        }
        const file = field("file").files[0];
        if (!file) {
          show(message, "请选择 SOP Markdown 或纯文本文件", "error");
          field("file").focus();
          return;
        }
        const batch = field("batch").value.trim();
        if (!batch || !field("confirmed_by").value.trim()) {
          show(message, "请填写批次名称和业务确认人", "error");
          return;
        }
        if (file.size > 1024 * 1024) {
          show(message, "上传失败：SOP 原文不能超过 1 MiB", "error");
          return;
        }
        pending = true;
        writes += 1;
        submit.disabled = true;
        saveIdentity();
        try {
          let content;
          try {
            content = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(await file.arrayBuffer());
          } catch {
            show(message, "上传失败：文件读取失败，SOP 原文必须为 UTF-8", "error");
            return;
          }
          const target = "/api/v1/workspaces/" + encodeURIComponent(key) + "/batches/" + encodeURIComponent(batch) + "/sop";
          const response = await fetch(target, {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({filename: file.name, content, uploaded_by: marker.value.trim(),
              confirmed_by: field("confirmed_by").value.trim(), base_sha256: bases.get(batch) || ""})});
          const data = await response.json();
          if (response.ok && data.ok) {
            bases.set(batch, data.sha256);
            form.reset();
            show(message, data.unchanged ? "原文未变，保留已有版本。" : "业务 SOP 上传成功。");
            const link = element("a", "查看 SOP 原文与版本");
            link.href = "/w/" + encodeURIComponent(key) + "/b/" + encodeURIComponent(batch) + "/sop";
            message.append(" ", link);
          } else {
            show(message, "上传失败", "error");
            for (const problem of data.problems || []) {
              problems.appendChild(element("li", problem.field + "：" + problem.problem));
            }
          }
        } catch {
          show(message, "文件读取或网络请求失败，请核对上传记录后重试。", "error");
        } finally {
          pending = false;
          writes -= 1;
          submit.disabled = false;
        }
      });
    });
    const draftIdentity = message => {
      if (marker.value.trim()) {saveIdentity(); return true;}
      show(message, "请先在身份栏填写标记人", "error");
      focusIdentity(marker);
      return false;
    };
    const submitDraft = async (url, body, message, button) => {
      if (button.disabled) return false;
      writes += 1;
      button.disabled = true;
      try {
        const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        const data = await response.json();
        if (!response.ok || !data.ok) {
          show(message, (data.problems || []).map(p => p.problem).join("；") || "操作失败", "error");
          return false;
        }
        show(message, data.task ? "任务 #" + data.task.id + "：" + data.task.status : "已保存");
        pendingRefresh = true;
        return true;
      } catch {
        show(message, "网络请求失败，内容已保留。请先核对是否已保存，再决定是否重试。", "error");
        return false;
      } finally {
        writes -= 1;
        button.disabled = false;
      }
    };
    document.querySelectorAll("[data-draft-form]").forEach(form => {
      const field = name => form.elements.namedItem(name);
      const message = form.querySelector("[data-draft-message]");
      const button = form.querySelector("button[type='submit']");
      const cancel = form.querySelector("[data-draft-cancel]");
      cancel.addEventListener("click", async () => {
        if (button.disabled) return;
        form.reset();
        form.closest("details").open = false;
        show(message, "");
        await refreshPage();
      });
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (!draftIdentity(message)) return;
        const rejecting = form.dataset.draftForm === "reject";
        const input = field(rejecting ? "reason" : "text");
        if (!input.value.trim()) {
          show(message, rejecting ? "请填写未采纳理由" : "请填写批注内容", "error");
          input.focus();
          return;
        }
        const body = rejecting ? {reason: input.value, author: marker.value.trim()} :
          {version: Number(field("version").value), target: field("target").value,
            kind: field("kind").value, text: input.value, author: marker.value.trim()};
        if (await submitDraft(form.action, body, message, button)) {
          form.reset();
          form.closest("details").open = false;
        }
        await refreshPage();
      });
    });
    const draftActions = document.querySelector("[data-draft-actions]");
    if (draftActions) {
      const message = draftActions.querySelector("[data-draft-message]");
      const revise = draftActions.querySelector("[data-draft-revise]");
      revise.addEventListener("click", async () => {
        await submitDraft(draftActions.dataset.draftActions + "/revise", {}, message, revise);
        await refreshPage();
      });
      const dialog = document.getElementById("draft-confirm-dialog");
      const confirm = dialog.querySelector("[data-draft-confirm-submit]");
      draftActions.querySelector("[data-draft-confirm]").addEventListener("click", () => {
        if (!draftIdentity(message)) return;
        show(dialog.querySelector("[data-draft-message]"), "");
        dialog.showModal();
      });
      dialog.querySelector("[data-draft-confirm-cancel]").addEventListener("click", () => {
        if (!confirm.disabled) dialog.close();
      });
      dialog.addEventListener("cancel", event => {if (confirm.disabled) event.preventDefault();});
      dialog.addEventListener("close", () => refreshPage());
      confirm.addEventListener("click", async () => {
        const message = dialog.querySelector("[data-draft-message]");
        if (!draftIdentity(message)) return;
        if (await submitDraft(draftActions.dataset.draftActions + "/confirm",
          {version: Number(draftActions.dataset.version), confirmed_by: marker.value.trim()}, message, confirm)) {
          window.location.reload();
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
        container.appendChild(defectLink(result.finding, result.jira_link));
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
        if (marker.value.trim()) return true;
        show(message, "请先在身份栏填写标记人", "error");
        focusIdentity(marker);
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
          payload.defect_url = field("defect_url").value;
          if (field("note").value) payload.note = field("note").value;
        }
        pending = true;
        writes += 1;
        const controls = form.querySelectorAll("button");
        controls.forEach(button => {button.disabled = true;});
        try {
          const response = await fetch(form.action, {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload)});
          const data = await response.json();
          if (response.ok && data.ok) {
            updateScenario(form.closest(".scenario-card"), data.scenario, data.filters);
            updateReview(data.review);
            collapse();
            show(message, "标记已保存为第 " + data.round + " 轮。");
          } else {
            const code = data.error_code || "";
            const reasons = (data.problems || []).map(problem => problem.problem);
            const explanation = [code, ...reasons].filter(Boolean).join("；");
            show(message, explanation || "标记未保存，请核对填写内容。", "error");
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
  let fixPolling = false;
  setInterval(async () => {
    const panel = document.querySelector("[data-fix-state]");
    if (!panel || fixPolling) return;
    fixPolling = true;
    try {
      const response = await fetch(panel.dataset.fixState, {cache: "no-store"});
      const data = await response.json();
      if (response.ok && data.ok) renderWaiting(panel.querySelector("[data-fix-waiting]"), data.review);
    } catch { /* 下次轮询重试，保留已显示的等待原因。 */ }
    finally {fixPolling = false;}
  }, 3000);
  setInterval(async () => {
    const controls = Array.from(document.querySelectorAll("[data-execution-url]"));
    if (!controls.length || executionPolling || writes || refreshing) return;
    executionPolling = true;
    try {
      const home = document.querySelector("[data-execution-home]");
      const response = await fetch(home ? "/api/v1/workspaces" : controls[0].dataset.executionUrl, {cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      if (!data.ok || writes || refreshing) return;
      const batches = home ? new Map(data.workspaces.flatMap(workspace => workspace.batches.map(batch =>
        ["/api/v1/workspaces/" + encodeURIComponent(workspace.key) + "/batches/" + encodeURIComponent(batch.name), batch]))) :
        new Map([[controls[0].dataset.executionUrl, data]]);
      controls.forEach(group => {
        const batch = batches.get(group.dataset.executionUrl);
        if (!group.isConnected || group.dataset.busy || !batch?.execution) return;
        for (const scope of ["pending", "all"]) {
          const button = group.querySelector(`[data-execution-scope="${scope}"]`);
          const reason = batch.execution[scope + "_reason"];
          button.disabled = Boolean(reason);
          button.textContent = (scope === "pending" ? "执行待执行场景（" + batch.execution.pending_count :
            "重新验证全部场景（" + batch.execution.total_count) + " 条）";
          group.querySelector(`[data-execution-reason="${scope}"]`).textContent = reason;
        }
        const saved = executionMessages.get(group.dataset.executionUrl);
        const task = batch.execution_tasks?.find(task => String(task.id) === saved?.taskId);
        if (task) {
          saved.text = "任务 #" + task.id + "：" + (task.status === "等待合并" ? task.error : task.status);
          renderExecutionMessage(group);
        }
      });
    } catch { /* Keep the last server-provided counts until the next successful read. */ }
    finally {executionPolling = false;}
  }, 3000);
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
  setInterval(async () => {
    const panel = document.getElementById("environment-panel");
    if (!panel) return;
    try {
      const response = await fetch(panel.dataset.environmentUrl, {cache: "no-store"});
      const data = await response.json();
      document.getElementById("environment-state").textContent = "环境" + data.status;
      document.getElementById("environment-ports").textContent = "环境端口：" + (data.ports.join("、") || "未声明");
      document.getElementById("environment-check-state").textContent = data.checking ? "自检中" : (data.check_passed ? "最近自检通过" : "尚未通过自检") + (data.checked_at ? " · " + data.checked_at : "");
      const jira = document.getElementById("environment-jira");
      if (jira) jira.textContent = "Jira 读取：" + (data.jira_configured ? "已配置" : "未配置");
      const setup = document.getElementById("environment-setup");
      setup.replaceChildren(...data.setup_instructions.map(instruction => {
        const line = document.createElement("p");
        line.textContent = instruction;
        return line;
      }));
      setup.hidden = data.setup_instructions.length === 0;
      const steps = document.getElementById("environment-check-steps");
      if (steps && data.latest_check) {
        steps.replaceChildren();
        for (const step of data.latest_check.steps) {
          const line = document.createElement("li");
          line.textContent = step.name + "：" + step.status + " · " + step.message;
          steps.appendChild(line);
        }
      }
      const log = document.getElementById("environment-log");
      if (log) log.textContent = (data.log_lines || []).join("\n");
      document.querySelectorAll("[data-requires-ready]").forEach(button => {button.disabled = !data.enabled;});
    } catch { /* Preserve the last visible state while reconnecting. */ }
  }, 3000);
})();
