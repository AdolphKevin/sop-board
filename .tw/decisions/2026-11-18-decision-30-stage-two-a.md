---
title: "入口探测与待人处理决定是否自动选取场景"
scope: ["self:src/sop_board/probes.py", "self:src/sop_board/gate.py", "self:src/sop_board/runs.py", "self:src/sop_board/launcher.py", "self:src/sop_board/ledger.py", "self:src/sop_board/status.py", "self:src/sop_board/store.py"]
source: ["self:docs/design.md", "self:docs/reference.md", "用户决定（2026-11-18，已定决策 30 第二段 A）"]
status: "accepted"
tags: ["决策 30", "入口探测", "原因类别", "卡住", "再选", "待人处理", "格式 9"]
related: ["decisions/2026-11-18-decision-30-stage-one.md", "decisions/2026-11-12-shared-acceptance-records.md"]
verified: {"self": {"commit": "8c87ac298b554c336149d38e8e705f3fe1327a62", "date": "2026-11-18"}}
---
# 入口探测与待人处理决定是否自动选取场景

反复自动选择暂时无法得出结论的场景会消耗执行轮次。入口可声明只读探测，准备时先核对就绪环境，再为通过筛选的场景分配身份。部分入口不健康时，准备阶段登记被排除场景的未执行记录；全部不健康或修复复验的任何入口不健康时拒绝准备。

受阻和未执行登记原因类别。前置数据、场景本身、入口能力及等人处理在提交时生成待人处理。环境和外部依赖按同一机器、检出、场景指纹的登记顺序连续三次才生成，准备阶段登记的排除项也参与。结果和自动条目共用业务事务，解决与登记沿用同一个统一序号锁。

## 备选

- 用独立表保存卡住状态。待人处理已经有创建人、处理人及说明，同一个问题需要在现有待人处理入口查看和解决。
- 首次环境或外部依赖故障就暂停自动选择。这两类问题可以短暂发生，连续三次再暂停给自动重试保留机会。

## 后果

卡住仅影响当前场景内容的自动选取，点名仍可执行。解决条目或修改场景四要素后可恢复自动选择，解决说明进入之后同指纹的运行材料。场景状态与计数继续按已有业务记录推导。

业务记录格式 8 → 9 使用三条 `ADD COLUMN`。升级沿用格式 7 起的显式升级事务，本机状态保持格式 8，探测记录放在运行目录。
