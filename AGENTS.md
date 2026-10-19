<!-- team-workflow:guidance:knowledge start -->
## 项目知识

开工前读取 `.tw/` 中与任务相关的现行约束、决策和踩过的坑：
`python3 <team-knowledge 技能目录>/scripts/tw.py brief --task "<任务>" --path <相关路径>`。
完成后按 team-knowledge 技能的规则更新知识（与代码同一个 PR），并运行 `tw.py check`。
<!-- team-workflow:guidance:knowledge end -->

<!-- team-workflow:guidance:project start -->
## 团队开发流程

使用已安装的 `team-workflow` 与 `team-knowledge` 技能；团队配置在 `.tw/config.json`，项目知识在 `.tw/`（开工前 `tw brief`，完成后在同一改动中更新），通用工具从技能目录运行。本仓补充（入口、负向场景清单、回归位置、本机与持续集成命令、真实验证操作手册）登记在 `.tw/config.json` 的 `context`。

### 需求完成门槛

- 需求完成的标准是通过用户实际使用的入口走完业务并核对数据结果，不是测试通过。
- 预期结果来自已确认的业务要求、SOP、接口约定或独立事实，不把当前实现的输出当作正确答案。
- 首次业务验收由未参与实现的测试同事、用户或 AI 经真实入口执行并留证；实现者自检只用于准备交付。
- 不为了通过而删除断言、放宽预期、跳过场景或改成当前输出。
- 缺真实入口证据、缺独立验收、回归未实际运行或自动化待建设，分别如实报告为未完成，不写成完成。

团队任务、业务验收、自动回归、缺陷处理、每周质量复盘或多代理派遣时，读取并执行技能内
`references/business-acceptance.md`。该页统一维护独立验收、有效回归、端到端链路、运行环境、
交付证据、缺陷关联和每周复盘规则，并提供记录模板。本仓的验收方式按技能内
`references/test-capability.md` 设计，方案登记在 `context`，经开发与测试同事确认；能力不足时按该页
记录过渡状态并建设入口。项目仅补充具体入口与执行方法。

### 安全边界

- 真实或生产账号的交互式登录由用户在真实浏览器完成，不用无头浏览器伪造；遇到 "browser may not be secure" 立即停止。
  `.tw/config.json` 的 `accounts` 中声明为 `synthetic` 的本地隔离测试账号，在任务开始时经用户一次性授权（记录在准备上下文）
  后可由代理登录；授权前不读取验证码、不提交登录表单。未声明的账号按真实账号处理。
- 不复用带动态 state 或设备上下文的旧授权链接；服务续期只依赖服务端刷新令牌，不依赖常开的浏览器标签页。
- 凭证只通过环境变量传递，不写入文档、任务说明或证据文件。
- 不自行执行破坏性操作；数据清理按真实验证操作手册的规则进行。
<!-- team-workflow:guidance:project end -->
