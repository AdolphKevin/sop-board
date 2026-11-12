---
title: "本机 trace 直接打开须提示浏览器权限，内网页面提供下载后选文件的入口"
scope: ["self:src/sop_board/assets/board.js", "self:src/sop_board/web.py"]
source: ["self:docs/design.md", "self:docs/sop/workbench.yaml", "https://developer.chrome.com/blog/local-network-access", "统一验收 F2 第 2 轮修复（2026-11-13）：未授权控制台报 LocalNetworkAccessPermissionDenied，授权后同一地址加载成功"]
verified: {"self": {"commit": "60d1c86a0ad464e07ad86ad10898b99006e0b416", "date": "2026-11-13"}}
guards: ["self:docs/sop/workbench.yaml#WB-02-P01"]
tags: ["trace", "Trace Viewer", "内网", "config.local", "本地网络", "权限", "LocalNetworkAccessPermissionDenied"]
---
# 本机 trace 直接打开须提示浏览器权限，内网页面提供下载后选文件的入口

现象：公网 Trace Viewer 读取回环地址的 trace 失败，控制台报访问 loopback 地址空间被拒绝；同一 trace 下载后选文件可打开。

根因：浏览器要求网站获得本地网络访问权限。新 Chrome 使用用户权限控制，不能把该错误一概归为看板缺少预检响应；授权前后用同一地址验证，才能区分权限、跨域响应与证据损坏。

修复与防护：按 config.local 区分页面。本机保留直接打开，提示首次询问本地网络访问时允许；本机与内网都提供下载说明，以及不带 trace 参数的 Trace Viewer 首页链接。内网不提供直接打开链接。证据由本机看板从另一台机器转发时沿用相同规则。

发现方法：保留未授权控制台原文，再在隔离浏览器中模拟用户允许权限，核对真实 trace 中的操作与页面。还须实际下载并在新的未授权浏览器环境中选文件加载。页面链接正确不等于 trace 能加载。

适用条件：修改 trace 的证据展示、跨域响应或跨机器转发时。
