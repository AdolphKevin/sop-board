# Flow Board 参考手册

Flow Board（命令 `sop-board`）是在开发者本机运行的业务流程验收工具。场景写在产品仓库的 YAML 文件里，Agent 会话经 CLI 准备、执行、提交；看板守护进程负责网页、按检出启停产品环境、Chrome for Testing 浏览器代理和本机无头会话。本手册只描述现行行为。验收口径以 [业务验收与质量改进标准](standard.md) 为准，搭建步骤见 `sop-board guide`，Web 驱动见 [playwright-acceptance.md](playwright-acceptance.md)。

## 1. 概念

| 概念 | 含义 | 存放 |
| --- | --- | --- |
| 项目 | 名称 + 若干 Git 仓库。仓库身份是规范化后的 origin（`host/path`，去掉 `.git`） | `~/.sop-board/<名称>/`（`project.json`、`config.yaml`、`board.sqlite3`） |
| 检出 | 项目各仓库的一套工作副本。`main` 是 config.yaml 登记的路径，槽位固定为 0；worktree 检出各有槽位 ≥1 | `checkouts` 表 |
| 场景文件 | 产品仓库 `scenarios.path` 下的 `*.yaml`，是场景的唯一真相。看板从检出目录现读（按 mtime 与大小缓存），从不回写 | 产品仓库 |
| 场景键 | `<文件名去掉 .yaml>#<场景编号>`。场景编号在全部文件中唯一时，命令可只写编号 | 计算得出 |
| 指纹 | `initial_state`、`steps`、`expected`、`forbidden_effects` 四要素的 sha256（换行统一为 `\n`）。改标题、优先级等不改变指纹 | 计算得出 |
| 产品快照 | 检出各仓库当前内容（含未提交、未跟踪文件）的摘要，记录取证时的代码版本，不参与状态判定。场景目录、`snapshot_exclude`、`regression.yaml` 引用的测试文件不计入 | 计算得出 |
| 运行 | 一次场景执行或复验，绑定检出、准备时的快照、执行者与实现者 | `work` 表（kind=run）+ `runs/<编号>/` |
| 结果 | 运行中某场景的结论与证据，绑定指纹，记录准备时的快照；证据按 sha256 存储。从旧看板导入的结果 `product_digest` 为空，即代码版本未记录 | `results`（kind=agent）、`evidence/` |
| 标注 | 人工复测结论（通过 / 不通过 / 阻塞 / 需求变更，可附缺陷链接、说明、复测人） | `results`（kind=manual） |
| 守护 | 场景到低成本测试的关联，绑定场景指纹与测试文件摘要 | 产品仓库 `<scenarios.path>/regression.yaml` |
| 守护运行 | 一次在某快照上运行守护命令的结果 | `guard_runs` 表 + 输出证据 |
| 修复 | 失败场景 → 会话改产品 → 另一会话独立复验 → 提交后关闭 | `fixes` 表 + `fixes/<编号>/` |

### 场景状态

状态按检出推导，不存储。只看当前指纹的结果，从新到旧。哪些场景需要执行由场景内容（指纹）决定，代码改动不改变状态。

| 状态 | 键 | 条件 |
| --- | --- | --- |
| 草稿 | `draft` | 文件 `metadata.status: draft`；不进入执行与复测 |
| 未执行 | `not-run` | 当前指纹没有结果 |
| 通过 | `pass` | 最新结果为通过，且没有更晚的未通过守护运行 |
| 待复测 | `retest` | 当前指纹最近一次守护运行未通过，且晚于最新结果的执行时间；或最新为失败/受阻、之后修复已关闭；或人工不通过后修复已关闭、尚无新的人工通过 |
| 失败 / 受阻 / 跳过 | `fail` / `blocked` / `skip` | 最新结果如此；人工标注不通过按失败、阻塞按受阻显示 |
| 需求变更 | `changed` | 最新结果是人工标注的需求变更；场景文件按新需求修改后指纹变化，回到未执行 |

**经真实入口的 Agent 独立通过**指 kind=agent、`independent`、`reused_from` 为空的通过。执行期间产品有变化的通过也算，变化的路径只作版本记录。

**待沉淀**是徽标，可与其他状态并存：非草稿，同一指纹上有经真实入口的 Agent 独立通过，且没有有效守护。

**待人工复核**也是徽标，可与待沉淀并存：状态为通过，且当前指纹最新一条结果是经真实入口的 Agent 独立通过。之后有任一人工标注即消失。

待复测的原因写在状态旁，例如“守护测试未通过：<原因>”“修复已关闭，等待复测”“修复已关闭，等待测试同学复测”。

## 2. 安装与搭建

```sh
uv tool install <sop-board 检出或包地址>
sop-board skills install          # 把 sop-board、regression-distill、playwright-acceptance 链接到 ~/.agents/skills 与 ~/.claude/skills
sop-board init <名称> <仓库目录>...
sop-board serve <名称>            # 另开终端或在会话中启动守护进程
sop-board browser install         # 有 Web 入口时
sop-board check
```

- `skills install` 只更新本安装器拥有的链接（记录在各目录 `.sop-board-links.json`），其他来源的同名目录列出并跳过。
- `init`：每个目录须是 Git 工作树根目录、有可规范化的 origin、不能重复；数据目录 `<根目录>/<名称>` 不能已存在，也不能位于仓库内。名称以字母或数字开头，只含字母、数字、`.`、`_`、`-`，最长 64。别名取目录名小写化（不以字母开头时加 `r-`，重复时加 `-2`…）。写出 `project.json`（`{"format": "sop-board-project-6", "name": …}`）与只含 `repos` 的 `config.yaml`。
- 运行时根目录是 `SOP_BOARD_HOME`（缺省 `~/.sop-board`）；`--data` 指定项目所在的根目录。本机操作者文件 `~/.sop-board/operator` 与浏览器运行时 `~/.sop-board/runtime/` 也在这里。
- 旧格式数据目录（含 `workspace.json` / `workspace.sqlite3`、`project.json` 格式不符、数据库 `user_version` 不是 6）一律返回 `project-format`，不读取、不迁移。

### 项目与检出的识别

- 项目：显式 `--project/-p` 或位置参数优先；否则当前目录在某个项目数据目录内（例如运行目录）时用该项目；再否则按当前仓库根目录或 git common dir 匹配已登记仓库。匹配多个项目时报错，要求写名称。
- 检出：`--checkout/-c` 优先；否则对当前目录运行 `git rev-parse`，若是已登记仓库的 worktree，则该仓库取当前路径；同一父目录下的兄弟目录若是其他已登记仓库的 worktree（共用 common dir），一并归入；其余仓库回落到 main。第一次见到时登记，名称取父目录名（有兄弟仓库时）或仓库目录名，清理成 `[A-Za-z0-9._-]`、最长 48，重名加 `-2`…；槽位取最小空闲正整数。不在项目仓库内时为 main。

