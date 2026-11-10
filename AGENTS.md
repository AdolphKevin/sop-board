# Flow Board 开发约定

## 当前入口

本仓正在按 [docs/design.md](docs/design.md) 整体重写为 v10（分支 v10-rewrite，旧实现见 tag v9-final）。设计、阶段与已定决策以该文件为准：直接替换，不写双轨或兼容旧数据的代码；需要沿用的旧代码用 `git show v9-final:<路径>` 取出。

验收标准的唯一正文是 [docs/standard.md](docs/standard.md)；本仓的验收入口与留证见 [docs/verification.md](docs/verification.md)。

随包技能在 src/sop_board/skills/：sop-board（会话入口）、playwright-acceptance（Web 驱动）、regression-distill（沉淀守护测试），由 `sop-board skills install` 安装到两个客户端。编辑这些技能、提示词或本文件时，按 writing-for-agents 技能写作。

## 完成标准

- 预期来自已确认的业务要求、SOP 或接口约定，不把当前实现输出当作正确答案。
- 产品业务验收经真实入口执行并留证；实现者自检不冒充独立验收。
- 不删除断言、放宽预期、跳过场景或改成当前输出来通过验收。
- 如实区分通过、失败、受阻、跳过、未执行、守护通过和回归未建设；不把子集通过写成整批完成。
- 知识更新与代码属于同一逻辑改动。过程报告与浏览器证据放在仓库外。
- 交验收前经真实 HTTP 入口及 --lan 的实际非回环地址冒烟；HTML/JSON 自检与浏览器操作分开留证。独立性字段对应真实参与关系。

## 本机验证

docs/sop/*.yaml 是现行验收场景集，在实现与返工会话中只读；与设计不一致时写入仓库外报告，由审核方修改。新需求直接修改受影响的现行能力文件，不再按需求新开 yaml。

开发期间不写测试。现行场景集经独立真实入口验收通过后，按 regression-distill 把场景沉淀为根目录 tests/ 下的 pytest，守护索引写在 docs/sop/regression.yaml。

Codex 沙箱内不运行涉及 Git 工作树写入或启动浏览器的命令；原生 Codex、浏览器及真实工作树链路不能运行时，保留未执行范围，按 docs/verification.md 明示模拟材料，不能宣称独立验收通过。

## 安全边界

- 真实账号登录、人机验证和二次验证由用户在真实浏览器完成，遇到 browser may not be secure 停止。
- 仅在已确认的测试环境操作，合成账号登录沿用本次授权范围；不自行扩大权限。
- 不复用动态授权链接，不依赖常开的浏览器标签页续期服务。
- 凭证只经环境变量传递，不写入文档、任务说明或证据。
- 不自行清理用户数据；清理仅限用户授权范围。

<!-- team-workflow:guidance:knowledge start -->
## 项目知识

开工前按 team-knowledge 技能读取 `.tw/` 中与任务相关的现行约束、决策和踩过的坑：
`tw brief --task "<任务>" --path <相关路径>`。`tw` 是 PATH 上的启动器；找不到 `tw` 时改用 `python3 <team-knowledge 技能目录>/scripts/tw.py`。
评审改动时以改动的文件作 `--path` 读取，逐条核对其中的规则，评审意见写明不满足哪一条。
完成后按 team-knowledge 技能的规则更新知识（与代码同一个 PR），并运行 `tw check`。
<!-- team-workflow:guidance:knowledge end -->
