---
title: "浏览器调试端口被占用时顺延到下一个空闲端口，就绪以 chrome.log 里本次的监听地址为准"
scope: ["self:src/sop_board/browsers.py", "self:src/sop_board/runs.py", "self:src/sop_board/guards.py"]
source: ["使用者 blur.yin，2026-11-12，docs/design.md 已定决策 24", "self:docs/design.md", "self:docs/sop/browser.yaml"]
status: "accepted"
tags: ["浏览器", "调试端口", "端口占用", "顺延", "9401", "多个看板", "cw2", "browser status", "run.env", "PW_CDP_ENDPOINT", "PW_IDENTITIES", "default_port", "守护命令 browser"]
related: ["decisions/2026-11-11-browser-handover.md", "decisions/2026-11-12-environment-by-product.md"]
---
# 浏览器调试端口被占用时顺延到下一个空闲端口，就绪以 chrome.log 里本次的监听地址为准

背景：身份浏览器的调试端口按 `9400 + 槽位×20 + 身份序号` 计算（已定决策 28 起改为 `9400 + 身份序号`，每个身份一个浏览器、各检出共用），不分项目和数据根目录，被占用时直接报错。使用者运行着 cw2 时，别的项目 main 检出上的 Web 场景都无法执行（9401 被 cw2 的浏览器占着），验收只能把这些步骤挪到 worktree 检出里做替代。

决定（已定决策 24 的实现，2026-11-12）：

- 缺省端口仍按原公式计算。`Instance.launch` 从缺省端口找起，第一个空闲的端口就用：127.0.0.1 上没有进程监听，且本机地址绑定得上（`SO_REUSEADDR` 探测，TIME_WAIT 不算占用）。到 65535 都找不到才报错。对占用者只做端口探测（连上即关），不结束、不干扰它。
- 实际端口记在 `Instance.actual` 与 `browser_instances.port`；`browser status` 显示 `port`（实际或最近一次启动所用）与 `default_port`。租约给出的 endpoint 用实际端口，`run.env` 的 `PW_CDP_ENDPOINT`、`PW_IDENTITIES` 与守护命令（声明 `browser:`）都取自租约，执行者不需要知道公式。
- 浏览器运行期间沿用同一进程与端口；下一次启动重新从缺省端口找起。
- **就绪须确认是自己的浏览器**：CfT 在 127.0.0.1 上绑定失败时不退出，而是改在 `[::1]:<端口>` 上监听（chrome.log 写 `bind() failed` 与 `DevTools listening on ws://[::1]:…`），这时 127.0.0.1 上应答 `/json/version` 的是别人的浏览器。所以就绪要求 `/json/version` 应答的是本次启动的浏览器：chrome.log 里本次启动（按启动前的文件长度截取）的监听行是 `127.0.0.1:<端口>` 且浏览器标识与 `webSocketDebuggerUrl` 一致，或者还没有监听行时 lsof 显示在该端口监听的正是本进程；监听在别的地址上说明探测之后端口被别人抢先占去，结束这次启动，从下一个端口再找（最多 5 次）。

## 备选

- `--remote-debugging-port=0` 让 Chrome 自选端口、读 `DevToolsActivePort`：设计要求先用缺省端口；而且显式端口时 CfT 不写 `DevToolsActivePort`，不能用它核对。
- 只靠启动前的端口探测（原来的做法）：探测与 CfT 绑定之间被别的看板抢先时，会把别人的浏览器当成自己的。
- 每个项目或数据根目录换一段端口：仍会与别的程序冲突，且要改公式与已有文档，设计没有采用。

## 后果

- 同一台机器上的多个看板不再因调试端口互相阻挡；实际端口每次启动可能不同，排查时看 `browser status` 或 `run.env`，不要按公式推算。
- 核对依赖 chrome.log 的“DevTools listening on”一行（固定版本的 CfT 无头模式实测会写），没有时退回 lsof（本仓环境要求里已有 lsof）；换运行时版本后要确认这一行仍在。
