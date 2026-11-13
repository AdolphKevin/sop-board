---
title: "环境按产品来：adapter 上报各服务状态，部分就绪即可执行，故障场景在运行中经 env stop|start 模拟"
scope: ["self:src/sop_board/envs.py", "self:src/sop_board/runs.py", "self:src/sop_board/guards.py", "self:src/sop_board/config.py", "self:src/sop_board/prompts/**", "self:examples/demo-shop/**"]
source: ["使用者 blur.yin，2026-11-12，docs/design.md 已定决策 22", "self:docs/design.md", "self:docs/sop/environment.yaml", "self:docs/sop/run.yaml"]
status: "accepted"
tags: ["环境", "服务", "部分就绪", "adapter.services", "服务状态", "故障模拟", "env stop", "env start", "依赖故障", "降级", "自动恢复", "未执行", "受阻", "本机未搭建", "异常退出", "库存演示项目"]
related: ["rules/adapter-services-contract.md", "rules/not-executed-vs-blocked.md", "decisions/2026-11-12-format-6-add-tables-only.md"]
---
# 环境按产品来：adapter 上报各服务状态，部分就绪即可执行，故障场景在运行中经 env stop|start 模拟

背景：原来 adapter 只报告整体就绪，任何一个服务没起来，`check` 和 `run prepare` 就失败，不需要这些服务的场景也跑不了；需要某个依赖故障的场景没有正规的模拟办法，cw2 只好在本机 `env.py` 里自写了一套暂停机制。使用者认定（2026-11-12，已定决策 22）：仓库、服务与中间件只是各人本机拉起验收环境的材料，不要求每次都全部活着。

决定：

- `adapter.services` 声明服务（业务说明、是否可停）；adapter 按[服务契约](../rules/adapter-services-contract.md)上报状态、响应停启请求。没声明 `services` 的 adapter 照旧只有整体就绪与否。
- adapter 输出就绪即环境可用：`check` 与 `run prepare` 只列出没在运行的服务与缺失的入口地址，不算失败；`env status`、本机看板概况与运行目录（`services.json`、brief.md“服务”一节）列出各服务的状态与业务说明，供执行会话按场景判断。内网不显示服务状态。
- `env stop|start <服务>` 只在本检出有 prepared 运行、且这套环境上只有这一个（已准备好的）运行时可用（已定决策 28：否则 `env-shared`；运行取 `--run`，其次 `SOP_BOARD_RUN`，多个又没指明时 `run-ambiguous`），停启记进该运行（`service_events` 表）；提交或放弃时还停着的服务自动恢复并记一条 `restore`。提交时按 `executed_at` 落在停止与下一次启动或恢复之间（按秒比较）记下停着期间执行的场景，结果详情据此显示“执行时 <服务>（<业务说明>）已停止”。
- 未执行与受阻按性质划分（见 rules/not-executed-vs-blocked.md）。未执行记在运行摘要 `work.result.not_executed`（带指纹与提交人），场景详情与报告显示当前指纹上最近一次，不新建存储。
- `retest` 不判断失败原因：守护未通过时，原因在失败的测试之后写明运行开始时没在运行的服务（声明了服务时）与各仓库工作区是否有未提交改动（`git status`，含未跟踪文件）。
- 故障场景的守护写在不需要真实停服的层，用替身模拟依赖不可用；写不出时不沉淀并写明原因（regression-distill 技能的分层选择）。
- 演示项目加库存演示项目：`make_demo.py --with-stock` 生成第三个仓库 stock，`--reuse --shop-only` 用同一份仓库只登记 app、lib（只搭商城）；基础演示项目不变，没有 `adapter.services`。

实现时补充的取舍（设计没写明）：停或启一个当前不是可停启状态的服务（本机未搭建、异常退出、未上报、要启动的服务不是由 env stop 停下的）、或环境没有运行时，返回新增的 `service-unavailable`，不等到超时；已在目标状态时返回 `changed: false`，不记录；启动超时不重建停止请求；声明校验先于 `run-required`（没声明 `services` 的项目无论有没有运行都返回 `service-not-declared`）。

## 备选

- 每个项目在自己的 adapter 里实现暂停机制（cw2 的做法）：停启不记进运行、提交时不恢复，结果上看不出执行时依赖是停着的。
- 环境必须全部服务就绪才可用：本机暂时没搭的服务会挡住全部场景，是这次要去掉的问题。
- 服务状态写进 `environments` 表、停启记录写进运行行的业务字段：要改已有表的结构，格式 6 只能加表（见 decisions/2026-11-12-format-6-add-tables-only.md）；共享库（已定决策 21，格式 7）时再合并。

## 后果

- 未执行不形成结论、不改变任何人看到的状态；独立复验仍须完整执行，不接受未执行。
- 服务异常退出不由 sop-board 自动判定场景结论：执行会话按 `env status` 或 `services.json` 的日志末尾写 blocked 并登记待人处理；`run submit` 输出列出提交时异常退出的服务，`during_run` 区分准备前还是运行期间。
- 只限本机由 adapter 启动的服务；外部共享的中间件不能停，需要这类故障的场景记受阻。延迟、错误注入等其他故障方式不做。
- 浏览器调试端口按 `9400 + 身份序号`（已定决策 28 起不含槽位）、不分项目；本机同时运行多个看板时，后启动的浏览器顺延到下一个空闲端口（见 decisions/2026-11-12-browser-port-roll-forward.md）。