### config.yaml

严格 YAML：不接受锚点、别名、显式标签和重复键；出现格式外字段即不合格。配置不合格时 `status` 仍可用，其余需要配置的命令报错。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `repos` | 是 | 别名 → 绝对路径（可 `~/`）。别名 `[a-z][a-z0-9-]*`；路径须是 Git 根目录、有 origin，数据目录不能在其中 |
| `identities` | 是 | 别名 → 登记时规范化的 origin 身份（`init` 写入）。仓库的 origin 与登记不一致时配置不合格，防止路径指向了别的仓库 |
| `scenarios` | 是 | `{repo: <别名>, path: <仓内相对目录>}`，不含 `..`，不能是仓库根 |
| `skill` | 是 | 项目验证技能目录名，位于 `<数据目录>/skills/<名称>/SKILL.md`；须有含非空 `name`、`description` 的 YAML 文件头，每个入口名须出现在某个 `## ` 二级标题中；技能目录内不能有符号链接 |
| `entries` | 是 | 入口名 → 入口声明，至少一个（见下表） |
| `adapter` | 是 | 见下文；至少要有 `env` |
| `snapshot_exclude` | 否 | 别名 → 仓内相对路径数组，不计入产品快照 |
| `fixtures` | 否 | 前置数据配方（见 §9） |
| `agents` | 否 | 工作类型 → `{runner: codex\|claude, model?, effort?, args?}`。类型：`default`、`run`、`fix`、`distill`、`merge`、`author`、`draft`、`atlas`；`effort` 取 `low/medium/high/xhigh/max`。启动器只用 `run`、`distill`，缺省回落 `default`，再回落 codex |

入口字段：

| 字段 | 说明 |
| --- | --- |
| `surface`、`operate`、`isolation` | 必填，非空文字。`surface: web` 走浏览器代理 |
| `observe`、`evidence` | 必填，非空文字数组 |
| `address_env` | 入口地址的环境变量名，值来自 adapter 的 ready 行 |
| `credential_env` | 凭据变量名数组。只写名称，取值从守护进程/会话环境读，输出与日志按取值脱敏 |
| `driver` | 辅助技能目录名。Web 入口缺省为随包 `playwright-acceptance`（`SOP_BOARD_SKILLS_DIR` 下同名技能可覆盖）；其他技能在 `<数据目录>/skills/` 或 `SOP_BOARD_SKILLS_DIR`（缺省 `~/.agents/skills`）中查找 |
| `web_evidence` | 仅 Web，`{video: bool, trace: bool}`，缺省都开 |
| `identities` | 仅 Web，身份数组：`id`（小写编号，不能是 `default`）、`label`（对应场景 actor）、`account_env?`、`code?`（`mailbox/backend/manual`，缺省 manual）、`limits?`（`logins_per_day`、`codes_per_hour`、`codes_per_day` 正整数）、`real?`（真实账号，只由人登录）。未声明时只有 `default` |
| `spec_library` | 仅 Web，`<别名>:<仓内目录>`，不能是仓库根或符号链接；交互地图放在其下 `atlas/` |
| `atlas` | 仅 Web，`{repos: [别名…], languages?: [..]}`：地图可引用的前端仓库 |

### adapter 契约

| 字段 | 说明 |
| --- | --- |
| `env` | 必填，argv 数组，不经 shell。`argv[0]` 若是数据目录下的文件则按该路径执行；工作目录为数据目录 |
| `worktree`、`cleanup` | 可选，须同时声明；用于 `checkout create/remove` |
| `ports` | 基础端口数组（1–65535，不重复） |
| `port_step` | 0～10000，缺省 0。实际端口 = `ports[i] + slot × port_step`；为 0 时各检出共用端口、轮流使用 |
| `ready_timeout` | 正整数秒，缺省 180；也是 worktree/cleanup 的超时 |

**adapter.env**：前台运行，就绪时输出一行 `sop-board: ready`，可在同一行后附 JSON 对象给出变量，例如 `sop-board: ready {"APP_URL": "http://127.0.0.1:8100"}`（变量名须合法且不以 `SOP_` 开头）。输入变量：

| 变量 | 值 |
| --- | --- |
| `SOP_CHECKOUT` | 检出名称 |
| `SOP_REPO_<别名>` | 本检出该仓库路径（别名大写，`-` 换 `_`） |
| `SOP_BASE_<别名>` | 登记（main）仓库路径 |
| `SOP_PORT_OFFSET` | `slot × port_step` |
| `SOP_PORTS` | 实际端口 JSON 数组 |
| `SOP_ENV_LOG_DIR` | `<数据目录>/env/<检出>/`，输出写入其中 `<序号>.log`（按凭据取值脱敏） |

另外注入 `adapter.worktree` 为该检出返回的 `env`，以及守护进程环境中的凭据变量；继承环境里其他 `SOP_*` 变量被去掉。

**adapter.worktree**：输入 `SOP_TASK_DIR`（`<数据目录>/checkouts/<名称>`）、`SOP_FIX_BRANCH`（`sop-board/<名称>`）、`SOP_REPOS`（逗号分隔别名）、`SOP_BASE_<别名>`、有 `--from` 时 `SOP_BASE_COMMITS`（别名 → ref 的 JSON）。stdout 最后一行须为 `{"repos": {别名: 路径}, "env": {变量: 文字}}`，覆盖全部请求的仓库，env 不能用 `SOP_` 前缀。每个返回路径须通过三项核对：是工作树根目录且与登记仓库共用 common dir、检出了 `sop-board/<名称>` 分支、出现在 `git worktree list` 中。没有声明 worktree 时直接 `git worktree add -b sop-board/<名称> <数据目录>/checkouts/<名称>/<别名> <from 或 HEAD>`。

**adapter.cleanup**：输入同上，另有 `SOP_WORKTREES`（别名 → 路径 JSON，只含本检出自有的 worktree）。没有声明时逐个 `git worktree remove`。清理后目录仍存在则报错并保留检出记录。

### check

`sop-board check` 输出 `steps`，三项全部合格才退出 0：

1. **配置**：config.yaml、技能与入口校验。
2. **环境**：守护进程须在运行；在当前检出拉起 adapter.env 并等就绪，且每个入口的 `address_env` 都在 ready 变量里。
3. **浏览器运行时**：`~/.sop-board/runtime/runtime.json` 存在且可执行文件在。

## 3. 守护进程与访问范围

`sop-board serve [项目] [--port 8787] [--lan]` 在前台运行一个项目的守护进程，可在终端或 agent 会话中启动。它负责网页、各检出的 adapter 环境、浏览器代理、守护测试运行与无头会话启动器。启动时写 `<数据目录>/daemon.json`（pid、端口、URL、内网地址）与 `daemon.token`（权限 0600），清理上次遗留的环境进程组与浏览器、把遗留的运行中启动记为 failed；收到 SIGINT/SIGTERM 时停掉全部环境、浏览器和启动后删除状态文件。同一项目已在运行时拒绝再启动。

