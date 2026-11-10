---
title: "Web 验收只用看板守护进程启动的 Chrome for Testing，缺省无头"
scope: ["self:src/sop_board/browsers.py", "self:src/sop_board/skills/playwright-acceptance/**"]
source: ["self:docs/design.md"]
guards: ["self:docs/sop/browser.yaml#BRW-04-P01", "self:docs/sop/browser.yaml#BRW-05-N01", "self:docs/sop/browser.yaml#BRW-02-P01"]
verified: {"self": {"commit": "f310990b145aa38fd1a1b57c62d4fd8957fd21b8", "date": "2026-11-10"}}
tags: ["浏览器", "Chrome for Testing", "Playwright quit unexpectedly", "崩溃", "无头", "CDP"]
---
# Web 验收只用看板守护进程启动的 Chrome for Testing，缺省无头

现象：2026-11-10 的验收被用户叫停（“一直报错 Playwright quit unexpectedly”）。崩溃报告显示，崩溃的是另一个项目 Playwright 1.62.1 的 WebKit `Playwright.app`：它由 agent 进程树（Paseo）启动，约 50ms 后在 NSApplication 注册时中止。11-05 还有一次 Chrome for Testing 渲染进程中止。当时 sop-board 只能接管用户已打开的 Chrome，其 1.64.1 运行时需要的 CfT 修订 1248 也没有安装；多个项目共用同一份 ms-playwright 缓存。

根因：浏览器的启动方和版本都不受 sop-board 控制。有界面浏览器从 agent 进程树启动时会在窗口注册阶段中止；共用缓存让各项目的浏览器修订混在一起。崩溃发生后，执行者看不到原因，场景也没有如实记录。

修复与防护：运行时固定版本，装在本机级的 `~/.sop-board/runtime`，浏览器目录自用。只有看板守护进程启动 CfT：每个 检出×身份 一份持久 profile，缺省 `--headless=new`，只在真人登录时开窗口。执行者只通过 run.env 的 PW_CDP_ENDPOINT 连接，不自己启动浏览器。浏览器异常退出时记录信号、chrome.log 与 .ips；租约期间不自动重启，相关场景提交时记为受阻；30 分钟内两次异常退出进入 crashed，由人 `--reset-crash`。

发现方法：用 `kill -ABRT` 结束 CfT，核对场景是否记为受阻并写明信号与日志；用 `ps -o ppid,command` 核对浏览器的父进程是守护进程，参数含 `--headless=new`。

适用条件：改动浏览器启动、Web 模板、运行准备与提交，或增加新的浏览器类型时。
