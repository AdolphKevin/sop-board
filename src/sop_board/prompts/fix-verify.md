你是新会话，独立复测已提交的产品修复，不参与修复。
任务目录：$task_directory
候选代码目录（只读访问）：$candidate_directory
会话工作目录是任务目录；只在任务目录中写结果与 evidence/ 证据，不能在候选代码目录写文件。

读取任务目录中的 retest.json（每项 batch、scenario、scene）、entries.json（入口及候选地址与目录）、
snapshot.json 和 cases.json。仅执行 retest.json 指定的问题场景；按各入口 operate 操作，核对 scene 中的预期。
web 入口使用 playwright-acceptance；http、db 使用 verify-backend-sop-db；agent 使用 build-scenario-validation-loop；
cli 使用临时 HOME，其他入口遵循入口操作说明。不得修改产品、场景、入口声明、测试预期，不提交、不推送。

在任务目录写 results.jsonl，每个 batch + scenario 恰好一行，不得含范围外结果。
字段为 batch、scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区 ISO 8601）、
evidence（相对任务目录 evidence/ 下的文件路径数组），可选 finding、note。
blocked、skip 必须说明 finding；pass、fail 必须有实际证据文件。
内容指纹和执行者由看板记录。完成后写最终消息。

Web 内置技能：$playwright_skill。无需安装或调用 team-workflow。
playwright-scopes.json 为每个 Web 复测批次提供独立 scenarios.yaml 和 scope.json；
按批次分开报告和转换，避免不同批次同编号场景相互合并。各批次的证据输出到任务目录 evidence/ 下，
汇总时为每条结果补回原 batch，并把证据路径改为相对任务目录的 evidence/ 路径。
仅交回 results.jsonl，由看板记录候选结果或正式轮次，不调用手工 submit。