CLI 需要环境、浏览器、守护测试或启动时，以本机令牌经回环 HTTP 调用守护进程；守护进程没运行时返回 `daemon-not-running`，并给出 `sop-board serve <名称>` 命令。`status` 也显示守护进程是否运行。

| 访问级别 | 谁可以调用 | 用于 |
| --- | --- | --- |
| `lan` | 任何连接（`--lan` 时监听 0.0.0.0） | 查看工作台与页面、人工复测标注、答复问题、批注草稿、上传用例或 SOP |
| `local` | 只接受实际回环连接 | 工作台的“AI 重跑”“运行守护”“沉淀”按钮、启动日志 |
| `token` | 回环连接 + `Authorization: Bearer <daemon.token>` | `/local/*`：CLI 调用的环境、浏览器、retest、distill submit、launch |

写入（POST/PUT/DELETE）有 `Origin` 头时须与 Host 同源；有正文时须为 `application/json`，正文上限 64 MiB。非回环访问返回 `403 local-only`。

**内网遮蔽**：对非回环连接，除 `/assets/` 外的所有响应（含 zip 证据内的文本文件与响应头）都把凭据取值替换掉、隐藏凭据变量名，并把数据目录、登记仓库与各检出路径替换为 `<本机路径>`。接口本身也不向内网返回会话编号、执行者/实现者、运行目录、启动日志与命令；报告里执行者显示为“Agent 会话”；未预期错误只返回异常类型。

## 4. 命令一览

通用约定：

- 除 `status`（缺省文字，`--json` 输出 JSON）、`guide`、`playwright guide` 外，所有命令都输出 JSON，`--json` 可省。
- 退出码：0 成功；1 业务失败（`{"ok": false, "error_code", "problems": [{field, problem}]}`）；2 参数错误；`run wait` 超时为 3。
- 大多数命令接受 `--project/-p`、`--data`、`--checkout/-c`。
- 署名：登记自动用本机操作者（`~/.sop-board/operator`，否则 `git config user.name`）；别人确认或上传时用 `--as <姓名>`。没有操作者时要求先 `operator set`。

| 分组 | 命令 | 要点 |
| --- | --- | --- |
| 搭建 | `init <名称> <仓库>... [--data]` | 见 §2 |
| | `guide` | 打印完整搭建指引 |
| | `operator [show\|set <姓名>]` | 查看来源或设置本机操作者（单行，最长 64） |
| | `skills install` | 安装随包技能链接 |
| | `serve [项目] [--port] [--lan]` | 见 §3 |
| | `status [项目] [--json]` | 检出、操作者、快照、各状态计数、待沉淀数（顶层 `distill`）、待人工复核数（顶层 `review`）、`queues`（retest/fail/blocked/not-run）、`distill_queue`、`review_queue`、不合格场景文件、守护进程、待人处理；文字输出的场景行末尾总是列出“待沉淀 N；待人工复核 N” |
| | `check` | 见 §2 |
| 检出 | `checkout list` | 全部检出及槽位、偏移、路径 |
| | `checkout show [名称]` | 含场景目录与各仓分支 |
| | `checkout create <名称> [--repo 别名]... [--from ref]` | 名称规则同检出名、不能是 main；`--repo` 只为这些仓库建 worktree；日志 `<数据目录>/checkouts/<名称>.log` |
| | `checkout remove <名称>` | 环境须先停；main 不能移除；记录标为已移除，槽位释放 |
| 环境 | `env up\|down\|restart\|status [--all]` | 经守护进程启停或重启当前检出环境；`status --all` 列出全部检出 |
| 浏览器 | `browser install` | 安装运行时与 CfT（不需要守护进程） |
| | `browser status [--identity]` | 守护进程未运行时只给运行时信息 |
| | `browser open [--identity] [--headed] [--reset-crash] [--url]` | 缺省身份 `default`；见 §6 |
| | `browser release [--identity]` | 真人完成后交还：只释放真人租约，窗口保持打开，之后的运行沿用；见 §6 |
| | `browser close [--identity]` | 结束浏览器并释放真人租约 |
| 执行 | `run prepare [--scope pending\|all\|键…] [--fix N] [--session S]` | 见 §5；`--fix` 不能与 `--scope` 同用 |
| | `run submit --run N [--implementer X]` | 见 §5 |
| | `run abandon --run N` | 放弃并释放浏览器租约 |
| | `run launch [--scope …] [--fix N] [--implementer X]` | 由守护进程启动无头执行会话；`pending` 在 CLI 侧展开为具体场景；`--fix` 启动修复的独立复验 |
| | `run wait [--launch N \| --run N] [--timeout 1800]` | 等启动结束或运行离开 prepared；缺省为本检出最近一次启动；`0` 不限时 |
| | `retest [--scenario 键…] [--dry-run]` | 见 §7 |
| | `mark <键> pass\|fail\|blocked\|changed [--defect 链接] [--note 说明] [--as 姓名]` | 人工复测：通过、不通过、阻塞、需求变更；缺陷链接与说明可选；草稿拒绝 |
| | `launch status` / `launch stop --launch N` | 最近 20 次启动；停止发 SIGTERM 给进程组 |
| 修复 | `fix prepare [键…] [--session]`、`fix submit\|close\|abandon [--fix N]` | 见 §8；省略 `--fix` 时取本检出最近一个状态相符的修复 |
| 沉淀 | `distill prepare [键…] [--session]`、`distill submit [--report guard.json] [--work N]` | 见 §7 |
| 编写 | `inbox add <文件> [--kind cases\|sop] [--as]`、`inbox list [--all]`、`inbox show\|done\|reject <编号> [--note]` | 见 §9 |
| | `merge prepare [--inbox N]...`、`merge submit [--work N]` | |
| | `ask --about <inbox:N\|键\|文件名> --text … [--work] [--as]`、`answers [--about] [--unanswered]` | |
| | `sop prepare --source 文件...`、`sop submit --file <md> [--work] [--as]` | |
| | `draft annotations [文件] [--all]`、`draft resolve <批注编号> [--reject] [--note]`、`draft confirm <文件> [--as]` | |
| 其他 | `atlas prepare --entry E [--mode full\|stale]`、`atlas submit --entry E [--work]`、`atlas stale [--entry]` | |
| | `specs check --repo 别名 <路径> [--ledger 账本.json]` | spec 断言保真检查，违规退出 1 |
| | `fixtures run <配方> [--param 名=值]... [--run N] [--scenario]`、`fixtures exec --reason … [--run] -- <命令>` | |
| | `identity status`、`identity record --identity I --kind login\|code [--run N]` | |
| | `attention list`、`attention add --kind K --text … [--scenario]... [--run]`、`attention resolve <编号> [--note]` | |
| | `report [--file 文件] [--zip] [--out 目录]` | 见 §10 |
| | `playwright guide\|convert\|tags` | Web 指引、Playwright JSON 报告转 results.jsonl、配对标签 |

