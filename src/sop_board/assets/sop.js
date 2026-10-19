"use strict";
(async () => {
  // 禁止图源改变安全设置、注入主题 CSS 或启用 HTML 标签。
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    suppressErrorRendering: true,
    flowchart: {htmlLabels: false},
    secure: ["secure", "securityLevel", "startOnLoad", "suppressErrorRendering",
      "flowchart", "themeCSS", "themeVariables", "fontFamily"]
  });
  for (const node of document.querySelectorAll("pre.mermaid")) {
    const original = node.textContent;
    try {
      await mermaid.run({nodes: [node]});
    } catch {
      // 错误图源保持可读，且不影响其余图示。
      node.textContent = original;
      const message = document.createElement("p");
      message.textContent = "图示无法渲染，请核对 Mermaid 原文。";
      node.after(message);
    }
  }
})();
