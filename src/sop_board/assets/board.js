"use strict";
(() => {
  const form = document.getElementById("mark-form");
  if (!form) return;
  const field = name => form.elements.namedItem(name);
  const message = document.getElementById("mark-message");
  const show = (value, state = "success") => {
    message.textContent = value;
    message.dataset.state = state;
  };
  const saveIdentity = () => {
    localStorage.setItem("sop-board.marker", field("marker").value.trim());
    localStorage.setItem("sop-board.token", field("token").value);
  };
  field("marker").value = localStorage.getItem("sop-board.marker") || "";
  field("token").value = localStorage.getItem("sop-board.token") || "";
  document.getElementById("save-identity").addEventListener("click", () => {
    saveIdentity();
    show("标记人与令牌已保存，可随时修改后再次保存。");
  });
  const updateRequirements = () => {
    field("defect_url").required = field("status").value === "fail";
    field("finding").required = ["blocked", "skip"].includes(field("status").value);
  };
  field("status").addEventListener("change", updateRequirements);
  updateRequirements();
  form.addEventListener("invalid", event => {
    if (event.target === field("defect_url") && field("status").value === "fail" && !field("defect_url").value.trim()) {
      event.preventDefault();
      show("失败结果必须填写本项目的缺陷链接", "error");
      field("defect_url").focus();
    }
  }, true);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    updateRequirements();
    if (!form.reportValidity()) return;
    saveIdentity();
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    const payload = {
      scenario: field("scenario").value,
      fingerprint: field("fingerprint").value,
      status: field("status").value,
      actual: field("actual").value,
      marker: field("marker").value.trim(),
      environment: field("environment").value,
      evidence: [],
    };
    for (const name of ["defect_url", "finding", "note"]) {
      if (field(name).value.trim()) payload[name] = field(name).value;
    }
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: {"Content-Type": "application/json", Authorization: "Bearer " + field("token").value},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (response.ok && data.ok) {
        show("标记已保存为第 " + data.round + " 轮。刷新页面可查看最新记录。");
      } else if (response.status === 401 || data.error_code === "unauthorized") {
        show("令牌无效，请重新填写写入令牌。已填内容已保留。", "error");
        field("token").focus();
      } else {
        const reasons = (data.problems || []).map(problem => problem.problem).join("；");
        show(reasons || "标记未保存，请核对填写内容。", "error");
      }
    } catch {
      show("网络请求失败，内容已保留。请先刷新核对是否已保存，再决定是否重试。", "error");
    } finally {
      button.disabled = false;
    }
  });
})();
