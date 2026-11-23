---
title: "一趟体验开始一组场景，逐条裁决，停滞提醒取代单场景超时"
scope: ["self:src/sop_board/runs.py", "self:src/sop_board/progress.py", "self:src/sop_board/launcher.py", "self:src/sop_board/launch_stats.py", "self:src/sop_board/activity.py", "self:src/sop_board/prompts/run.md", "self:src/sop_board/skills/sop-board/references/run.md"]
source: ["self:docs/design.md", "self:docs/reference.md", "用户决定（2026-11-23，已定决策 31 第一段）"]
status: "accepted"
tags: ["决策 31", "第一段", "一趟体验", "分组", "停滞提醒", "进度"]
related: ["decisions/2026-11-19-decision-30-stage-two-b.md"]
---
# 一趟体验开始一组场景，逐条裁决，停滞提醒取代单场景超时

场景用业务语言写，执行会话像真实用户一样体验产品：进到一个页面，可以一次核对多条场景的预期（例如在收件箱打开一条会话，同时核对聊天记录、右侧客户信息、备注与布局）。cw2 在 2026-11-22～23 的 76 次多场景运行里，场景的执行时段一次都没有重叠，因为 `run begin` 拒绝第二条正在计时的场景。

决定（用户 2026-11-23）：`run begin <键>…` 一次开始一组，全部键先核对，有一条不合格整条拒绝；同时进行的条数不限，裁决、证据和登记仍逐条。`progress.json` 的 `active` 按场景键索引，输出用 `in_progress`，运行结束时仍在进行的记入 `unfinished`。单场景超时改为停滞提醒：从“最近一次 `run verdict`”与“进行中场景最早的开始”两者中较晚的时刻起超过 `scenario_timeout` 仍没有新的裁决。提示词让执行会话按起点、身份、页面分组，只看不改的排前，要等业务时间的先触发、等待时做别的组；协调会话按功能区域成批启动。

## 备选
- 保留单场景计时，只放宽成可以先后开始：同一页面的多条预期仍要分别进页面，省不下往返。
- 按一趟统一计时、只记一个开始时刻：单条场景失去开始时刻，`launch stats` 的逐条耗时没有依据。
- 继续按单条开始时刻算超时：同一趟开始、最后裁决的场景会在正常走完一趟后被误报超时。

## 后果
同一趟开始的场景，`duration_ms` 各自从开始算到裁决，彼此重叠，不能相加成轮次用时。一直在进行却没人裁决的场景，只要别的场景还在不断裁决就不会触发停滞提醒，提交时列在未裁决里。本机进度文件结构变了，没有兼容旧进度的代码：安装新版前要确认没有 prepared 的运行。业务记录库格式不变。

### 放弃过的做法
- 一次运行里同时只计时一条场景，另一条正在计时时 `run begin` 返回 `scenario-active`
  放弃原因：一次进页面只能核对一条场景，同一页面的预期要分多轮各走一遍
  重新考虑的条件：无
  来源：已定决策 30 第二段 B（decisions/2026-11-19-decision-30-stage-two-b.md）
  关键词：scenario-active、当前场景、current、单场景超时、last_active
- 交互地图由场景操作记录取代，下一次按上次通过的操作序列回放
  放弃原因：把点击路线写死，一趟体验核对多条预期就做不到；cw2 前端变化快，记录的定位几天内大半过期
  重新考虑的条件：第二段量出找路占往返的比例后再定是否保留界面知识要点
  来源：已定决策 30 第四段 D；docs/design.md 已定决策 31“取代与搁置”
  关键词：操作记录、回放、交互地图、atlas、surface-map
