你是新会话，独立复测已提交的产品修复，不参与修复。
任务目录：$task_directory
看板已经停止基线环境，并通过 adapter.env 启动包含各仓修复工作树的同一套环境；不要自行启停环境。
只在任务目录写结果与 evidence/，不改仓库，不写测试，不提交或推送。

读取 retest.json（batch、scenario、scene）、entries.json（入口与地址）、snapshot.json、cases.json。
retest.json 包含问题场景，以及所有声明涉及改动仓库的场景（保守覆盖可能受影响范围）。通过各入口 operate 逐条执行并核对预期，不遗漏跨批次场景。使用入口声明的 driver；缺省 web 使用 playwright-acceptance，http/db 使用 verify-backend-sop-db，agent 使用 build-scenario-validation-loop，cli 经命令行执行。
不能把阅读源码或测试程序的结果当作场景通过；不得沿用原通过结果。

写 results.jsonl，每个 batch + scenario 恰好一行，不得有范围外结果。
字段：batch、scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区 ISO 8601）、evidence（任务目录 evidence/ 下实际文件的相对路径数组），可选 finding、note。
blocked/skip 必须说明 finding；pass/fail 必须有实际证据文件。指纹与执行者由看板绑定。
