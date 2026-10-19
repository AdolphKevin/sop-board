"use strict";
(() => {
  const identity = document.getElementById("review-identity");
  if (!identity) return;
  const marker = identity.querySelector("[name='marker']");
  const token = identity.querySelector("[name='token']");
  const show = (element, value, state = "success") => {
    element.textContent = value;
    element.dataset.state = state;
  };
  const saveIdentity = () => {
    localStorage.setItem("sop-board.marker", marker.value.trim());
    localStorage.setItem("sop-board.token", token.value);
  };
  marker.value = localStorage.getItem("sop-board.marker") || "";
  token.value = localStorage.getItem("sop-board.token") || "";
  for (const event of ["input", "change"]) {
    marker.addEventListener(event, saveIdentity);
    token.addEventListener(event, saveIdentity);
  }
  document.getElementById("save-identity").addEventListener("click", () => {
    saveIdentity();
    show(document.getElementById("identity-message"), "标记人与令牌已保存，可随时更换。");
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
    window.addEventListener("popstate", applyFilter);
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
        marker.focus();
        return;
      }
      if (!token.value.trim()) {
        show(message, "请先在身份栏填写写入令牌", "error");
        token.focus();
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
    }
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
      (marker.value.trim() ? token : marker).focus();
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
            token.focus();
          } else {
            show(message, explanation || "标记未保存，请核对填写内容。", "error");
          }
        }
      } catch {
        show(message, "网络请求失败，内容已保留。请先核对是否已保存，再决定是否重试。", "error");
      } finally {
        pending = false;
        controls.forEach(button => {button.disabled = false;});
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
    form.querySelector("[data-review-cancel]").addEventListener("click", () => {
      collapse();
      show(message, "");
    });
    form.addEventListener("submit", event => {
      event.preventDefault();
      return submitMark(selected);
    });
  });
})();