## 5. 运行

### 准备

`run prepare` 的执行者取 `--session`，其次 `SOP_BOARD_SESSION`（启动器注入），再次 `CODEX_THREAD_ID` 或 `CLAUDE_CODE_SESSION_ID`；都取不到返回 `session-required`。范围与原因：

| `--scope` | 包含 | 原因 |
| --- | --- | --- |
| `pending`（缺省） | 未执行、待复测、失败、受阻、跳过的非草稿场景 | 未执行 `new`；守护未通过导致的待复测 `guard-failed`，修复关闭导致的为 `rerun`；失败/受阻/跳过 `failed` |
| `all` | 全部非草稿场景 | `rerun` |
| 键或编号 | 指定场景；含草稿则 `draft-not-runnable` | 同 pending 的映射，其余 `rerun` |
| `--fix N` | 修复的问题场景 | `rerun` |

同一检出同时只能有一个 prepared 的运行（`run-prepared`，提示 `run abandon`）。准备时经守护进程拉起本检出环境（快照与环境启动时不同则重启；`port_step: 0` 且别的检出环境在运行时返回 `env-busy`），为本轮 Web 入口的全部身份取得浏览器租约，再写出运行目录 `<数据目录>/runs/<编号>/`：

| 文件 | 内容 |
| --- | --- |
| `prompt.md` | 执行提示词（随包 `run.md`，修复复验附“逐条实际执行问题场景”） |
| `brief.md` | 速查卡：范围计数、入口证据与录制开关、浏览器问题、前置数据规则、时间上限 |
| `scenarios.yaml`、`pending.json` | 本轮场景（带 key、reason）与范围 |
| `entries.json`、`repos.json` | 用到的入口与地址；检出仓库路径 |
| `snapshot.json`、`product-files.json` | 准备时的快照与逐文件清单 |
| `run.env` | 入口地址变量、`SOP_BOARD_RUN`、`SOP_BOARD_CHECKOUT`；有 Web 场景时另有 `PW_CDP_ENDPOINT`（default 身份或第一个身份）、`PW_IDENTITIES`、`PW_SCOPE`、`PW_SCENARIO_FILE`、`PW_OUT`、`PLAYWRIGHT_BROWSERS_PATH`、`PW_NODE_ROOT` |
| `scope.json`、`identities.json`、`node_modules`（链接到运行时） | 仅 Web：Playwright 范围；各身份及其 CDP endpoint |
| `evidence/` | 证据放这里 |
| `fixtures/` | `fixtures run/exec --run N` 的留痕 |

会话写出 `results.jsonl` 后提交。

### results.jsonl

每个范围内场景恰好一行（字段不能重复，不接受 NaN/Infinity），`scenario` 写场景键，或在本轮唯一的编号。

| 字段 | 规则 |
| --- | --- |
| `status` | `pass`、`fail`、`blocked`、`skip`、`not-executed` |
| `actual` | 非空（not-executed 除外） |
| `executed_at` | 带时区的 ISO 8601，不能晚于现在 5 分钟 |
| `evidence` | `evidence/` 下的相对路径数组，普通文件，单个不超过 64 MiB；实际执行的 pass/fail 至少一份 |
| `finding` | blocked、skip、not-executed 必填 |
| `note` | 可选说明 |

`not-executed` 行写 finding、不附证据，不登记结果，场景留在待执行。独立复验（`--fix`）不接受 `not-executed`。表外字段不合格。

### 提交规则

`run submit --run N`（可在任意目录运行，检出取运行登记的检出）：

1. 运行须为 prepared，否则 `run-not-prepared`。
2. **作废**：准备后场景被删除或指纹变化的，只作废这些场景（`voided`），不登记。
3. **浏览器崩溃**：租约期间该运行持有的身份浏览器异常退出时，用到该身份的 Web 场景若没有 pass 行，记为 `blocked`，finding 为“浏览器进程 <时间> 因 <信号或退出码> 退出，日志 <路径>[，崩溃报告 <路径>]”。
4. 其余场景逐行校验（缺行即失败）。
5. **product_changed**：比较准备时与提交时的逐文件清单，执行期间变化的路径记在每条结果上，作为版本记录，不影响状态与独立性。
6. 登记结果、证据入库；某场景最近一次守护运行通过而本次失败时记漏检（§7）；释放浏览器租约；修复复验时更新修复状态（§8）。

输出 `recorded`、`voided`、`not_executed`、`product_changed`、`browser_crashes`、`independence`。校验不合格时不登记任何结果，运行保持 prepared，修正后重交。

### 独立性

实现者依次取：修复复验时的修复会话；`run submit --implementer`；`SOP_BOARD_IMPLEMENTER`（`run launch --implementer` 注入）。没有实现者记 `undeclared`（独立性未声明）；执行者等于实现者记 `self-check`（实现者自检）；否则 `independent`（独立验收）。只有 `independent` 的真实入口通过能作为沉淀与守护的依据。

### launch / wait

`run launch` 与工作台“AI 重跑”“沉淀”按钮都由守护进程启动无头会话：

- 同一检出同时只有一个 running 的启动（`launch-running`）。类型 `run` 或 `distill`，须有具体场景。
- 工作目录是该检出的场景仓库；提示词 `<数据目录>/launches/<编号>.prompt.md`（随包 `launch.md`：先运行 `sop-board run prepare --scope …` 或 `distill prepare …`，按 prompt.md 执行并提交，需要人时 `attention add` 后结束），日志 `<编号>.jsonl`。
- 注入 `SOP_BOARD_LAUNCH`、`SOP_BOARD_CHECKOUT`、`SOP_BOARD_IMPLEMENTER?`。Claude：启动器生成 UUID 作为 `--session-id` 并注入 `SOP_BOARD_SESSION`，缺省参数 `--permission-mode acceptEdits --allowedTools Bash Read Write Edit Glob Grep`。Codex：`codex exec --json -s workspace-write -c sandbox_workspace_write.network_access=true --skip-git-repo-check -C <目录>`，会话编号取日志中的 `thread.started`。两者都 `--add-dir` 数据目录与浏览器运行时目录。
- 状态 `running` → `exited`（退出码 0）/`failed`/`stopped`。启动记录与其会话编号就是独立执行的证据；运行通过 `SOP_BOARD_LAUNCH` 关联启动。
- `run wait` 每 2 秒轮询，结束时输出启动状态及其运行结果。

## 6. 浏览器代理

