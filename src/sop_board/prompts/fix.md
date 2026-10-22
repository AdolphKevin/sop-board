你在同一任务目录的多个隔离 worktree 中修复产品。一次根因分析覆盖全部问题与全部仓库。
本任务仓库（别名、工作树目录、规范化身份）：
$repositories
先读取以下绝对路径输入：
- 问题（批次、场景、实际结果、证据、缺陷单与此前 Agent 结果）：$problems_file
- 入口声明：$entries_file
- 固定场景与代码快照：$snapshot_file
- 用例并集：$cases_file
- 本次新增问题与上次验证证据：$feedback_file

入口 credential_env 声明的凭据变量已从启动看板的环境传入，使用须遵循项目手册与本任务授权记录；仅在进程环境和获授权请求中使用，关闭命令跟踪，脚本、日志、截图、报告与最终答复只记变量名，证据先脱敏。变量缺失属于环境问题，交开发者处理。

对全部问题做一份根因分析，逐项判定 product、execution、environment、scenario、requirement 或 unfixable。
每个问题用 root_repo 指出根因仓库别名；product 必须给出本任务中的别名，其他类别无法定位到仓库时填 null。
只修 product；其他问题写明理由与开发者方案，不为它们改代码。根因在被 Git 忽略的本机配置或运行环境时，必须判为 environment，指出配置项和位置后转开发者；不读取或输出凭证值，不产生代码修补。
从根因修复，直接替换错误逻辑，不加兼容层，不保留新旧两套实现，不做只让个别场景通过的局部修补，顾及其他场景与已有行为。
只修改产品代码，不写任何单元、接口或集成测试；即使仓库约定 TDD，本任务也不产出测试。验收由后续新会话经真实入口执行场景。
snapshot.json 中的场景文件、各仓 docs/sop/ 和工作区配置保持原样；只在上面逐仓列出的工作树路径内改产品代码，不修改基线目录或其它仓库。改动留在 worktree，不提交，不推送。失败尝试的工作树会保留：依据反馈修正已有改动，禁止用 reset、stash 或强制移动分支抹掉现场。
最终输出严格符合 fix.schema.json：root_cause；problems 每项 batch、scenario、category、root_repo、handling、reason；changed_files（使用 别名/相对路径）；plan（无方案用空字符串）。每个输入问题必须出现且只出现一次。

开发者说明：$developer_note
