---
title: "adapter.services 的服务契约：状态文件由 adapter 写，请求文件由守护进程写，停启等到目标状态才返回"
scope: ["self:src/sop_board/envs.py", "self:src/sop_board/server.py", "self:src/sop_board/config.py", "self:examples/demo-shop/data/env.py"]
source: ["self:docs/design.md", "self:docs/reference.md", "self:docs/sop/environment.yaml"]
verified: {"self": {"commit": "13399114fecde952aaf0f5f505bacb0db3e17620", "date": "2026-11-12"}}
guards: ["self:docs/sop/environment.yaml#ENV-05-P01", "self:docs/sop/environment.yaml#ENV-05-N01", "self:docs/sop/environment.yaml#ENV-06-P01", "self:docs/sop/environment.yaml#ENV-06-N03"]
tags: ["adapter", "服务", "服务契约", "services", "state", "request", "状态文件", "请求文件", "service_timeout", "service-timeout", "env stop", "env start", "未上报"]
related: ["decisions/2026-11-12-environment-by-product.md"]
---
# adapter.services 的服务契约：状态文件由 adapter 写，请求文件由守护进程写，停启等到目标状态才返回

每个项目的 adapter 作者照这份契约写，改动它就是改动对外契约，须同步 docs/reference.md“服务契约”、guide.md“写适配”与演示 `env.py`。

- 声明：`config.yaml` 的 `adapter.services` 是 服务名 → `{about: 一行业务说明（≤200 字）, stoppable: 布尔，缺省 false}`，服务名 `[a-z][a-z0-9_-]*`、最长 64；`adapter.service_timeout` 为 1～3600 秒，缺省 60。
- 状态：adapter 把每个声明的服务写成 `SOP_ENV_LOG_DIR/services/<名称>.state`，内容是 JSON 对象，`state` 取 `running`、`stopped`、`absent`（本机未搭建）、`crashed`（异常退出，附 `log` 日志路径，相对路径按 `SOP_ENV_LOG_DIR` 解析），状态变化时改写。没有状态文件或内容不合格显示“未上报”（`unreported`）；环境没有运行时显示“环境未运行”（`down`）。
- 请求：守护进程在同一目录写 `<名称>.request` 请求停止，删除它请求启动；只对 `stoppable` 的服务写。adapter 轮询执行，完成后改写状态文件。守护进程每 0.2 秒读一次状态，变为目标值才返回；超过 `service_timeout` 返回 `service-timeout`，停止超时时删除请求文件撤回请求。
- 清空：守护进程在启动环境前、停止环境后与自身启动时清空 `services/` 目录，旧状态与遗留请求不带进新环境。
- 意外退出的服务由 adapter 写 `crashed`、不自动重启，环境整体照常就绪；服务状态只影响用到它的场景。
- 错误码：`run-required`（本检出没有 prepared 运行）、`service-not-declared`、`service-unknown`（列出已声明的服务）、`service-not-stoppable`、`service-timeout`、`service-unavailable`（环境没运行或服务不在可停启的状态），都是业务失败（退出码 1）。被拒绝的请求不写请求文件、不记停启。

## 依据

docs/design.md 已定决策 22 的“adapter 契约”“故障模拟”两条；文件格式（JSON 对象）、轮询间隔、清空时机与 `service-unavailable` 是实现时定下的细节，见 decisions/2026-11-12-environment-by-product.md。