- **运行时**：`browser install` 把随包 `package.json`/`package-lock.json`（固定 `@playwright/test` 1.64.1）复制到 `~/.sop-board/runtime/`，`npm ci` 后以 `PLAYWRIGHT_BROWSERS_PATH=<runtime>/browsers` 执行 `playwright install --no-shell chromium ffmpeg`，把 Playwright 版本、CfT 修订号与版本、可执行文件路径写入 `runtime.json`。需要 Node ≥ 20 与 npm。不读写共享的 ms-playwright 缓存，不安装 WebKit/Firefox/headless shell。
- **启动**：只有守护进程启动浏览器，执行者只经 CDP 连接，不自己启动或安装。每个 检出×身份 一个 CfT：`--user-data-dir=<数据目录>/browsers/<检出>/<身份>/profile --remote-debugging-port=<端口> --no-first-run --no-default-browser-check --use-mock-keychain`，缺省加 `--headless=new`，不加 `--enable-automation`；输出写同目录 `chrome.log`。`/json/version` 15 秒内有应答才算就绪。
- **端口**：`9400 + slot × 20 + 身份序号`。序号：`default` 为 0，其后按各 Web 入口声明顺序；超过 20 个身份报错。端口已被占用时直接报错，不换随机端口。
- **租约**（`browser_instances.holder`）：持有者 `run:<编号>`、`retest:<检出>`、`distill:<检出>` 或 `human`。别的持有者占用时返回 `browser-busy`；状态 crashed 时返回 `browser-crashed`；启动即退出不抛错，问题写进 brief.md，相关场景应记受阻。运行提交或放弃时释放。
- **真人登录与交还**：`browser open --identity X --headed` 以有界面窗口打开（缺省地址为该检出第一个 Web 入口的地址），并持有 `human` 租约；窗口已在运行时只取得租约，不重启，正在运行的无头浏览器则重启为有界面窗口（profile 不变）。用户在窗口里登录、完成人机验证或二次验证后，`browser release --identity X` 交还：只释放 `human` 租约，进程、窗口与页面保持运行，下一次取得租约的运行直接沿用这个窗口，不重启；身份被运行持有时返回 `browser-busy`。不带 `--headed` 的 `browser open` 遇到运行中的有界面窗口时沿用它，不取得租约。`browser close` 结束浏览器并释放租约，之后的运行重新以无头启动。出现 “browser may not be secure” 时停止，该身份的场景记受阻；不回退到日常 Chrome，不改 UA 或参数绕过。
- **崩溃**：监视线程等待进程退出。非主动停止的退出记录退出码、信号名、日志路径与启动后写出的 `~/Library/Logs/DiagnosticReports/Google Chrome for Testing*.ips`，并登记一条“浏览器崩溃”待人处理。租约期间不自动重启，下次取得租约时再启动。30 分钟内异常退出两次进入 `crashed`，须人运行 `browser open --identity X --reset-crash`。
- **身份计数**：真实登录或请求验证码前，执行者运行 `identity record --identity X --kind login|code`；达到 `limits` 上限时拒绝（`identity-limit`）并登记待人处理。

## 7. 守护与沉淀

### regression.yaml

位于 `<scenarios.path>/regression.yaml`（不被当作场景文件），随检出走，严格 YAML。

```yaml
version: 1
commands:
  unit: {repo: app, cwd: ., argv: [uv, run, --group, dev, pytest, -q, "--junitxml={junit}"], timeout: 900}
  web:  {repo: app, cwd: acceptance/specs, argv: [node, "{playwright}", test, --reporter=junit], timeout: 1800, browser: merchant}
guards:
  "orders#SOP-12-P01":
    fingerprint: <场景指纹>
    acceptance: {result: <结果编号>, executor: <会话>, product_digest: <摘要>}
    tests: [{id: t1, repo: app, path: tests/test_orders.py, sha256: <摘要>, name: "test_orders::test_rename", layer: unit, command: unit}]
    coverage:
      expected: [{index: 0, text: "…", tests: [t1]}]
      forbidden_effects: [{index: 0, text: "…", tests: [t1]}]
    detection: {method: mutation, assertion: "…", evidence: <证据 sha256>}
    ledger: []
```

- `commands.<编号>`：`repo`、`cwd`（仓内相对，缺省 `.`）、`argv`（不经 shell）、`timeout`（1～7200，缺省 900）、`browser?`（身份编号）、`env?`（大写变量名，不能 `SOP_` 开头）。`{junit}` 替换为 junit 输出路径，`{playwright}` 替换为运行时的 Playwright CLI。
- `tests[].layer`：`unit`、`api`、`component`、`browser`；`detection.method`：`mutation` 或 `known-defect`。

**守护有效**，缺一不可：

1. 场景存在且当前指纹等于 `fingerprint`；
2. 引用的命令存在、仓库在检出中；每个测试文件是仓库内普通文件且 sha256 一致；
3. `coverage` 按顺序逐条覆盖当前全部 `expected` 与 `forbidden_effects`，`index`、`text` 与当前一致；
4. 检出能力证据（`detection.evidence`）在证据库中；
5. `acceptance.result` 是同文件、同场景、同指纹的独立真实入口通过；
6. 该指纹与测试摘要没有登记过漏检。

索引结构不合格时全部守护无效，原因显示在场景状态里。

### retest

`sop-board retest` 交给守护进程执行，不启动 AI，用来发现代码改动带来的回归：

1. 缺省取当前检出全部非草稿场景（`--scenario` 可指定），只运行守护有效的，其余列在 `not_guarded` 并附原因；`--dry-run` 只输出场景与将运行的命令。
2. 每条引用到的命令只运行一次：工作目录为检出对应仓库下的 `cwd`，`start_new_session`，超时结束整个进程组。环境为守护进程环境去掉 `SOP_*`，加上该检出环境运行中时的入口地址变量、命令 `env`、`PLAYWRIGHT_JUNIT_OUTPUT_NAME`、`PYTHONDONTWRITEBYTECODE=1`；声明了 `browser` 的命令先拉起环境、取得浏览器租约，再注入 `PW_CDP_ENDPOINT`、`PLAYWRIGHT_BROWSERS_PATH`、`PW_NODE_ROOT`。日志与 junit 写在 `<数据目录>/retests/<时间>-<检出>/`，并作为证据入库。
3. 场景的守护运行记为通过须同时满足：相关命令退出码 0、没超时；每个声明的测试名都出现在 junit 中且为通过（跳过、缺失都不算；匹配完整名、以其结尾或测试标题相同）；运行前后产品快照不变。
4. 每个场景写一条 `guard_runs`（pass/fail + 原因 + 快照）。通过不改变状态；未通过的场景转为待复测并显示原因，之后经真实入口通过，或守护重新运行通过，就回到通过。

### 漏检

某场景在同一指纹上最近一次守护运行通过，之后真实入口结果或人工复测为失败（且其间没有更晚的通过）时，按 场景 + 指纹 + 测试摘要 记一条 `guard_misses`。该守护随即失效。重新沉淀时，`acceptance` 须是漏检之后的新独立通过，且测试摘要须与漏检时不同。

### 沉淀

