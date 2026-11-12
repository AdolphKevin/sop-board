---
title: "未执行与受阻按性质划分：本机暂时不具备记未执行、不改变状态，意外故障或全队都不具备的能力记受阻"
scope: ["self:src/sop_board/prompts/**", "self:src/sop_board/runs.py", "self:src/sop_board/skills/**", "self:docs/standard.md"]
source: ["使用者 blur.yin，2026-11-12，docs/design.md 已定决策 22", "self:docs/standard.md", "self:docs/sop/run.yaml"]
verified: {"self": {"commit": "13399114fecde952aaf0f5f505bacb0db3e17620", "date": "2026-11-12"}}
guards: ["self:docs/sop/run.yaml#RUN-05-P01", "self:docs/sop/run.yaml#RUN-05-P02", "self:docs/sop/run.yaml#RUN-05-N01", "self:docs/sop/run.yaml#RUN-05-N02", "self:docs/sop/browser.yaml#BRW-06-N01"]
tags: ["未执行", "not-executed", "受阻", "blocked", "等人处理", "缺凭据", "凭据变量", "本机未搭建", "服务异常退出", "人机验证", "真实账号登录", "入口缺口"]
related: ["decisions/2026-11-12-environment-by-product.md", "decisions/2026-11-11-declared-test-accounts.md"]
---
# 未执行与受阻按性质划分：本机暂时不具备记未执行、不改变状态，意外故障或全队都不具备的能力记受阻

判断看性质，不看发生在执行开始前还是执行中：

- **未执行**（`not-executed`）：本机暂时不具备、补齐或换一台机器就能执行。场景用到的服务本机未搭建或被人停着；缺少入口声明的凭据变量（finding 只写变量名）；等人处理的事，即真实或未声明账号的登录、人机验证（含执行中途出现的）、只发到本人设备的验证码、身份使用次数到上限，同时登记待人处理。未执行不登记结果，场景状态不变，留在待执行范围；运行摘要记下谁、什么时候、缺什么，场景详情与报告显示当前指纹上最近一次，内网照常遮蔽变量名。
- **受阻**（`blocked`）：意外故障或全队都不具备的能力。服务或浏览器异常退出（附日志末尾并登记待人处理）；前置数据准备失败、证据丢失、运行作废；`browser may not be secure`；入口或观察能力还没建设（标准第 7 节的入口缺口）；需要停外部共享中间件或不可停服务的故障场景。
- 独立复验（`--fix`）须完整执行，不接受未执行。

这条口径写在标准第 1 节、提示词 run.md、`run prepare` 的 brief.md、playwright-acceptance（`notExecuted()`、转换脚本把 `未执行：` 开头的错误转成 not-executed）与 sop-board 技能里；改口径时一并改这几处。

## 依据

使用者 2026-11-12 决定（docs/design.md 已定决策 22）：原来缺凭据变量、等人处理、环境缺失都记受阻，受阻会改变状态、进入失败与受阻队列，把“这台机器暂时没准备好”说成了产品或环境故障；本机没搭的服务也被当作入口缺口。
