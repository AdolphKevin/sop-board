$validation_instruction
任务目录：$task_directory
任务目录有 brief.md（单批次复验时提供）时先读它：其中写明本轮的地图节选（atlas.json、bindings.json）用法、前置数据规则、时间上限与停止条件，与本提示词同为操作契约。
$environment_instruction
只在任务目录写结果与 evidence/，不改仓库，不写测试，不提交或推送。

无关前置数据指 initial_state 中不由本场景步骤产生、也不验证其产生过程的对象或状态。需要准备时先读本轮 fixtures.json，优先用 sop-board fixtures run；没有合适配方且 adhoc 为 true 时，才在声明环境与 scope 内用 fixtures exec --reason <说明> 临时写入。两种命令都带 --task <本任务编号>、--scenario <场景编号> 留痕；跨批次复验用 --scenario <批次>#<场景编号>。real_only 对象与被验收流程应产生的对象只能经真实入口产生，不能写库代替；禁止 DROP、TRUNCATE、无条件 DELETE/UPDATE 及范围外写入。无法准备时记 blocked，finding 写明缺什么配方、范围或真实流程。准备记录只说明数据来源，预期来自场景，结论来自真实入口的观察。

读取 retest.json（batch、scenario、scene）、entries.json（入口与地址）、snapshot.json、cases.json。
入口 credential_env 声明的变量已从${environment_origin}的环境传入，按项目手册和本任务授权记录使用。只在进程环境和获授权请求中使用取值，关闭命令跟踪，日志、脚本、截图与结果先脱敏；材料只记变量名。缺变量的场景由看板记为受阻，不在 retest.json 中，不更换账号或扩大权限。
操作前仅按名称核对执行命令能取得声明变量；客户端额外过滤导致未取得时，相关场景写 blocked，finding 仅列变量名与原因，不绕过客户端规则。
retest.json 包含问题场景，以及所有声明涉及改动仓库的场景（保守覆盖可能受影响范围）。通过各入口 operate 逐条执行并核对预期，不遗漏跨批次场景。先读取并遵循项目验证技能：$skill_path
入口在 entries.json 中有 driver_path 时（声明了 driver，或 Web 入口缺省使用随包的 playwright-acceptance），再读取并使用该绝对路径的辅助技能；没有 driver_path 的入口只按项目验证技能操作。
不能把阅读源码或测试程序的结果当作场景通过；不得沿用原通过结果。

写 results.jsonl，每个 batch + scenario 恰好一行，不得有范围外结果。
字段：batch、scenario、status（pass/fail/blocked/skip）、actual、executed_at（带时区 ISO 8601）、evidence（任务目录 evidence/ 下实际文件的相对路径数组），可选 finding、note。
blocked/skip 必须说明 finding；pass/fail 必须有实际证据文件。指纹与执行者由看板绑定。

入口 driver 缺省或为 playwright-acceptance 的 Web 场景使用内置技能：$playwright_skill。无需安装或调用 team-workflow。
按场景 verify 对应入口的 evidence 保存证据；web_evidence 的 video、trace 各自缺省 true，显式 false 时关闭对应录制，不要求该附件。
playwright-scopes.json 为每个 Web 复测批次提供独立 scenarios.yaml 和 scope.json，scope 已按入口固定各场景的录制开关；
按批次分开报告和转换，避免不同批次同编号场景相互合并。各批次的证据输出到任务目录 evidence/ 下，
汇总时为每条结果补回原 batch，并把证据路径改为相对任务目录的 evidence/ 路径。
仅交回 results.jsonl，由看板记录候选结果或正式轮次，不调用手工 submit。