- `distill prepare [键…]`：缺省为全部待沉淀场景；指定的场景须为待沉淀（`not-distillable`）。同一检出只能有一个进行中的沉淀（`distill-prepared`）。工作目录 `<数据目录>/distills/<编号>/` 含 `prompt.md`、`scenarios.yaml`、`acceptance.json`（独立通过的结果编号、执行者、运行目录）、`repos.json`。会话按随包 regression-distill 技能在当前检出写测试（不提交），写出 `guard.json`。
- `distill submit`：给出 `--work` 时须是进行中的沉淀（`work-state-invalid`）；缺省读进行中沉淀目录里的 `guard.json`（YAML 或 JSON，结构同 regression.yaml 的 `commands` 与 `guards`；`detection.evidence` 写证据文件路径，相对 guard.json 目录）。守护进程逐条核对：acceptance 为同指纹独立通过、测试文件存在并重新计算 sha256、逐条覆盖、detection 完整、漏检后的条件；同名命令与已有定义不同则 `report-invalid`；`layer: browser` 的测试另做 spec 保真检查（基线为 `git show HEAD:<路径>`；HEAD 还没有该文件时，以产生独立通过的那次运行目录中的同名 spec 为基线，都没有时只做静态检查）。不合格返回 `distill-invalid`。全部合格后按 retest 规则实际运行一次，未通过返回 `distill-tests-failed`；通过后合并写入 regression.yaml、记录守护运行，测试与索引留作未提交改动供开发者审核。
- 工作台“沉淀”按钮仅本机可用，作用是启动一个无头沉淀会话。

## 8. 修复

| 步骤 | 命令 | 规则 |
| --- | --- | --- |
| 认领 | `fix prepare [键…] [--session]` | 缺省为当前检出全部失败或受阻场景；指定的须为失败/受阻（`fix-scenario-invalid`）。同一检出只能有一个 open 的修复（`fix-open`）；场景已在已提交/已复验修复中时 `fix-already-owned`。目录 `<数据目录>/fixes/<编号>/`：`prompt.md`、`problems.json`（最近 5 条结果与证据路径）、`scenarios.yaml`、`repos.json`、`scenario-files.json`；记录准备时的产品文件清单 |
| 提交 | `fix submit [--fix N]` | 读 `fix.json`：`{"root_cause": 非空, "product_problem": bool}`。场景文件被改动则 `scenario-file-protected`（修复只改产品代码）。判定为产品问题却没有产品改动则 `fix-no-changes`。`product_problem: false` 时修复记为 abandoned 并登记待人处理；否则为 submitted，记下改动文件 |
| 独立复验 | 另一会话 `run prepare --fix N` | 修复须为 submitted/verified，且在同一检出。实现者自动为修复会话；不接受 not-executed。全部问题场景都实际通过、且执行者不是修复会话时，修复进入 verified，并记下复验时各改动文件的内容摘要 |
| 关闭 | `fix close [--fix N]` | 须为 verified（`fix-validation-required`）；改动文件须已提交且工作区与 HEAD 一致（`fix-commit-required`）；提交内容须与复验时一致（`fix-content-changed`）。关闭后之前失败/受阻（及人工不通过）的问题场景显示为待复测 |
| 放弃 | `fix abandon --fix N` | open/submitted/verified 可放弃 |

修复状态：`open` → `submitted` → `verified` → `closed`，或 `abandoned`。

## 9. 编写

所有编写都直接改产品仓库里的文件，开发者审核提交；看板不存场景版本，也不回写场景内容。

### 场景文件格式

```yaml
metadata:
  requirement: …
  version: 3            # 文字或数字
  owner: …
  status: draft         # 可选，draft 或 confirmed，缺省 confirmed
  sources: [{ref: …, version: …, confirmed_by: …}]   # 非空
  tags: [..]            # 可选
sops:
  - id: N1
    title: …
    actor: …
    entry: …
    must: [..]               # 可选
    negative_exempt: …       # 可选，没有反向场景时的理由
    scenarios:
      - {id: P01, polarity: positive, title, actor, priority, verify: <入口名>,
         initial_state: [..], steps: [..], expected: [..], forbidden_effects: [..],
         negative_type?, refs?, evidence_hints?, tags?}
```

只接受 JSON 数据类型的 YAML（无锚点、别名、标签、重复键，数字有限）。节点编号与场景编号各自唯一，场景编号不含 `#`；`initial_state`、`steps`、`expected` 非空。每个节点至少一个正向场景，并有反向场景或 `negative_exempt`。反向场景的 `negative_type` 取：`empty-input`、`over-length`、`not-found`、`permission`、`cross-tenant`、`duplicate`、`concurrency`、`disconnect`、`timeout`、`archived`、`invalid-input`、`incomplete-input`、`interrupted`、`stale-input`、`tampered-input`、`other`。`verify` 须为已声明的入口。问题带行号返回（`scenario-file-invalid`）。

### 收件箱、合并与问答

- **上传**：内网页面或 `inbox add` 上传 UTF-8 文本（≤2 MiB）。`cases` 按用例格式校验：`metadata{module, feature, owner, last_reviewed: YYYY-MM-DD, tags?}`，`test_cases[]{id: TC_<大写>_<三位数字>, title, priority: P0\|P1\|P2, type: functional\|ui\|security\|performance\|accessibility, steps, expected_results, tags, description?, preconditions?, auto?}`；`sop` 须含六个标题（见下）。状态 pending → merged / rejected（`inbox done|reject`）。
- **合并**：`merge prepare [--inbox N]` 取待合并用例（缺省全部），当前场景文件须先合格；目录 `<数据目录>/merges/<编号>/` 含 `inbox/` 原文、`answers.json`、`before.json`、`prompt.md`。会话直接编辑场景与用例文件，有疑问在对话里问或用 `ask`。`merge submit` 校验全部场景文件，输出场景差异（新增、移除、内容改动，以及 actor/verify/polarity 变化），把收件箱条目标为已合并。
- **问答**：`ask --about inbox:N|<键>|<文件名> --text …` 挂到看板“待答复”；测试同学在内网答复（须填姓名）；会话用 `answers [--about] [--unanswered]` 读取。确认人取操作者或答复人，记在场景 `refs` 与 `metadata.sources`。

### 业务 SOP

- `sop prepare --source <资料>...`（最多 32 份、每份 ≤20 MiB）把资料复制到 `<数据目录>/authoring/<编号>/sources/S<n>/`，写 `sources.json` 与 `prompt.md`。
- 会话先逐份核对场景目录中已有的 SOP，找出业务交叉（见[标准](standard.md)第 5 节）；新规则改变已有规则口径的，经使用者确认后同时改正已有 SOP（删去其文末确认记录后重新 `sop submit`）。然后在场景目录写 SOP Markdown，二级标题须有：业务目标与范围、角色、业务规则（其下 `###`/`####` 的 `R<n>` 规则小节）、流程图、禁止的副作用、未覆盖范围。
- `sop submit --file <场景目录下的 .md>` 校验后在文末追加 `## 确认记录`（确认人、时间、各资料摘要）；正文不能自带该节。

