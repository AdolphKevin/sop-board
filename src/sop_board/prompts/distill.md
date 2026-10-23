# 看板调度回归沉淀

本任务由开发者在本机发起。任务目录：$task_directory。按随包技能 `$distill_skill` 执行。

1. 完整读取 `input.json`、`snapshot.json`、`scenarios.yaml`、`pass-evidence.json`、`repos.json`。范围仅为 input.json 的 scenes；场景原文含其他场景以保留上下文。通过记录的证据原件在 evidence/，文件名为记录的 sha256。逐条核对确认依据、当前场景与通过记录；报告的 confirmed_by、pass_evidence、independent_acceptance 使用快照顶层的对应字段。queued_snapshot 仅保存提交和验证前的入队历史。
2. 读取项目验证技能 `$skill_path`，然后按技能指向读取 repos.json 中对应产品版本的真实验证手册及本任务账号授权记录。账号使用范围以授权记录为准；授权缺失时保留受阻事实。凭据只从已传入的环境变量读取，材料只记录变量名。
3. 只在 repos.json 中登记的工作树修改测试、夹具、测试启动脚本、最小 CI 配置及项目知识的测试引用，产品代码和场景原文保持不变。沿用已有框架，按 regression-distill 选择测试层、实际运行并保存执行清单和原始输出。若使用看板环境，已由看板切换到工作树；保持该环境运行，由看板负责恢复。
4. 在任务目录写 `regression.json`，格式按技能的 references/report.md，逐项列全所选场景。测试 root 必须等于 repos.json 的实际路径，commit 为 null，scenario_file 使用 input.json.scenario_file。独立验收、未覆盖范围、检出能力和 CI 均按事实报告。运行日志等材料放任务目录 evidence/。
5. 改动留在工作树供开发者审核。不得提交、合入、推送或更改基线工作目录。此入口由看板在会话退出后重新核对输入并自动登记，不运行 distill submit、不直接写看板数据库、不提交场景轮次。最终消息简述改动、实际运行结果及报告位置。

测试与已确认预期不符时保留失败测试和证据，停止扩写该场景，报告现象、预期依据与疑似原因；不修改断言迁就实现。
