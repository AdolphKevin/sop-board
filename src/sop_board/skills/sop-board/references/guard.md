# 复测与沉淀守护测试

## 产品变化后复测

待复测是指：通过发生在旧快照上，且当前快照没有守护通过。

1. `sop-board retest --dry-run` 列出有有效守护的待复测场景与将运行的命令。
2. `sop-board retest [--scenario <场景键> …]`：守护进程直接运行守护测试，不启动 AI，有未提交改动也可以运行。全部通过的场景记为守护通过。
3. 仍在待复测的场景（守护失败、缺失或过期）按输出原因报告，再按 [run.md](run.md) 经真实入口执行，首选 `run launch --scope pending`。

## 沉淀

使用者要求沉淀（“待沉淀”“写成回归测试 / 低成本测试 / 守护测试”）时进行；执行与修复会话专注各自的工作，测试由沉淀会话写。

1. 可沉淀的场景在 `status --json` 的 `distill_queue`：同一指纹上有独立真实入口通过、还没有有效守护。
2. 按 [regression-distill](../../regression-distill/SKILL.md) 技能执行：`sop-board distill prepare [<场景键> …]`，按 prompt.md 写测试与 guard.json，再 `sop-board distill submit`。
3. 测试文件与 regression.yaml 留作未提交改动，由开发者审核提交。