### 草稿

带 `metadata.status: draft` 的场景文件就是草稿，不进入执行与复测。会话由 SOP 写草稿或合并用例时核对已有场景：业务交叉改变已有场景预期的，直接修改这些场景（指纹变化后回到未执行），不另写重复场景。测试同学在内网批注（目标为整个文件或某个场景，≤4000 字，须填姓名），批注存数据库。会话按批注修订后 `draft resolve <编号> [--reject]`。`draft confirm <文件>` 要求没有未处理批注、metadata 中有单独一行 `status: draft` 与块格式 `sources:`，把状态改为 `confirmed` 并在 sources 首项插入 `{ref: "sop-board draft confirm", version: <日期>, confirmed_by: <确认人>}`。

### 交互地图

Web 入口须声明 `atlas` 与 `spec_library`。地图在 `<spec_library>/atlas/<入口>.json`（结构见随包 `prompts/atlas.schema.json`），只描述页面、控件、文案与定位方式，不提供预期。源码位置写成 `<别名>:<路径>[:<行>]`，别名限于 `atlas.repos`，文件须存在。`atlas prepare --entry E [--mode stale]` 写提示词（stale 模式只更新过期页面）；`atlas submit` 校验后写 `<入口>.digests.json` 记录引用源码的 sha256；`atlas stale` 列出源码摘要变化的页面，地图不存在时为 null。

### 前置数据

```yaml
fixtures:
  environment: 测试环境说明
  scope: [可写入的数据范围]
  credential_env: [DB_URL]
  adhoc: false
  real_only: [被验收流程应产生的对象]
  recipes:
    - {name: seed-order, title: …, params: {ORDER_STATE: 说明}, creates: [..], run: [./fixtures/seed.py], check: [./fixtures/check.py]}
```

`fixtures run <配方> --param 名=值` 以数据目录为工作目录执行 `run`（参数名转大写注入环境，另注入 `SOP_CHECKOUT`、`SOP_REPO_<别名>`，超时 600 秒），成功后执行 `check`。`fixtures exec --reason … -- <命令>` 只在 `adhoc: true` 时可用。输出按凭据取值脱敏，留痕写 `fixture_runs`，带 `--run` 时同时写 `runs/<编号>/fixtures/<n>.json`；非零退出返回 `fixture-failed`。`real_only` 对象不得写库代替。

### 待人处理

`attention add --kind K --text …`，种类：人机验证、OAuth 授权、账号登录、第三方后台设置、权限放行、邮箱验证、环境问题、前置数据、浏览器崩溃、其他。看板“待人处理”页只读，解决用 `attention resolve <编号>`。浏览器崩溃、身份计数超限与非产品问题的修复会自动登记。

## 10. 报告与看板页面

`report` 导出当前检出的 `report.md`（缺省目录 `<数据目录>/reports/<检出>-<时间>/`，不写进产品仓库）：快照与各仓提交、各状态计数、Agent 通过率与一次通过率、人工复测覆盖与通过率、Agent 独立通过的人工确认率与复核覆盖、独立性计数；每个场景的状态与原因、最近一次守护运行（注明不是真实入口执行）、最近 5 条结果（取证时的快照、独立性、finding、证据 sha256）和人工复测；然后是误判明细与待人工复核；最后列出未执行范围（未执行、待复测、草稿）。`--zip` 另写 `evidence.zip`（含报告与所引证据原文）。正文按凭据取值脱敏。

统计口径（全部取当前检出的结果记录，不限当前场景文件）：

| 指标 | 分子 / 分母 |
| --- | --- |
| Agent 通过率 | 各指纹最新 Agent 结果中的通过 / 其中通过与失败 |
| 一次通过率 | 各指纹首条 Agent 结果中的通过 / 其中通过与失败 |
| 人工复测覆盖 | 有 Agent 结果且有人工标注的指纹 / 有 Agent 结果的指纹 |
| 人工复测通过率 | 各指纹最新人工标注中的通过 / 全部最新人工标注 |
| Agent 独立通过的人工确认率 | 样本中人工结论为通过的 / 有人工结论的样本 |
| 复核覆盖 | 有人工结论的样本 / 全部样本 |

样本是每一条经真实入口的 Agent 独立通过（§1）。人工标注登记时，`of_result` 记当时该场景当前指纹最新一条 Agent 结果（不论其间是否已有别的人工标注），`product_digest` 记标注时的产品快照（版本记录）。样本的人工结论取 `of_result` 指向它的最近一条人工“通过”或“不通过”，不比较快照；只有阻塞或需求变更的样本不计入分子分母，按最近一条的结论计入“未决”，报告总是写出“未决：阻塞 N，需求变更 M”。分母为 0 的比率在报告与概况中显示为“—”。`report --file` 只筛选场景各节、待人工复核与未执行范围，概况中的统计与误判明细仍按整个检出。人工结论为不通过的样本列为误判明细：场景键、运行编号、Agent 执行时间、复测时间、复测人、缺陷链接、说明；执行者只在本机报告中显示。

| 路由 | 级别 | 内容 |
| --- | --- | --- |
| `GET /` | lan | 跳转到 `/p/<项目>` |
| `GET /p/<项目>?checkout=<名称>#<场景键>` | lan | 场景工作台：左侧检出切换、搜索、状态筛选（含待沉淀、待人工复核与计数；待复测、失败、待人工复核、未执行、通过在计数为 0 时也显示）与 文件→节点→场景 树；不带 `#场景键` 时右栏是概况，显示各状态与徽标计数、Agent 独立通过的人工确认率与复核覆盖；右侧标题与徽标、状态与原因、四要素（附守护覆盖）、最新结果与证据（截图、视频、trace、文本）、复测标注、本机按钮、历史 |
| `GET /p/<项目>/runs` | lan | 启动记录与运行记录 |
| `GET /p/<项目>/report[?file=&download=1]` | lan | 报告预览与 Markdown 下载 |
| `GET /p/<项目>/attention` | lan | 待人处理 |
| `GET /p/<项目>/inbox` | lan | 上传用例/SOP、待答复问题 |
| `GET /p/<项目>/drafts` | lan | 草稿与批注 |
| `GET /p/<项目>/sop` | lan | 场景目录下的 SOP 原文渲染 |
| `GET /p/<项目>/atlas` | lan | 交互地图与过期页面 |
| `GET /api/board`、`/api/scenario?key=`、`/api/runs`、`/api/annotations` | lan | 工作台数据；`/api/board` 的每个场景带 `distill`、`review` 徽标，`counts` 含 `distill`、`review`，`stats` 含 `agent_confirm_rate`、`agent_review_coverage`（numerator、denominator、value） |
| `GET /evidence/<sha256>/<文件名>` | lan | 证据下载；视频支持 Range；trace zip 允许 trace.playwright.dev 跨域读取 |
| `POST /api/mark` | lan | 复测标注：`verdict` 取 pass/fail/blocked/changed，`defect`、`note`、`by` 都可不填（本机缺省为操作者，内网不填记为未署名） |
| `POST /api/inbox`、`/api/answer`、`/api/annotate` | lan | 上传、答复、批注；须有姓名（顶栏署名填一次，浏览器 localStorage 记住，各页面共用） |
| `GET /p/<项目>/sop`、`GET /api/sop?checkout=<名称>` | lan | 业务 SOP：场景目录下的 Markdown 按 1～3 级标题分节，左树右文；含“答复”小节的节标出已答复 / 待答复 |
| `POST /api/launch`、`/api/retest`、`/api/distill` | local | AI 重跑此场景、运行守护、沉淀 |
| `GET /api/launches/<编号>/log` | local | 启动日志末尾 512 KiB |
| `/local/health`、`/local/env/{up,down,status}`、`/local/browser/{lease,release,crashes,open,close,status}`、`/local/retest`、`/local/distill/submit`、`/local/launch`、`/local/launch/stop` | token | CLI 调用 |

