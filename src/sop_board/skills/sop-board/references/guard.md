# 守护测试与沉淀

## 运行守护测试

代码改动不改变场景状态；代码回归由守护测试发现。

1. `sop-board retest --dry-run` 列出守护有效的场景与将运行的命令；`not_guarded` 是不运行的场景与原因（含别人已沉淀、本机未同步的），`cannot_run` 是本机无法运行的守护（引用的仓库本机没有登记），处理见 [shared.md](shared.md)。
2. `sop-board retest [--scenario <场景键> …]`：守护进程直接运行守护测试，不启动 AI，有未提交改动也可以运行。通过的场景状态不变；未通过的场景转为待复测。
3. 待复测的场景按输出原因报告：retest 不判断失败原因，原因里写明失败的测试、当时本机没在运行的服务和工作区是否有未提交改动。代码问题修好后可再运行守护，或按 [run.md](run.md) 经真实入口执行，首选 `run launch --scope pending`；因为服务没搭或停着而跑不通的，执行会话记未执行，场景保持待复测，补齐服务后再运行守护或重跑。

## 沉淀

使用者要求沉淀（“待沉淀”“写成回归测试 / 低成本测试 / 守护测试”）时进行；执行与修复会话专注各自的工作，测试由沉淀会话写。

1. 可沉淀的场景在 `status --json` 的 `distill_queue`：同一指纹上有独立真实入口通过、还没有有效守护。
2. 按 [regression-distill](../../regression-distill/SKILL.md) 技能执行：`sop-board distill prepare [<场景键> …]`，按 prompt.md 写测试与 guard.json，再 `sop-board distill submit`。
3. 测试文件与 regression.yaml 留作未提交改动，由开发者审核提交。