## 11. 错误码

| 错误码 | 含义 / 处理 |
| --- | --- |
| `project-not-found` | 找不到项目或当前仓库未登记；`init` 或写 `--project` |
| `project-format` | 旧格式数据目录或数据库；用 `init` 新建项目，不迁移 |
| `daemon-not-running` | 守护进程没运行或连不上；运行输出中的 `sop-board serve …` |
| `invalid` | 其他参数或配置问题（含配置不合格、检出不存在等），见 problems |
| `env-busy` | 端口被其他检出或项目的环境租用（`port_step: 0` 时写明占用的检出、正在使用的运行编号与 `env down` 命令），或端口已被其他进程监听（附 lsof 信息） |
| `browser-busy` | 该身份浏览器被真人登录或其他运行占用；真人占用时，真人完成后 `browser release` 交还 |
| `browser-crashed` | 30 分钟内两次异常退出；`browser open --reset-crash` |
| `browser-runtime-missing` | 未安装运行时；`browser install` |
| `session-required` | 取不到执行者会话编号；加 `--session` |
| `run-prepared` | 本检出已有准备好的运行；提交或 `run abandon` |
| `run-not-prepared` / `run-not-found` | 运行不是 prepared / 不存在 |
| `nothing-to-run` | 范围为空 |
| `scenario-not-found` | 场景键或编号不存在或不唯一 |
| `draft-not-runnable` | 草稿场景不能执行或复测 |
| `scope-invalid` | `--fix` 与 `--scope` 同用 |
| `launch-running` | 本检出已有运行中的启动；等待或 `launch stop` |
| `launch-not-found` | 启动不存在 |
| `not-distillable` / `nothing-to-distill` / `distill-prepared` | 场景不是待沉淀 / 没有待沉淀 / 已有进行中的沉淀 |
| `report-invalid` | guard.json 无法读取、缺 guards 或命令定义冲突 |
| `distill-invalid` | 沉淀核对未通过（独立通过、覆盖、检出能力、漏检、保真检查） |
| `distill-tests-failed` | 守护命令实际运行未通过 |
| `fix-open` / `fix-already-owned` / `fix-scenario-invalid` / `nothing-to-fix` | 修复认领冲突或范围不对 |
| `fix-answer-invalid` / `fix-no-changes` | fix.json 不合格 / 产品问题但无改动 |
| `scenario-file-protected` | 修复期间场景文件被改动 |
| `fix-state-invalid` / `fix-checkout-mismatch` / `fix-scenario-missing` | 修复状态、检出或问题场景不符合复验条件 |
| `fix-validation-required` | 关闭前须独立复验通过 |
| `fix-commit-required` | 修复改动文件未提交 |
| `fix-content-changed` | 提交内容与复验时不一致，须重新复验 |
| `scenario-file-invalid` | 场景文件不合格（带行号） |
| `cases-invalid` / `sop-invalid` / `upload-invalid` / `upload-too-large` | 上传或提交内容不合格 |
| `nothing-to-merge` / `inbox-invalid` / `inbox-state-invalid` | 合并范围不对 |
| `work-not-found` / `work-state-invalid` | 没有进行中的 merge/author/atlas 工作 |
| `draft-not-found` / `draft-annotations-open` / `draft-format` | 草稿确认条件不满足 |
| `atlas-not-declared` / `atlas-invalid` | 入口未声明 atlas/spec_library / 地图不合格 |
| `fixtures-not-declared` / `recipe-not-found` / `adhoc-not-allowed` / `fixture-failed` | 前置数据问题 |
| `identity-not-found` / `identity-limit` | 身份未声明 / 计数达上限（已登记待人处理） |
| `local-only` / `token-required` / `cross-origin` / `json-required` | 访问级别或请求格式不符 |
| `internal-error` | 守护进程未预期错误（HTTP 500） |

### 数据库表

`board.sqlite3`，格式 6（`PRAGMA user_version=6`），共 18 张表，没有任务状态机：

| 表 | 内容 |
| --- | --- |
| `meta` | 格式号 |
| `checkouts` | 名称、槽位（均唯一）、kind main/worktree、路径、adapter 返回的变量、移除时间 |
| `work` | run/fix/distill/merge/author/draft/atlas 工作：检出、会话、操作者、实现者、修复、启动、目录、范围、快照、状态 prepared/submitted/abandoned；同一检出的 run 与 distill 各只能有一个 prepared |
| `results` | agent 与 manual 结果：文件、场景、指纹、快照、product_changed、状态、actual/finding/defect/note、reused_from（只出现在早先的保留通过上，现行运行不再写入）、of_result、执行者、实现者、独立性、复测人 |
| `evidence`、`result_evidence` | 按 sha256 存储的证据（文件在 `evidence/<前两位>/<sha256>`）及结果引用 |
| `guard_runs`、`guard_misses` | 守护运行（快照、测试摘要、pass/fail、原因、输出证据）与漏检 |
| `fixes` | 修复：会话、状态、问题、基线文件清单、fix.json、改动、复验运行与文件摘要 |
| `launches` | 无头启动：类型、runner、范围、会话、pid、日志、状态；同一检出只能有一个 running |
| `environments` | 各检出 adapter 环境：状态 starting/ready/failed/stopped、pid、端口、ready 变量、快照、日志 |
| `browser_instances` | 检出×身份浏览器：端口、pid、profile、有无界面、状态、租约持有者、退出码/信号、日志、崩溃报告、近期崩溃 |
| `inbox`、`questions`、`annotations` | 上传、提问与答复、草稿批注 |
| `attention`、`identity_events`、`fixture_runs` | 待人处理、身份登录与验证码计数、前置数据留痕 |

端口租约不在数据库中：守护进程用 `/tmp/sop-board-port-<uid>-<端口>.lock` 文件锁跨项目、跨检出租用端口，并在锁文件里记录占用的项目与检出。
