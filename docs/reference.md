# Flow Board 参考手册

Flow Board（命令 `sop-board`）是在开发者本机运行的业务流程验收工具。场景写在产品仓库的 YAML 文件里，Agent 会话经 CLI 准备、执行、提交；看板守护进程负责网页、按检出启停产品环境、Chrome for Testing 浏览器代理和本机无头会话。本手册只描述现行行为。验收口径以 [业务验收与质量改进标准](standard.md) 为准，搭建步骤见 `sop-board guide`，Web 驱动见 [playwright-acceptance.md](playwright-acceptance.md)。

## 1. 概念

| 概念 | 含义 | 存放 |
| --- | --- | --- |
| 项目 | 名称 + 若干 Git 仓库，是一台机器上对一个产品的搭建。仓库身份是规范化后的 origin（`host/path`，去掉 `.git`） | `~/.sop-board/<名称>/`（`project.json`、`config.yaml`、`board.sqlite3`） |
| 产品 | 共享的单位：多人协作时各机器的项目绑定到共享库里的同一个产品（ID + 名称）；没配共享库时项目自己就是产品（ID `local`） | 共享库 `products`；绑定在本机 `project.json` 的 `product` |
| 机器 | 一个数据根目录：生成一次的随机标识与显示名（缺省“操作者@主机名”）；记录里的“产出机器”都指它 | `<数据根目录>/machine.json` |
| 业务记录库 | 结果、标注、守护运行、沉淀登记、修复、运行记录的业务部分、证据索引、收件箱、问答、批注、同意、待人处理：配了 `shared_database` 时是共享的 PostgreSQL，否则是本机 `board.sqlite3`，两者同一套表与 SQL | 见“数据库表” |
| 检出 | 项目各仓库的一套工作副本。`main` 是 config.yaml 登记的路径，槽位固定为 0；worktree 检出各有槽位 ≥1 | `checkouts` 表 |
| 场景文件 | 产品仓库 `scenarios.path` 下的 `*.yaml`，是场景的唯一真相。看板从检出目录现读（按 mtime 与大小缓存），从不回写 | 产品仓库 |
| 场景键 | `<文件名去掉 .yaml>#<场景编号>`。场景编号在全部文件中唯一时，命令可只写编号 | 计算得出 |
| 指纹 | `initial_state`、`steps`、`expected`、`forbidden_effects` 四要素的 sha256（换行统一为 `\n`）。改标题、优先级等不改变指纹 | 计算得出 |
| 产品快照 | 检出各仓库当前内容（含未提交、未跟踪文件）的摘要，记录取证时的代码版本，不参与状态判定。场景目录、`snapshot_exclude`、`regression.yaml` 引用的测试文件不计入 | 计算得出 |
| 运行 | 一次场景执行或复验，绑定检出、准备时的快照、执行者与实现者；提交后记下未执行清单及原因、服务停启记录。运行编号由本机分配 | 本机 `local_work.dir` 指向的目录（缺省 `runs/<编号>/`，占用时顺延后缀）；提交时写进业务记录库 `work`（机器标识 + 本机库标识 + 本机编号唯一），停启随之写进 `work.services` |
| 未执行 | 某次运行里某场景因本机暂时不具备条件没有执行：谁、什么时候、缺什么。不是结果，不参与状态推导，场景详情与报告显示最近一次 | 运行摘要（`work.result.not_executed`） |
| 结果 | 运行中某场景的结论与证据，绑定指纹，记录准备时的快照、产出机器、检出与分支（场景文件所在仓库的当前分支）。通过与失败是结论，受阻与跳过是非结论。证据文件按 sha256 存在产出机器上。从旧看板导入的结果 `product_digest` 为空，即代码版本未记录 | `results`（kind=agent）；证据文件在数据根目录 `evidence/`，索引在 `evidence` 表 |
| 标注 | 人工复测结论（通过 / 不通过 / 阻塞 / 需求变更，可附缺陷链接、说明、复测人），都是结论，记下标注时看板的机器、检出与分支 | `results`（kind=manual） |
| 守护 | 场景到低成本测试的关联，绑定场景指纹与测试文件摘要 | 产品仓库 `<scenarios.path>/regression.yaml` |
| 守护运行 | 一次在某快照上运行守护命令的结果，记下产出机器、检出与分支；未通过是非结论，只影响产出它的检出 | `guard_runs` 表 + 输出证据 |
| 沉淀登记 | 某场景某指纹已由谁、在哪台机器、用哪份测试（测试摘要）沉淀为守护 | `distillations` 表 |
| 修复 | 失败场景 → 会话改产品 → 另一会话独立复验 → 提交后关闭。记录共享，操作只在产出它的机器上 | `fixes` 表（业务部分）+ 本机 `local_fixes.dir` 指向的目录（缺省 `fixes/<编号>/`，占用时顺延后缀） |

### 场景状态

状态按检出推导，不存储，只看当前指纹的记录。哪些场景需要执行由场景内容（指纹）决定，代码改动不改变状态。参与推导的记录：

- **结论**（Agent 的通过与失败、人工标注、修复关闭）在产品内共享，按分支归属。**本分支优先**：当前指纹在本检出的分支上有结论时只用本分支的；一条都没有（或检出不在任何分支上）时用全部分支的，状态旁标出来源分支（`source_branch`）。**已并入**的结论也算本分支的（已定决策 28）：结论所在运行准备时的快照（`work.snapshot`）里各仓库的 `dirty` 都为 0，结果的 `product_changed` 为空，并且快照里每个仓库的 `commit` 都是本检出对应仓库 HEAD 的祖先；提交在本机找不到、仓库在本机没有登记的不算。已并入的结论带 `source_branch` 与 `merged: true`，状态旁与历史里显示“来自 <分支>（已并入）”。祖先关系按提交对缓存。
- **非结论**（Agent 的受阻与跳过、守护未通过）只取本机本检出产出的；别的机器与检出的显示在场景详情的“其他机器上的情况”，不改变本机状态。
- 先后只比较业务记录库分配的统一序号（结果、标注、守护运行、修复关闭与运行记录共用一串），执行时间只用于显示。
- 历史列出参与本看板推导的记录，每条写明产出机器与分支：当前内容（当前指纹）上按上面的规则选出的记录；旧内容（场景改过之前的指纹）上只列本分支（含已并入）的结论和本检出的非结论，别的分支上内容不同的结果不列入，也不回落到全部分支（检出不在任何分支上时，旧内容只列本检出的非结论）。概况的通过率与人工确认率用同一组记录。没配共享库的单机项目用同一规则。

| 状态 | 键 | 条件 |
| --- | --- | --- |
| 草稿 | `draft` | 文件 `metadata.status: draft`；不进入执行与复测 |
| 未执行 | `not-run` | 当前指纹没有结果 |
| 通过 | `pass` | 最新结果为通过，且没有更晚的未通过守护运行 |
| 待复测 | `retest` | 本检出当前指纹最近一次守护运行未通过，且晚于最新结果；或最新为失败/受阻、之后修复已关闭；或人工不通过后修复已关闭、尚无新的人工通过 |
| 失败 / 受阻 / 跳过 | `fail` / `blocked` / `skip` | 最新结果如此；人工标注不通过按失败、阻塞按受阻显示 |
| 需求变更 | `changed` | 最新结果是人工标注的需求变更；场景文件按新需求修改后指纹变化，回到未执行 |

**经真实入口的 Agent 独立通过**指 kind=agent、`independent`、`reused_from` 为空的通过。执行期间产品有变化的通过也算，变化的路径只作版本记录。

**待沉淀**是徽标，只与通过并存：状态为通过，选出的记录中有经真实入口的 Agent 独立通过，且没有有效守护。共享库里有当前指纹的沉淀登记、而本检出没有这条守护或测试摘要不同时，不显示待沉淀，仅在状态为通过时显示“已由 <沉淀人> 沉淀，本机未同步”；守护引用的仓库本机没有登记时显示“本机无法运行”，也不显示待沉淀。

**待人工复核**也是徽标，可与待沉淀并存：状态为通过，且当前指纹最新一条结果是经真实入口的 Agent 独立通过。之后有任一人工标注即消失。

**草稿审阅状态**只用于草稿场景，状态仍是草稿：**有批注**（该场景有未处理的批注）、**已同意**（当前指纹上有未撤销的同意，且该场景没有未处理的批注）、**待审**（其余）。同意按“文件 + 场景 + 指纹”记录，不区分检出；场景四要素一改，原有同意失效；批注不采纳、场景未改时原有同意仍有效。文件级批注不影响单条场景的审阅状态，但阻止确认。

待复测的原因写在状态旁，例如“守护测试未通过：<原因>”“修复已关闭，等待复测”“修复已关闭，等待测试同学复测”。

## 2. 安装与搭建

```sh
uv tool install <sop-board 检出或包地址>
sop-board skills install          # 把 sop-board、regression-distill、playwright-acceptance 链接到 ~/.agents/skills 与 ~/.claude/skills
sop-board init <名称> <仓库目录>...
# 多人共享时：config.yaml 写 shared_database，密码写在本机 config.yaml 的 password 或由 ~/.pgpass 提供，然后
sop-board init <名称>             # 列出共享库里的产品，以 product-choice-required 结束
sop-board init <名称> --join <ID> # 或 --new [--product <产品名称>]
sop-board serve <名称>            # 另开终端或在会话中启动守护进程
sop-board browser install         # 有 Web 入口时
sop-board check
```

- `skills install` 只更新本安装器拥有的链接（记录在各目录 `.sop-board-links.json`），其他来源的同名目录列出并跳过。
- `init`：每个目录须是 Git 工作树根目录、有可规范化的 origin、不能重复；数据目录 `<根目录>/<名称>` 不能已存在，也不能位于仓库内。名称以字母或数字开头，只含字母、数字、`.`、`_`、`-`，最长 64。别名取目录名小写化（不以字母开头时加 `r-`，重复时加 `-2`…）。写出 `project.json`（`{"format": "sop-board-project-6", "name": …}`，绑定产品后另有 `product: {id, name}`）与只含 `repos`、`identities` 的 `config.yaml`。对已登记的项目不写仓库目录时，`init <项目> [--join <ID> | --new [--product 名称]]` 列出或绑定共享库里的产品，见 §12。
- 运行时根目录是 `SOP_BOARD_HOME`（缺省 `~/.sop-board`）；`--data` 指定项目所在的根目录。本机操作者文件 `~/.sop-board/operator` 与浏览器运行时 `~/.sop-board/runtime/` 也在这里。
- 旧格式数据目录（含 `workspace.json` / `workspace.sqlite3`、`project.json` 格式不符、数据库 `user_version` 不是 7）一律返回 `project-format`，不读取、不迁移。
- 数据根目录（`SOP_BOARD_HOME` 或 `--data`）下另有 `machine.json`（本机标识与显示名，第一次需要时生成一次）与 `evidence/`（本机产出的证据文件，按 sha256 存放，不随项目数据目录重建而丢失）。共享库的搭建、产品的新建与加入见 §12。

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
| `shared_database` | 否 | 多人共享验收记录时的共享库 `{host, port（缺省 5432）, dbname, user, password（可选）}`；password 须为非空单行文字，省略时由 libpq 使用 `~/.pgpass` 或 `PGPASSFILE`。含密码的配置及上一份副本权限为 600。不写时业务记录在本机 `board.sqlite3`，见 §12 |
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
| `services` | 可选，服务名 → `{about: 一行业务说明（≤200 字）, stoppable: 布尔，缺省 false}`，按声明顺序列出。服务名 `[a-z][a-z0-9_-]*`，最长 64。声明后 adapter 按下文“服务契约”上报状态、响应停启 |
| `service_timeout` | 1～3600 秒，缺省 60：`env stop|start` 与自动恢复等服务状态变为目标值的最长时间 |

**adapter.env**：前台运行，就绪时输出一行 `sop-board: ready`，可在同一行后附 JSON 对象给出变量，例如 `sop-board: ready {"APP_URL": "http://127.0.0.1:8100"}`（变量名须合法且不以 `SOP_` 开头）。输出就绪即环境可用，不要求每个服务都在运行。输入变量：

| 变量 | 值 |
| --- | --- |
| `SOP_CHECKOUT` | 检出名称 |
| `SOP_REPO_<别名>` | 本检出该仓库路径（别名大写，`-` 换 `_`） |
| `SOP_BASE_<别名>` | 登记（main）仓库路径 |
| `SOP_PORT_OFFSET` | `slot × port_step` |
| `SOP_PORTS` | 实际端口 JSON 数组 |
| `SOP_ENV_LOG_DIR` | `<数据目录>/env/<检出>/`，输出写入其中 `<序号>.log`（按凭据取值脱敏） |

另外注入 `adapter.worktree` 为该检出返回的 `env`，以及守护进程环境中的凭据变量；继承环境里其他 `SOP_*` 变量被去掉。

**服务契约**（声明了 `services` 时；没声明的 adapter 只有整体就绪与否，`services` 为空）：

| 文件 | 写入方 | 内容与含义 |
| --- | --- | --- |
| `SOP_ENV_LOG_DIR/services/<名称>.state` | adapter | UTF-8 JSON 对象：`{"state": "running"}`、`{"state": "stopped"}`、`{"state": "absent"}` 或 `{"state": "crashed", "log": "<日志路径>"}`。依次为运行中、已停止、本机未搭建、异常退出；`log` 可选（相对路径按 `SOP_ENV_LOG_DIR` 解析），异常退出时应给出。状态变化时改写，建议先写临时文件再换名。其他字段忽略 |
| `SOP_ENV_LOG_DIR/services/<名称>.request` | 守护进程 | 存在表示请求停止该服务，删除表示请求启动。内容是 `{"action": "stop", "run", "by", "at"}`，只供排查，adapter 只看文件是否存在。只对 `stoppable: true` 的服务写 |

- adapter 轮询请求文件（建议 0.2～1 秒）并执行，完成后改写状态文件。守护进程等状态变为 `stopped`（停）或 `running`（启）才返回，超过 `service_timeout` 返回 `service-timeout`；停止超时时删除请求文件，撤回请求。
- 声明了但没有状态文件（或内容不合格）的服务显示为“未上报”（`unreported`，不合格时附 `problem`）；环境没有运行时各服务显示“环境未运行”（`down`）。异常退出的服务另给 `log_tail`（日志末尾 20 行，按凭据取值脱敏）。
- 服务意外退出时 adapter 写 `crashed`，不自动重启，环境整体照常就绪。
- 守护进程在启动环境前、停止环境后与自身启动时清空 `services/` 目录，旧状态与遗留的停止请求不会带进新环境。

**adapter.worktree**：输入 `SOP_TASK_DIR`（`<数据目录>/checkouts/<名称>`）、`SOP_FIX_BRANCH`（`sop-board/<名称>`）、`SOP_REPOS`（逗号分隔别名）、`SOP_BASE_<别名>`、有 `--from` 时 `SOP_BASE_COMMITS`（别名 → ref 的 JSON）。stdout 最后一行须为 `{"repos": {别名: 路径}, "env": {变量: 文字}}`，覆盖全部请求的仓库，env 不能用 `SOP_` 前缀。每个返回路径须通过三项核对：是工作树根目录且与登记仓库共用 common dir、检出了 `sop-board/<名称>` 分支、出现在 `git worktree list` 中。没有声明 worktree 时直接 `git worktree add -b sop-board/<名称> <数据目录>/checkouts/<名称>/<别名> <from 或 HEAD>`。

**adapter.cleanup**：输入同上，另有 `SOP_WORKTREES`（别名 → 路径 JSON，只含本检出自有的 worktree）。没有声明时逐个 `git worktree remove`。清理后目录仍存在则报错并保留检出记录。

### check

`sop-board check` 输出 `steps`，三项全部合格才退出 0：

1. **配置**：config.yaml、技能与入口校验。
2. **环境**：守护进程须在运行；在当前检出拉起 adapter.env 并等就绪。就绪以 adapter 输出就绪为准：没在运行的服务（`not_running`）与 ready 变量里缺失的入口地址（`missing_address_env`，入口名 → 变量名）只列出，不算失败，同时写在输出顶层。
3. **浏览器运行时**：`~/.sop-board/runtime/runtime.json` 存在且可执行文件在。

有一项不合格时以业务失败退出（退出码 1），按通用约定另给 `error_code: check-failed` 与 `problems`（每个不合格项一条：配置问题逐条列出，环境写明没有就绪，运行时写明缺失与安装位置）；`steps` 照常给出三项的完整结果。

## 3. 守护进程与访问范围

`sop-board serve [项目] [--port 8787] [--lan]` 在前台运行一个项目的守护进程，可在终端或 agent 会话中启动。它负责网页、各检出的 adapter 环境、浏览器代理、守护测试运行与无头会话启动器。启动时写 `<数据目录>/daemon.json`（pid、端口、URL、内网地址）与 `daemon.token`（权限 0600），清理上次遗留的环境进程组，认回有界面的浏览器、清理遗留的无头浏览器，保留核对不通过的有界面窗口、清空各检出的服务状态目录、把遗留的运行中启动记为 failed；收到 SIGINT/SIGTERM 时停掉全部环境、无头浏览器和会话启动，输出保留的有界面浏览器后删除状态文件。同一项目已在运行时拒绝再启动。

停止与重启用命令（已定决策 28）：

- `sop-board stop [项目]`：让守护进程按上面的规则正常停止，等它退出后输出保留的有界面窗口（身份、进程号、调试端口）。
- `sop-board restart [项目]`：换一个守护进程，不打断进行中的工作。旧进程停掉全部环境后退出，不结束任何浏览器（有界面的和无头的）和启动的会话，浏览器租约与 prepared 的运行保持原样。新进程沿用原来的端口与 `--lan`，在后台运行，输出追加到 `<数据目录>/serve.log`；启动时认回这些浏览器（无头的只认回 restart 交接下来的）与会话，租约照旧有效，并重新拉起原来在运行的环境。命令等新进程就绪后返回，输出列出保留与认回的浏览器和会话，以及正在重新拉起的环境。
- 重启期间 `daemon.json` 标明正在重启，CLI 等新进程就绪（最多 120 秒），不返回 `daemon-not-running`。环境重新就绪之前，每个 prepared 的运行记一条“环境随看板重启中断”（起止时间），提交时写进运行摘要的 `interruptions`。
- 两条命令在守护进程没运行时都返回 `daemon-not-running`。`restart` 在 Codex 沙箱里（有 `CODEX_SANDBOX`）拒绝执行，提示在终端运行。

CLI 需要环境、浏览器、守护测试或启动时，以本机令牌经回环 HTTP 调用守护进程；守护进程没运行时返回 `daemon-not-running`，并给出 `sop-board serve <名称>` 命令。`status` 也显示守护进程是否运行。

| 访问级别 | 谁可以调用 | 用于 |
| --- | --- | --- |
| `lan` | 任何连接（`--lan` 时监听 0.0.0.0） | 查看工作台与页面、人工复测标注、答复问题、批注草稿、同意草稿场景或撤销自己的同意、上传用例或 SOP |
| `local` | 只接受实际回环连接 | 工作台的“AI 重跑”“运行守护”“沉淀”按钮、启动日志 |
| `token` | 回环连接 + `Authorization: Bearer <daemon.token>` | `/local/*`：CLI 调用的环境、浏览器、retest、distill submit、launch |

写入（POST/PUT/DELETE）有 `Origin` 头时须与 Host 同源；有正文时须为 `application/json`，正文上限 64 MiB。非回环访问返回 `403 local-only`。

**内网遮蔽**：对非回环连接，除 `/assets/` 外的所有响应（含 zip 证据内的文本文件与响应头）都把凭据取值替换掉、隐藏凭据变量名，并把数据目录、登记仓库与各检出路径替换为 `<本机路径>`。接口本身也不向内网返回会话编号、执行者/实现者、运行目录、启动日志与命令；报告里执行者显示为“Agent 会话”；未预期错误只返回异常类型。

## 4. 命令一览

通用约定：

- 除 `status`（缺省文字，`--json` 输出 JSON）、`guide`、`playwright guide` 外，所有命令都输出 JSON，`--json` 可省。
- 退出码：0 成功；1 业务失败（`{"ok": false, "error_code", "problems": [{field, problem}]}`）；2 参数错误；`run wait` 超时为 3。
- 大多数命令接受 `--project/-p`、`--data`、`--checkout/-c`。
- 署名：登记自动用本机操作者（`~/.sop-board/operator`，否则 `git config user.name`）；别人确认或上传时用 `--as <姓名>`。没有操作者时要求先 `operator set`。`draft confirm` 例外：确认人是在看板上同意草稿场景的人，没有 `--as`。

| 分组 | 命令 | 要点 |
| --- | --- | --- |
| 搭建 | `init <名称> <仓库>... [--data]` | 见 §2 |
| | `guide` | 打印完整搭建指引 |
| | `operator [show\|set <姓名>]` | 查看来源或设置本机操作者（单行，最长 64） |
| | `skills install` | 安装随包技能链接 |
| | `serve [项目] [--port] [--lan]` | 见 §3 |
| | `stop [项目]` / `restart [项目]` | 停止看板 / 换守护进程而不打断进行中的工作；见 §3 |
| | `status [项目] [--json]` | 检出、操作者、快照、各状态计数、待沉淀数（顶层 `distill`）、待人工复核数（顶层 `review`）、草稿按审阅状态的计数（`draft_review`：`pending` 待审、`annotated` 有批注、`approved` 已同意）、`queues`（retest/fail/blocked/not-run）、`distill_queue`、`review_queue`、不合格场景文件、守护进程、待人处理；文字输出的场景行末尾总是列出“待沉淀 N；待人工复核 N”，有草稿时另起一行“草稿审阅：待审 N；有批注 N；已同意 N” |
| | `check` | 见 §2 |
| 检出 | `checkout list` | 全部检出及槽位、偏移、路径 |
| | `checkout show [名称]` | 含场景目录与各仓分支 |
| | `checkout create <名称> [--repo 别名]... [--from ref]` | 名称规则同检出名、不能是 main；`--repo` 只为这些仓库建 worktree；日志 `<数据目录>/checkouts/<名称>.log` |
| | `checkout remove <名称>` | 环境须先停；main 不能移除；记录标为已移除，槽位释放 |
| 环境 | `env up\|down\|restart\|status [--all]` | 经守护进程启停或重启当前检出环境；`status --all` 列出全部检出。输出 `state`、`address_env`、`variables`（ready 行的全部变量）、`services`（名称、业务说明、可停、状态、`stop_requested`，异常退出附 `log` 与 `log_tail`）、`not_running`、`missing_address_env` |
| | `env stop\|start <服务> [--run N]` | 运行取 `--run`，其次 `SOP_BOARD_RUN`；本检出有多个 prepared 运行又没指明时返回 `run-ambiguous`。只在本检出有 prepared 运行时可用（`run-required`），且这套环境上只有这一个 prepared 运行（`env-shared`，列出其他运行）；有服务被停着时别的运行要用这套环境就排队。只对 `stoppable` 的服务；等状态变为已停止或运行中才返回（`service-timeout`），停启记进该运行（`service_events`），提交或放弃时自动恢复。已在目标状态时返回 `changed: false`，不记录。运行之外恢复环境用 `env restart` |
| 浏览器 | `browser install` | 安装运行时与 CfT（不需要守护进程） |
| | `browser status [--identity]` | 各身份浏览器：`port` 为实际调试端口（运行中的，或最近一次启动所用的）、`default_port` 为缺省端口、`alive`、`endpoint`（运行中时）、持有者、最近一次交给的检出（`last_checkout`）与崩溃记录；守护进程未运行时只给运行时信息 |
| | `browser open [--identity] [--headed] [--reset-crash] [--url]` | 缺省身份 `default`；见 §6 |
| | `browser release [--identity]` | 真人完成后交还：只释放真人租约，窗口保持打开，之后的运行沿用；见 §6 |
| | `browser close [--identity]` | 结束浏览器并释放真人租约 |
| 执行 | `run prepare [--scope pending\|all\|键…] [--fix N] [--session S] [--identity I…] [--wait 秒]` | 见 §5；`--fix` 不能与 `--scope` 同用 |
| | `run submit --run N [--implementer X \| --implemented-by 操作者]` | 见 §5 |
| | `run abandon --run N` | 放弃并释放浏览器租约，自动恢复本轮停着的服务（输出 `restored`、`service_events`） |
| | `run launch [--scope …] [--fix N] [--implementer X \| --implemented-by 操作者]` | 由守护进程启动无头执行会话；启动前与 `run prepare` 共用范围核对，`pending` 由守护进程展开为具体场景；`--fix` 启动修复的独立复验 |
| | `run wait [--launch N \| --run N] [--timeout 1800]` | 等启动结束或运行离开 prepared；缺省为本检出最近一次启动；`0` 不限时 |
| | `retest [--scenario 键…] [--dry-run]` | 见 §7 |
| | `mark <键> pass\|fail\|blocked\|changed [--defect 链接] [--note 说明] [--as 姓名]` | 人工复测：通过、不通过、阻塞、需求变更；缺陷链接与说明可选；草稿拒绝 |
| | `launch status` / `launch stop --launch N` | 最近 20 次启动；停止发 SIGTERM 给进程组 |
| 修复 | `fix prepare [键…] [--session]`、`fix submit\|close\|abandon [--fix N]` | 见 §8；省略 `--fix` 时取本机本检出最近一个状态相符的修复；别的机器产出的修复返回 `fix-not-local` |
| 沉淀 | `distill prepare [键…] [--session]`、`distill submit [--report guard.json] [--work N]` | 见 §7 |
| 编写 | `inbox add <文件> [--kind cases\|sop] [--as]`、`inbox list [--all]`、`inbox show\|done\|reject <编号> [--note]` | 见 §9 |
| | `merge prepare [--inbox N]...`、`merge submit [--work N]` | |
| | `ask --about <inbox:N\|键\|文件名> --text … [--work] [--as]`、`answers [--about] [--unanswered]` | |
| | `sop prepare --source 文件...`、`sop submit --file <md> [--work] [--as]` | |
| | `draft annotations [文件] [--all]`、`draft resolve <批注编号> [--reject] [--note]`、`draft confirm <文件>` | 见 §9“草稿”；`draft confirm` 没有 `--as`（带上即参数错误） |
| 其他 | `atlas prepare --entry E [--mode full\|stale]`、`atlas submit --entry E [--work]`、`atlas stale [--entry]` | |
| | `specs check --repo 别名 <路径> [--ledger 账本.json]` | spec 断言保真检查，违规退出 1；基线是 git HEAD 中的同一 spec 连同它的本地导入（`./fixtures.mjs`、`./library.mjs` 及其导入）与任务资产 |
| | `fixtures run <配方> [--param 名=值]... [--run N] [--scenario]`、`fixtures exec --reason … [--run] -- <命令>` | |
| | `identity status`、`identity record --identity I --kind login\|code [--run N]` | |
| | `attention list`、`attention add --kind K --text … [--scenario]... [--run]`、`attention resolve <编号> [--note]` | |
| | `report [--file 文件] [--zip] [--out 目录]` | 见 §10 |
| | `playwright guide\|convert` | Web 指引、Playwright JSON 报告转 results.jsonl |

## 5. 运行

### 准备

`run prepare` 的执行者取 `--session`，其次 `SOP_BOARD_SESSION`（启动器注入），再次 `CODEX_THREAD_ID` 或 `CLAUDE_CODE_SESSION_ID`；都取不到返回 `session-required`。范围与原因：

| `--scope` | 包含 | 原因 |
| --- | --- | --- |
| `pending`（缺省） | 未执行、待复测、失败、受阻、跳过的非草稿场景 | 未执行 `new`；守护未通过导致的待复测 `guard-failed`，修复关闭导致的为 `rerun`；失败/受阻/跳过 `failed` |
| `all` | 全部非草稿场景 | `rerun` |
| 键或编号 | 指定场景；含草稿则 `draft-not-runnable` | 同 pending 的映射，其余 `rerun` |
| `--fix N` | 修复的问题场景 | `rerun` |

同一检出可以有多个 prepared 的运行，按身份分开（已定决策 28）：`--identity`（可重复）只为这些身份取得浏览器租约，不写时取本轮 Web 入口的全部身份；身份租约整机独占。`--scope pending` 展开时跳过本检出其他 prepared 运行里的场景；明确指定的场景已在别的 prepared 运行里时返回 `scenario-in-run`，写明是哪个运行。

准备时经守护进程拉起本检出环境：快照与环境启动时不同则重启，这套环境上还有别的 prepared 运行时等它们结束。`port_step: 0` 时环境由当前检出的 prepared 运行占住；没有 prepared 运行就是空闲，守护进程停掉它、为本检出拉起，输出 `switched_from`；被别的检出占着时返回 `env-busy`。有别的检出在等环境时，占用环境的检出不能开始新的运行，同样返回 `env-busy`。身份被别的运行或真人占着时返回 `browser-busy`；这套环境上有服务被别的运行停着时返回 `env-busy`。

**排队**：带 `--wait <秒>` 时，上面这些情况在本机排队，最多等这么久，轮到了照常准备，超时返回原来的错误码；不带时立即返回。两种情况的错误输出都带 `holder`（占用的检出、运行与身份）与 `queue`（排第几，前面每个等待者的检出、运行、会话与已等秒数）。队列只在守护进程内存里，按到达先后；等待者按会话编号识别，请求结束后 60 秒内同一会话再来，保留原来的位置；看板重启后清空。

然后为本运行的身份取得浏览器租约，再创建运行目录，缺省 `<数据目录>/runs/<编号>/`；已存在时使用 `<编号>-2`、`<编号>-3` 等未占用目录，原目录和文件原样保留。实际目录写入 `local_work.dir`，以 prepare 返回的 `directory` 为准，提交、放弃及报告通过记录定位，不按编号推算。准备失败时只清理本次确实新建的目录。修复、沉淀、合并、SOP 编写、草稿、交互地图、启动记录和 retest 输出也使用未占用目录。环境只要 adapter 输出就绪就可以准备，不要求每个服务都在运行；输出另有 `services`、`not_running` 与 `missing_address_env`。

| 文件 | 内容 |
| --- | --- |
| `prompt.md` | 执行提示词（随包 `run.md`，修复复验附“逐条实际执行问题场景”） |
| `brief.md` | 速查卡：范围计数、入口证据与录制开关、缺失的入口地址、各服务的状态与业务说明及按服务判定未执行与受阻的规则、浏览器问题、前置数据规则、时间上限与等人处理的写法 |
| `services.json` | 准备时的环境状态、各服务（名称、业务说明、可停、状态，异常退出附日志与末尾）、`not_running`、`missing_address_env` |
| `scenarios.yaml`、`pending.json` | 本轮场景（带 key、reason）与范围 |
| `entries.json`、`repos.json` | 用到的入口与地址；检出仓库路径 |
| `snapshot.json`、`product-files.json` | 准备时的快照与逐文件清单 |
| `run.env` | 入口地址变量、`SOP_BOARD_RUN`、`SOP_BOARD_CHECKOUT`；有 Web 场景时另有 `PW_CDP_ENDPOINT`（default 身份或第一个身份）、`PW_IDENTITIES`（只列本运行持有的身份）、`PW_SCOPE`、`PW_SCENARIO_FILE`、`PW_OUT`、`PLAYWRIGHT_BROWSERS_PATH`、`PW_NODE_ROOT` |
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
| `executor`、`session` | `playwright convert` 写入的执行者信息，只作留痕；登记以运行准备时的执行者为准 |

`not-executed` 行写 finding、不附证据，不登记结果，场景状态不变，留在待执行。独立复验（`--fix`）不接受 `not-executed`。表外字段不合格。

未执行与受阻按性质划分，不看发生在开始前还是执行中（提示词 run.md 与标准第 1 节）：用到的服务本机未搭建或被人停着、缺少入口声明的凭据变量、等人处理的事（真实或未声明账号登录、人机验证、只发到本人设备的验证码、身份使用次数到上限）写 `not-executed`；服务或浏览器异常退出、前置数据准备失败、证据丢失、`browser may not be secure`、入口或观察能力还没建设、需要停外部共享中间件的故障场景写 `blocked`。

### 提交规则

`run submit --run N`（可在任意目录运行，检出取运行登记的检出）：

1. 运行须为 prepared，否则 `run-not-prepared`。
2. **作废**：准备后场景被删除或指纹变化的，只作废这些场景（`voided`），不登记。
3. **浏览器崩溃**：租约期间该运行持有的身份浏览器异常退出时，用到该身份的 Web 场景若没有 pass 行，记为 `blocked`，finding 为“浏览器进程 <时间> 因 <信号或退出码> 退出，日志 <路径>[，崩溃报告 <路径>]”；取不到退出信号时写“浏览器进程 <时间> 已退出（取不到退出信号），日志 <路径>”。
4. 其余场景逐行校验（缺行即失败）。
5. **product_changed**：比较准备时与提交时的逐文件清单，执行期间变化的路径记在每条结果上，作为版本记录，不影响状态与独立性。
6. 登记结果、证据入库；某场景最近一次守护运行通过而本次失败时记漏检（§7）；释放浏览器租约；修复复验时更新修复状态（§8）。
7. **服务**：本轮每条停止事件记下服务停着期间执行的场景（`executed_at` 落在停止与下一次启动或恢复之间，按秒比较），结果详情据此显示“执行时 <服务>（<业务说明>）已停止”。结果登记后恢复还停着的服务（守护进程未运行时只撤回停止请求），每个记一条 `restore`；声明了服务时另列出提交时异常退出的服务，`during_run` 表示准备时它还没有异常退出。

输出 `recorded`、`voided`、`not_executed`（`scenario`、`finding`、`fingerprint`）、`product_changed`、`browser_crashes`、`independence`、`submitted_by`；声明了服务或本轮停过服务时另有 `restored`、`crashed_services`、`service_events`（服务、`stop|start|restore`、`ok`、操作者、时间、说明、停着期间执行的场景），恢复出错时有 `services_problem`；运行期间看板重启过时另有 `interruptions`（每条“环境随看板重启中断”的起止时间，已定决策 28）。这些也写进运行摘要（`work.result`）。校验不合格时不登记任何结果，运行保持 prepared，也不恢复服务，修正后重交。

### 独立性

实现者依次取：修复复验时的修复会话；`run submit --implementer <会话编号>` 或 `--implemented-by <操作者>`（二选一，同时给是参数错误）；`SOP_BOARD_IMPLEMENTER` / `SOP_BOARD_IMPLEMENTED_BY`（`run launch` 的同名参数注入）。没有实现者记 `undeclared`（独立性未声明）；执行者等于实现者会话记 `self-check`（实现者自检）；否则 `independent`（独立验收）。`--implemented-by` 用于跨机器验收（执行会话拿不到实现者的会话编号）：与本机操作者不同记 `independent`，相同则以参数错误（退出码 2）拒绝，要求改填会话编号；历史里写“实现者 <操作者>”。实现者由使用者在任务里说明，没说明的照旧记未声明。只有 `independent` 的真实入口通过能作为沉淀与守护的依据。

提交分三步：证据文件存进本机证据目录；一个事务把运行记录、结果、证据索引写进业务记录库（运行以“机器标识 + 本机库标识 + 本机运行编号”唯一）；更新本机运行状态、释放浏览器租约、恢复停着的服务。第二步失败（如 `database-unreachable`）时运行保持 prepared，结果与证据留在运行目录，恢复后重新 `run submit`，只登记一次；第三步失败后重新提交只补做第三步（输出 `resubmitted: true`）。

### launch / wait

`run launch` 与工作台“AI 重跑”“沉淀”按钮都由守护进程启动无头会话：

- 同一检出同时只有一个 running 的启动（`launch-running`）。类型 `run` 或 `distill`，须有具体场景。
- 工作目录是该检出的场景仓库；提示词 `<数据目录>/launches/<编号>/output.prompt.md`（目录被占用时顺延后缀）（随包 `launch.md`：先运行 `sop-board run prepare --scope …` 或 `distill prepare …`，按 prompt.md 执行并提交，需要人时 `attention add` 后结束），日志为同目录 `output.jsonl`（实际位置取启动记录的 `log`）。
- 注入 `SOP_BOARD_LAUNCH`、`SOP_BOARD_CHECKOUT`、`SOP_BOARD_IMPLEMENTER?`。Claude：启动器生成 UUID 作为 `--session-id` 并注入 `SOP_BOARD_SESSION`，缺省参数 `--permission-mode acceptEdits --allowedTools Bash Read Write Edit Glob Grep`。Codex：`codex exec --json -s workspace-write -c sandbox_workspace_write.network_access=true --skip-git-repo-check -C <目录>`，会话编号取日志中的 `thread.started`。两者都 `--add-dir` 项目数据目录与数据根目录下的 `evidence` 目录，路径先解析为真实路径；浏览器运行时只读使用。
- 状态 `running` → `exited`（退出码 0）/`failed`/`stopped`。启动记录与其会话编号就是独立执行的证据；运行通过 `SOP_BOARD_LAUNCH` 关联启动。
- 会话输出由随会话存活的转写进程遮蔽后写进日志，不经守护进程；遮蔽范围不变（已保存的共享库密码）。看板 `restart` 时会话继续运行，新守护进程按进程号与命令行认回，用零信号检查存活；取不到退出码时，产出了运行或沉淀记 `exited`，否则按 `no-work` 处理（已定决策 28）。`stop` 与 SIGTERM 照旧结束会话。
- 会话准备运行时按提示词带 `--wait`，环境或身份被占着时排队（§5）。
- `run launch` 在启动前与 `run prepare` 共用范围检查；范围不合格不产生启动记录。指定文件不合格时返回 `scenario-file-invalid`，附文件、字段及行号。`pending`/`all` 只从合格文件选场景，不因其他文件不合格拒绝；prepare 输出及 launch 输出的 `launch` 对象附 `invalid_files`（文件、字段、行号），brief.md 的“未纳入的不合格文件”要求执行会话转述。选出范围为空仍返回 `nothing-to-run`，problems 同时列出不合格文件。交给客户端的目录解析为真实路径；浏览器运行时只读，不授予写权限。
- `run wait` 每 2 秒轮询，结束时输出启动状态及其运行结果。会话正常退出（`exited`）却没有产生运行或沉淀时，启动公开状态为 `no-work`（未产生运行），本机输出附 `log_tail`；wait 返回 `launch-no-work`、退出码 1。失败的启动保持 `failed`，本机同样附 `log_tail`，wait 返回 `launch-failed`、退出码 1。`exited` 关联工作中存在 `prepared` 或 `abandoned` 时，输出 `unsubmitted` 列出工作编号、类型、状态，problem 写明未提交或已放弃，本机附日志末尾；wait 返回 `launch-unsubmitted`、退出码 1。`failed` 有这些工作时同样列明，错误码仍为 `launch-failed`。不自动提交或放弃工作；其余已结束情况返回成功。

## 6. 浏览器代理

- **运行时**：`browser install` 把随包 `package.json`/`package-lock.json`（固定 `@playwright/test` 1.64.1）复制到 `~/.sop-board/runtime/`，`npm ci` 后以 `PLAYWRIGHT_BROWSERS_PATH=<runtime>/browsers` 执行 `playwright install --no-shell chromium ffmpeg`，把 Playwright 版本、CfT 修订号与版本、可执行文件路径写入 `runtime.json`。需要 Node ≥ 20 与 npm。不读写共享的 ms-playwright 缓存，不安装 WebKit/Firefox/headless shell。
- **启动**：只有守护进程启动浏览器，执行者只经 CDP 连接，不自己启动或安装。每个身份一个 CfT，各检出共用（已定决策 28）：`--user-data-dir=<数据目录>/browsers/<身份>/profile --remote-debugging-port=<端口> --no-first-run --no-default-browser-check --use-mock-keychain`，缺省加 `--headless=new`，不加 `--enable-automation`；输出写同目录 `chrome.log`。`/json/version` 15 秒内有应答才算就绪。
- **端口**：缺省端口 `9400 + 身份序号`（已定决策 28 起不含槽位），不分项目和数据根目录。序号：`default` 为 0，其后按各 Web 入口声明顺序；超过 20 个身份报错。缺省端口被别的进程占用（127.0.0.1 上有进程监听或绑定不上）时顺延到下一个空闲端口，不报错，不结束、不连接占用者；到 65535 都找不到才报错。实际端口记在 `browser_instances.port`，`browser status` 显示它（另给 `default_port`），`run.env` 的 `PW_CDP_ENDPOINT` 与 `PW_IDENTITIES` 里各身份的 `endpoint` 指向它；守护命令声明 `browser:` 时同样。就绪须确认 `/json/version` 应答的是本次启动的浏览器（`chrome.log` 里本次的监听行为 `127.0.0.1:<端口>` 且浏览器标识一致；没有监听行时用 lsof 核对监听进程）：探测之后端口才被别人占去时 CfT 会改在 `[::1]` 上监听，这时结束这次启动、从下一个端口再找。浏览器运行期间端口不变；下一次启动重新从缺省端口找起。（已定决策 24）
- **看板停止与重启**：正常停止保留有界面浏览器的进程、窗口与页面，输出检出、身份、进程号和调试端口；无头浏览器与环境照旧停止。启动时逐个读取 profile 的 `SingletonLock`，核实本机进程仍在、命令行的可执行文件是运行时登记的 CfT、`--user-data-dir` 完整匹配这个 profile，从命令行取模式与调试端口。有界面窗口在后台最多用 15 秒反复核对，每次核对后等待最多 0.2 秒再试；该身份的租约和打开请求等待核对结束，期间不另起浏览器。端口监听者正是该进程且 `/json/version` 有应答时认回；lsof 无输出或不可用时，改用 chrome.log 最后一条 DevTools 监听行的端口和浏览器标识核对调试应答。lsof 明确返回其他监听者时不请求该端口。核对始终不通过时保留进程，状态为未认回（`unrecovered`），status 带进程号与 `reason`，不提供 endpoint。租约、`run prepare` 与 `browser open` 返回 `browser-unrecovered`，包含原因及恢复应答后重试、或用 `browser close --identity <身份>` 结束窗口这两条出路。下一次请求会重新核对，通过即沿用原进程与端口；`browser close` 可以结束未认回的窗口，看板停止仍保留它。认回后沿用原端口并释放旧租约，`started_at` 为进程实际启动时间。核实属于该 profile 的无头浏览器结束并清理；锁指向的进程已消失时忽略，不登记新崩溃。经 `restart` 交接时不同：无头浏览器也认回，租约不释放（已定决策 28）。数据库重建时按 profile 所在的身份登记实例。认回的进程每秒用零信号检查存活，每五秒至多一次按启动时间和命令行核对身份，发信号前强制重核，无法取得退出信号时写“已退出（取不到退出信号）”，崩溃处理与普通实例相同；`browser close` 同样结束认回的进程。运行时登记缺失、锁或进程身份不符时不操作该进程。（已定决策 26）
- **租约**（`browser_instances.holder`）：持有者 `run:<编号>`、`retest:<检出>`、`distill:<检出>` 或 `human`。身份租约整机独占，同一身份同一时刻只有一个持有者（已定决策 28）。别的持有者占用时返回 `browser-busy`（`run prepare` 带 `--wait` 时排队，§5）。交给与上一次不同检出的持有者之前，经 CDP 的 `Network.clearBrowserCache` 清空 HTTP 缓存，cookie、本地存储与登录保留；`browser_instances` 记下最近一次交给的检出（`browser status` 的 `last_checkout`）。状态 crashed 时返回 `browser-crashed`；启动即退出不抛错，问题写进 brief.md，相关场景应记受阻。运行提交或放弃时释放。
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
4. 每个场景写一条 `guard_runs`（pass/fail + 原因 + 快照）。通过不改变状态；未通过的场景转为待复测并显示原因，之后经真实入口通过，或守护重新运行通过，就回到通过。retest 不判断失败原因：未通过的原因在失败的测试与命令之后，写明运行开始时的情况：声明了服务时是“当时本机没在运行的服务：<名称>（<业务说明>，<状态>）”或“当时声明的服务都在运行”（环境没在运行时写明），以及“工作区有未提交改动（<仓库> N 处）”或“工作区没有未提交改动”（各仓库 `git status`，含未跟踪文件）。有服务没在运行时照常运行全部有效守护。

### 漏检

某场景在同一指纹上最近一次守护运行通过，之后真实入口结果或人工复测为失败（且其间没有更晚的通过）时，按 场景 + 指纹 + 测试摘要 记一条 `guard_misses`。该守护随即失效。重新沉淀时，`acceptance` 须是漏检之后的新独立通过，且测试摘要须与漏检时不同。

### 沉淀

- `distill prepare [键…]`：缺省为全部待沉淀场景；指名状态不是通过的场景时返回 `distill-not-passing`，写明当前状态；其余指定的场景须为待沉淀（`not-distillable`）。同一检出只能有一个进行中的沉淀（`distill-prepared`）。工作目录 `<数据目录>/distills/<编号>/` 含 `prompt.md`、`scenarios.yaml`、`acceptance.json`（独立通过的结果编号、执行者、运行目录）、`repos.json`。会话按随包 regression-distill 技能在当前检出写测试（不提交），写出 `guard.json`。
- `distill submit`：给出 `--work` 时须是进行中的沉淀（`work-state-invalid`）；缺省读进行中沉淀目录里的 `guard.json`（YAML 或 JSON，结构同 regression.yaml 的 `commands` 与 `guards`；`detection.evidence` 写证据文件路径，相对 guard.json 目录）。守护进程先核对当前状态为通过，否则返回 `distill-not-passing` 并写明当前状态；运行守护后、登记前再次核对状态。逐条核对：acceptance 为同指纹独立通过、测试文件存在并重新计算 sha256、逐条覆盖、detection 完整、漏检后的条件；同名命令与已有定义不同则 `report-invalid`；`layer: browser` 的测试另做 spec 保真检查（基线为 `git show HEAD:<路径>`；HEAD 还没有该文件时，以产生独立通过的那次运行目录中覆盖该场景的 spec 为基线：按 playwright/report.json（缺失时 report.partial.json）中测试标题首词或场景标签与文件的对应关系查找；本机目录存在却找到零份或多份时拒绝并列出目录与文件；首次比较以各自 spec 所在目录作为辅助文件的逻辑路径根，排除 spec 直接 ./fixtures.mjs 导入的运行适配接口及其依赖，不按全局文件名排除项目代码；spec 与 library.mjs 及其本地导入仍完整比较，HEAD 基线比较保持原有完整依赖规则。替换夹具自身的正确性由实际运行与检出能力核对。基线不合规则时指出基线及原因，要求按规则写 spec 重新验收后再沉淀；本机没有那份运行目录（记录缺失或目录已不在，不论产出机器是谁）时只做静态检查，fidelity 写 no_baseline=true 及 no_baseline_reason=“运行目录不在本机”）。不合格返回 `distill-invalid`；成功输出的 `fidelity` 按场景列出脚本、基线及 same/new/review/violation/no_baseline，供核对实际比较依据。全部合格后按 retest 规则实际运行一次，未通过返回 `distill-tests-failed`；通过后合并写入 regression.yaml、记录守护运行，测试与索引留作未提交改动供开发者审核。
- 工作台“沉淀”按钮仅本机可用，作用是启动一个无头沉淀会话。

## 8. 修复

| 步骤 | 命令 | 规则 |
| --- | --- | --- |
| 认领 | `fix prepare [键…] [--session]` | 缺省为当前检出全部失败或受阻场景；指定的须为失败/受阻（`fix-scenario-invalid`）。同一检出只能有一个 open 的修复（`fix-open`）；场景已在已提交/已复验修复中时 `fix-already-owned`。目录 `<数据目录>/fixes/<编号>/`：`prompt.md`、`problems.json`（最近 5 条结果；证据写名称、sha256 与本机路径，本机没有的写明在哪台机器上）、`scenarios.yaml`、`repos.json`、`scenario-files.json`；记录准备时的产品文件清单 |
| 提交 | `fix submit [--fix N]` | 读 `fix.json`：`{"root_cause": 非空, "product_problem": bool}`。场景文件被改动则 `scenario-file-protected`（修复只改产品代码）。判定为产品问题却没有产品改动则 `fix-no-changes`。`product_problem: false` 时修复记为 abandoned 并登记待人处理；否则为 submitted，记下改动文件 |
| 独立复验 | 另一会话 `run prepare --fix N` | 修复须为 submitted/verified，且在同一检出。实现者自动为修复会话；不接受 not-executed。全部问题场景都实际通过、且执行者不是修复会话时，修复进入 verified，并记下复验时各改动文件的内容摘要 |
| 关闭 | `fix close [--fix N]` | 须为 verified（`fix-validation-required`）；改动文件须已提交且工作区与 HEAD 一致（`fix-commit-required`）；提交内容须与复验时一致（`fix-content-changed`）。关闭后之前失败/受阻（及人工不通过）的问题场景显示为待复测 |
| 放弃 | `fix abandon --fix N` | open/submitted/verified 可放弃 |

修复状态：`open` → `submitted` → `verified` → `closed`，或 `abandoned`。

修复记录（问题场景、修复会话与操作者、状态、关闭时间、产出机器与检出）在业务记录库，各看板与 `status --json` 的 `fixes` 都看得到进行中的修复；修复关闭是结论（取统一序号，按修复所在检出的分支归属），使问题场景转待复测的规则对所有看板生效。修复目录、文件基线与复验摘要只在产出机器上：`fix submit`、`run prepare --fix`、`fix close`、`fix abandon` 在其他机器返回 `fix-not-local` 并写明产出机器与检出。别人要验证时，等修复提交推送后用普通的 `run prepare --scope` 执行：结果是共享结论，但不关闭那条修复。

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
- **合并**：`merge prepare [--inbox N]` 取待合并用例（缺省全部），当前场景文件须先合格；目录 `<数据目录>/merges/<编号>/` 含 `inbox/` 原文、`answers.json`、`before.json`、`prompt.md`。会话直接编辑场景与用例文件，有疑问在对话里问或用 `ask`。`merge submit` 校验全部场景文件，输出场景差异（新增、移除、内容改动，以及 actor/verify/polarity 变化），把收件箱条目标为已合并。差异的 `references_only` 单列“只加了引用”的场景、用例编号及处理提示（本次用例编号新增进 refs、四要素未变），不计入 changed，也不阻止提交。
- **问答**：`ask --about inbox:N|<键>|<文件名> --text …` 挂到看板“待答复”；测试同学在内网答复（须填姓名）；会话用 `answers [--about] [--unanswered]` 读取。确认人取操作者或答复人，记在场景 `refs` 与 `metadata.sources`。

### 业务 SOP

- `sop prepare --source <资料>...`（最多 32 份、每份 ≤20 MiB）把资料复制到 `<数据目录>/authoring/<编号>/sources/S<n>/`，写 `sources.json` 与 `prompt.md`。
- 会话先逐份核对场景目录中已有的 SOP，找出业务交叉（见[标准](standard.md)第 5 节）；新规则改变已有规则口径的，经使用者确认后同时改正已有 SOP（删去其文末确认记录后重新 `sop submit`）。然后在场景目录写 SOP Markdown，二级标题须有：业务目标与范围、角色、业务规则（其下 `###`/`####` 的 `R<n>` 规则小节）、流程图、禁止的副作用、未覆盖范围。
- `sop submit --file <场景目录下的 .md>` 校验后在文末追加 `## 确认记录`（确认人、时间、各资料摘要）；正文不能自带该节。
- 一次 `sop prepare` 后，新 SOP 与随之改正的已有 SOP 分别 `sop submit`，每份各自校验、追加确认记录，资料清单都取这次工作的 `sources.json`。首次提交把工作标为 submitted，后续仍可继续提交；本机与业务记录库的同一条工作结果累计列出各文件的文件名、确认人和时间。
- 不带 `--work` 时取本检出最近一次 SOP 编写工作；带 `--work <编号>` 时使用指定工作。prepared 和 submitted 都可提交，abandoned 返回 `work-state-invalid`，从未准备过则返回 `work-not-found`。有新资料时重新 `sop prepare`，之后不带编号的提交归入新工作。

### 草稿

带 `metadata.status: draft` 的场景文件就是草稿，不进入执行与复测。会话由 SOP 写草稿或合并用例时核对已有场景：业务交叉改变已有场景预期的，直接修改这些场景（指纹变化后回到未执行），不另写重复场景。预期只来自 SOP 原文与使用者的答复；场景的 `refs` 写 `<SOP 文件名>#R<n>`。

- **审阅**只在工作台“草稿”筛选中进行（没有单独的草稿页）。草稿场景详情依次为：标题上方的“文件 › 节点”路径（点哪一级打开哪一级的面板）；四要素；`refs` 指向的 SOP 规则原文（可展开，按场景目录中 SOP 的 `R<n>` 标题取整节）；固定的五项检查清单（场景是否忠实于 SOP 规则；预期能否直接观察并带具体值；禁止副作用是否写全；起点数据是否具体；是否漏了反向、边界或与已有业务交叉的场景）；批注表单；「同意」；审阅记录（同意、撤销、旧内容上的同意与批注）。草稿不显示复测表单与本机按钮。
- **批注**：目标为整个文件或某个场景，≤4000 字，须填姓名（本机不填时为操作者），存数据库，不写场景文件。会话用 `draft annotations` 读取，修订后 `draft resolve <编号> [--reject]`。
- **同意**：按“文件 + 场景 + 指纹”记在数据库（`draft_approvals`），不写场景文件，不区分检出：任何检出中内容相同的场景都显示已同意。内网须署名（顶栏姓名），本机不填时以操作者署名；同一人在同一指纹上重复同意不重复登记。场景有未处理的批注时「同意」不可点，接口返回 `draft-annotations-open`；请求带的指纹与当前内容不符时返回 `draft-changed`。只能撤销自己的同意（`approval-not-own`），撤销只记时间，记录保留。同意后页面自动打开同一文件中下一条待审场景，没有时停留在当前场景并提示。
- **左栏**：草稿场景的状态点按审阅状态显示（空心为待审）；草稿文件行与节点行显示“已同意 x/y”和常显的「审阅」按钮（点它不展开或收起分组）。“草稿”筛选的计数是全部草稿场景。
- **节点面板**：完整列出组内每条场景的四要素、审阅状态与同意记录（自己的同意可逐条撤销），“同意本组 N 条待审场景”逐条登记组内待审场景，有未处理批注或内容已变化的跳过并列出原因。
- **文件面板**：需求说明与来源、审阅进度（待审、有批注、已同意的条数）与各节点进度、文件级批注列表与表单；全部已同意且没有未处理的批注时显示“可以确认”，本机页面附 `sop-board draft confirm <文件>`，内网不显示命令。不提供整份文件一键同意。
- **`draft annotations [文件]`** 输出批注（`--all` 含已处理）与 `drafts`：每份草稿的审阅进度、每条场景的审阅状态与同意人（如“已同意（张测）”）、未处理的批注、能否确认。
- **`draft confirm <文件>`** 要求每条场景在当前指纹上已同意、文件没有未处理的批注（含文件级），否则以业务失败退出：有未同意的场景时 `draft-not-approved`，只剩未处理批注时 `draft-annotations-open`，`problems` 逐条列出未同意的场景与未处理的批注。metadata 中须有单独一行 `status: draft` 与块格式 `sources:`；把状态改为 `confirmed` 并在 sources 首项插入 `{ref: "sop-board draft confirm", version: <日期>, confirmed_by: <同意人>}`，同意人去重、按首次同意的先后、用“、”连接。

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

只登记仓库后即可启动看板。配置不完整时，本机工作台显示“项目配置还没完成”、逐项问题与设置页链接；内网只显示问题类别，不带字段细节或设置入口。`/api/board` 的 configured 与 problems 对应此提示，problems 在内网按类别返回。

`status --json` 在配置不完整或共享库连接报错时也返回 daemon（运行状态、已启动看板的回环地址与启动命令），供会话给出本机设置页链接。

`report` 导出当前检出的 `report.md`（缺省目录 `<数据目录>/reports/<检出>-<时间>/`，不写进产品仓库）：快照与各仓提交、各状态计数、Agent 通过率与一次通过率、人工复测覆盖与通过率、Agent 独立通过的人工确认率与复核覆盖、独立性计数；每个场景的状态与原因、当前指纹上最近一次未执行（操作者、时间、运行、原因；不改变状态）、最近一次守护运行（注明不是真实入口执行）、最近 5 条结果（取证时的快照、独立性、执行时停着的服务、finding、证据 sha256）和人工复测；然后是误判明细、待沉淀与待人工复核；最后列出未执行范围（未执行、待复测、草稿），每条附最近一次未执行。`--zip` 另写 `evidence.zip`（含报告与所引证据原文）。正文按凭据取值脱敏。

统计口径（全部取当前检出的结果记录，不限当前场景文件）：

| 指标 | 分子 / 分母 |
| --- | --- |
| Agent 通过率 | 各指纹最新 Agent 结果中的通过 / 其中通过与失败 |
| 一次通过率 | 各指纹首条 Agent 结果中的通过 / 其中通过与失败 |
| 人工复测覆盖 | 有 Agent 结果且有人工标注的指纹 / 有 Agent 结果的指纹 |
| 人工复测通过率 | 各指纹最新人工标注中的通过 / 全部最新人工标注 |
| Agent 独立通过的人工确认率 | 样本中人工结论为通过的 / 有人工结论的样本 |
| 复核覆盖 | 有人工结论的样本 / 全部样本 |

样本是每一条经真实入口的 Agent 独立通过（§1）。人工标注登记时，`of_result` 记当时该场景当前指纹最新一条 Agent 结果（不论其间是否已有别的人工标注），`product_digest` 记标注时的产品快照（版本记录）。样本的人工结论取 `of_result` 指向它的最近一条人工“通过”或“不通过”，不比较快照；只有阻塞或需求变更的样本不计入分子分母，按最近一条的结论计入“未决”，报告总是写出“未决：阻塞 N，需求变更 M”。分母为 0 的比率在报告与概况中显示为“—”。`report --file` 只筛选场景各节、待沉淀、待人工复核与未执行范围，概况中的统计与误判明细仍按整个检出。人工结论为不通过的样本列为误判明细：场景键、运行编号、Agent 执行时间、复测时间、复测人、缺陷链接、说明；执行者只在本机报告中显示。

| 路由 | 级别 | 内容 |
| --- | --- | --- |
| `GET /` | lan | 跳转到 `/p/<项目>` |
| `GET /p/<项目>/settings` | local | 七节设置、每节问题数与字段旁的问题、凭据声明来源及是否已设置、共享库测试与产品选择；逐节保存，无需重启。顶栏“设置”只在本机显示，内网直访返回 403 local-only，不带配置内容。接口见 §13 |
| `GET /p/<项目>?checkout=<名称>#<场景键>` | lan | 场景工作台：左侧检出切换、搜索、状态筛选（含待沉淀、待人工复核与计数；待复测、失败、待人工复核、未执行、通过在计数为 0 时也显示）与 文件→节点→场景 树；不带 `#场景键` 时右栏是概况，显示各状态与徽标计数、Agent 独立通过的人工确认率与复核覆盖；右侧标题与徽标、状态与原因、四要素（附守护覆盖）、最新结果与证据（截图、视频、trace、文本）、复测标注、本机按钮、历史。草稿场景的详情与 `#review:<文件>`（文件面板）、`#review:<文件>#<节点>`（节点面板）见 §9“草稿” |
| （左栏，工作台与业务 SOP 页共用） | | 「全部展开 / 全部收起」切换与手动展开收起一样按项目与页面记在浏览器 localStorage；「隐藏左栏」后右栏占满，左侧窄栏里的「显示左栏」恢复，隐藏状态两页共用。左栏右边缘的手柄拖拽调宽，范围 240px 到窗口宽度的 60%，双击恢复 360px；宽度记在 localStorage（`sop-board-sidebar-width`），两页共用、不分项目，刷新、轮询、隐藏再显示后保持，窗口变窄时按新边界显示。文件、节点与场景标题悬停显示全文。这些都只改本浏览器，不向看板写入 |
| `GET /p/<项目>/runs` | lan | 启动记录与运行记录 |
| `GET /p/<项目>/report[?file=&download=1]` | lan | 报告预览与 Markdown 下载 |
| `GET /p/<项目>/attention` | lan | 待人处理 |
| `GET /p/<项目>/inbox` | lan | 上传用例/SOP、待答复问题 |
| `GET /p/<项目>/sop` | lan | 场景目录下的 SOP 原文渲染 |
| `GET /p/<项目>/atlas` | lan | 交互地图与过期页面 |
| `GET /api/board`、`/api/scenario?key=`、`/api/runs`、`/api/annotations` | lan | 工作台数据；`/api/board` 的每个场景带 `distill`、`review` 徽标，`counts` 含 `distill`、`review`，`stats` 含 `agent_confirm_rate`、`agent_review_coverage`（numerator、denominator、value）；只在本机响应中另有 `environment`（环境状态与各服务的名称、业务说明、状态、可停；`holder` 为环境当前属于的检出及在用的运行与身份，`waiting` 为排队的等待者：检出、运行、会话与已等秒数，已定决策 28），概况页据此显示“本检出的服务”和环境的占用与排队。`/api/scenario` 另有 `not_executed`（当前指纹上最近一次未执行：`run`、`by`、`at`、`finding`，没有为 null），历史中的 Agent 结果带 `stopped_services`（`[{name, about, text: "执行时 stock（库存查询与扣减）已停止"}]`），内网同样返回，变量名照常遮蔽。`/api/runs` 的运行只在本机带 `service_events` |
| `GET /evidence/<sha256>/<文件名>` | lan | 证据下载；视频支持 Range；trace zip 允许 trace.playwright.dev 跨域读取 |
| `POST /api/mark` | lan | 复测标注：`verdict` 取 pass/fail/blocked/changed，`defect`、`note`、`by` 都可不填（本机缺省为操作者，内网不填记为未署名） |
| `POST /api/inbox`、`/api/answer`、`/api/annotate` | lan | 上传、答复、批注；须有姓名（顶栏署名填一次，浏览器 localStorage 记住，各页面共用；批注在本机不填时为操作者） |
| `GET /api/draft?file=<草稿>` | lan | 文件面板与节点面板的数据：需求说明、来源、审阅进度、各节点场景的四要素与审阅状态、文件级批注、能否确认；确认命令只在本机响应中 |
| `POST /api/approve` | lan | 同意草稿场景：`file` 加 `scenario`（单条，可带 `fingerprint`）或 `node`（本组全部待审，可带 `fingerprints`）；`by` 内网必填（`signer-required`），本机缺省为操作者 |
| `POST /api/unapprove` | lan | 撤销自己的同意：`approval`（同意编号）或 `file` 加 `scenario`；`by` 须与同意人相同 |
| `GET /p/<项目>/sop`、`GET /api/sop?checkout=<名称>` | lan | 业务 SOP：场景目录下的 Markdown 按 1～3 级标题分节，左树右文；含“答复”小节的节标出已答复 / 待答复 |
| `POST /api/launch`、`/api/retest`、`/api/distill` | local | AI 重跑此场景、运行守护、沉淀 |
| `GET /api/launches/<编号>/log` | local | 启动日志末尾 512 KiB |
| `/local/health`（restart 交接后另带 `restart`：认回的浏览器与会话、重新拉起的环境）、`/local/stop`、`/local/restart`、`/local/env/{up,down,restart,status,service,restore}`、`/local/browser/{lease,release,crashes,open,close,status}`、`/local/retest`、`/local/distill/submit`、`/local/launch`、`/local/launch/stop` | token | CLI 调用 |

## 11. 错误码

| 错误码 | 含义 / 处理 |
| --- | --- |
| `project-not-found` | 找不到项目或当前仓库未登记；`init` 或写 `--project` |
| `project-format` | 旧格式数据目录或数据库（本机库格式不是 7）；用 `init` 新建项目，不迁移 |
| `product-unbound` | config.yaml 写了 `shared_database`，项目还没绑定产品；`init <项目>` 列出产品，问使用者后 `--join` 或 `--new` |
| `product-choice-required` | `init <项目>` 只列出产品（名称、创建人、有结果的场景数、最近写入时间、本机场景的重合条数），不自动绑定 |
| `product-not-found` | `--join` 的 ID 在共享库里不存在（输出列出已有产品），或绑定的产品已不在共享库 |
| `shared-database-required` / `shared-database-invalid` | 对没写 `shared_database` 的项目选择产品 / `shared_database` 不合格（例如端口不合格） |
| `database-authentication` | 共享库认证失败；设置接口与命令行都会返回，本机写明连接位置，内网只显示认证失败（HTTP 503） |
| `settings-invalid` | 设置不合格、破坏引用或引入其他节的问题；problems 标明字段（HTTP 422） |
| `settings-locked` | 配置正被使用；problems 给出可读说明，occupants 单列结构化占用者（HTTP 409） |
| `settings-stale` | config.yaml 已被别处修改；重新读取后再保存（HTTP 409） |
| `database-unreachable` | 连不上共享库：本机输出写明主机、端口、库名、用户及驱动原因的第一行（只遮蔽当前已保存的密码），内网只说明连不上；没有离线队列与本地缓存，恢复后重新运行（HTTP 503） |
| `database-version` | 共享库的表结构版本与本机 sop-board 不一致（新旧两个方向都拒绝）；换用匹配的 sop-board 版本，没有升级命令 |
| `fix-not-local` | 修复产出于别的机器（写明机器与检出）：`fix submit`、`run prepare --fix`、`fix close`、`fix abandon` 只在产出机器上执行 |
| `evidence-elsewhere` | 证据在别的机器上，产出机器的看板没开、没加 `--lan` 或地址已失效 |
| `invalid-arguments` | 参数组合不合格（`--join` 与 `--new` 同用、`--implementer` 与 `--implemented-by` 同用、`--implemented-by` 是本机操作者），退出码 2 |
| `database-error` | 数据库返回了未预期的错误，见 problems |
| `daemon-not-running` | 守护进程没运行或连不上；运行输出中的 `sop-board serve …`。`restart` 期间 CLI 先等新进程就绪（最多 120 秒），超时才返回 |
| `daemon-stop-timeout` | `stop` 或 `restart` 等了 600 秒，原来的守护进程仍未退出；查看 `serve.log` 或启动它的终端 |
| `restarting` | 看板已在重启，又收到一次 `restart` |
| `sandboxed` | 在 Codex 沙箱里运行 `restart`（有 `CODEX_SANDBOX`）；请使用者在终端运行 |
| `invalid` | 其他参数或配置问题（含配置不合格、检出不存在等），见 problems |
| `check-failed` | `check` 有不合格项：`problems` 逐项列出，`steps` 是三项的完整结果 |
| `env-busy` | 环境被别的检出的 prepared 运行占着、有别的检出在等环境、环境要重启而还有别的运行在用，或这套环境上有服务被别的运行停着（输出 `holder` 与 `queue`；`run prepare` 带 `--wait` 时先排队，超时才返回）；或端口被其他项目的环境租用、已被其他进程监听（附 lsof 信息） |
| `browser-busy` | 该身份浏览器被真人登录或其他运行占用（身份租约整机独占）；输出 `holder` 与 `queue`，`run prepare` 带 `--wait` 时先排队；真人占用时，真人完成后 `browser release` 交还 |
| `browser-unrecovered` | 有界面窗口未认回；输出进程号与原因。恢复应答后重试会重新核对，确实不要窗口时用 `browser close --identity <身份>` 结束 |
| `browser-crashed` | 30 分钟内两次异常退出；`browser open --reset-crash` |
| `browser-runtime-missing` | 未安装运行时；`browser install` |
| `session-required` | 取不到执行者会话编号；加 `--session` |
| `run-required` | `env stop\|start` 时本检出没有 prepared 的运行；运行之外恢复环境用 `env restart` |
| `run-ambiguous` | 本检出有多个 prepared 运行，命令没有用 `--run` 或 `SOP_BOARD_RUN` 指明是哪个；输出列出各运行 |
| `env-shared` | `env stop\|start` 时这套环境上还有别的 prepared 运行；输出列出它们，等它们提交或放弃后再停启 |
| `service-not-declared` | 项目没有声明 `adapter.services`，不能停启单个服务 |
| `service-unknown` | 服务名不在 `adapter.services` 中；输出列出已声明的服务 |
| `service-not-stoppable` | 服务没有声明 `stoppable: true` |
| `service-timeout` | adapter 没在 `service_timeout` 秒内把状态改成目标值；停止请求已撤回 |
| `service-unavailable` | 环境没有运行，或服务当前不是可停启的状态（本机未搭建、异常退出、未上报，或要启动的服务不是由 `env stop` 停下的） |
| `run-not-prepared` / `run-not-found` | 运行不是 prepared / 不存在 |
| `nothing-to-run` | 范围为空（`pending` 跳过别的 prepared 运行里的场景后为空也是） |
| `scenario-in-run` | 指定的场景已在本检出另一个 prepared 运行里；输出写明是哪个运行 |
| `scenario-not-found` | 场景键或编号不存在或不唯一 |
| `draft-not-runnable` | 草稿场景不能执行或复测 |
| `scope-invalid` | `--fix` 与 `--scope` 同用 |
| `launch-running` | 本检出已有运行中的启动；等待或 `launch stop` |
| `launch-not-found` | 启动不存在 |
| `distill-not-passing` | 场景当前状态不是通过，prepare 与 submit 拒绝并写明当前状态 |
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
| `launch-unsubmitted` | 启动正常退出但有关联工作未提交或已放弃，列出工作及本机日志末尾；run wait 退出码 1 |
| `launch-failed` | 启动失败；本机附日志末尾，run wait 退出码 1 |
| `launch-no-work` | 启动的会话退出但没有产生运行或沉淀；附启动日志末尾 |
| `scenario-file-invalid` | 场景文件不合格（带行号） |
| `cases-invalid` / `sop-invalid` / `upload-invalid` / `upload-too-large` | 上传或提交内容不合格 |
| `nothing-to-merge` / `inbox-invalid` / `inbox-state-invalid` | 合并范围不对 |
| `work-not-found` / `work-state-invalid` | 没有进行中的 merge/author/atlas 工作 |
| `draft-not-found` / `draft-not-approved` / `draft-annotations-open` / `draft-format` | 草稿确认条件不满足：不是草稿 / 有场景未在当前内容上同意 / 有未处理的批注（同意单条场景时也用它拒绝） / 文件头格式不符 |
| `draft-changed` / `draft-node-not-found` / `approval-invalid` | 同意请求带的指纹与当前内容不符（刷新后重新审阅）/ 节点不存在 / 请求缺场景或节点 |
| `signer-required` | 内网同意或撤销没有署名（先在顶栏填写姓名） |
| `approval-not-found` / `approval-not-own` / `approval-revoked` | 没有可撤销的同意 / 只能撤销自己的同意 / 这条同意已撤销 |
| `atlas-not-declared` / `atlas-invalid` | 入口未声明 atlas/spec_library / 地图不合格 |
| `fixtures-not-declared` / `recipe-not-found` / `adhoc-not-allowed` / `fixture-failed` | 前置数据问题 |
| `identity-not-found` / `identity-limit` | 身份未声明 / 计数达上限（已登记待人处理） |
| `local-only` / `token-required` / `cross-origin` / `json-required` | 访问级别或请求格式不符 |
| `internal-error` | 守护进程未预期错误（HTTP 500） |

### 数据库表

两组表（已定决策 21）。业务记录在业务记录库：配了 `shared_database` 时是共享的 PostgreSQL，否则在本机 `board.sqlite3`；两种库同一套表与 SQL（`?` 占位、`RETURNING id`、`ON CONFLICT DO NOTHING`），只有自增主键的写法、连接、事务与加锁按库分开（SQLite 用本机文件锁串行，PostgreSQL 靠事务、唯一约束与序号行锁）。本机状态始终在 `board.sqlite3`。本机库格式 7（`PRAGMA user_version=7`，`meta.format`），格式不符直接拒读；共享库在 `meta.format` 记表结构版本，不符时返回 `database-version`。

**业务记录**（每条带产品 `product`）：

| 表 | 内容 |
| --- | --- |
| `meta`、`counters` | 表结构版本；统一序号（`seq`） |
| `products` | 产品：ID、名称、创建人、创建机器 |
| `machines` | 机器标识、显示名、带 `--lan` 的看板登记的内网地址与对等令牌（别的看板凭它读取本机证据） |
| `work` | 运行记录的业务部分：kind run/fix/distill/merge/author/draft/atlas、机器标识 + 本机库标识 + 本机编号（唯一）、序号、检出名与分支、会话、操作者、实现者（会话或操作者）、独立性、修复、范围、快照、运行摘要（未执行清单及原因、提交人）、服务停启记录 |
| `results` | agent 与 manual 结果：序号、运行、产出机器、检出名与分支、文件、场景、指纹、快照、product_changed、状态、actual/finding/defect/note、reused_from、of_result、执行者、实现者、独立性、复测人；同一运行 + 文件 + 场景唯一 |
| `evidence`、`result_evidence` | 证据索引（sha256、大小、名称、产出机器）与结果引用；文件在产出机器的 `<数据根目录>/evidence/<前两位>/<sha256>` |
| `guard_runs`、`guard_misses` | 守护运行（序号、产出机器、检出名与分支、快照、测试摘要、pass/fail、原因、输出证据）与漏检（文件 + 场景 + 指纹 + 测试摘要唯一，各看板共用） |
| `distillations` | 沉淀登记：场景、指纹、测试摘要、沉淀人、机器、检出与分支、时间 |
| `fixes` | 修复的业务部分：问题场景（含指纹，最近 5 条结果，证据只记名称与 sha256，不带任何机器的本机路径）、修复会话与操作者、状态、fix.json、关闭序号与关闭人、产出机器与检出名；同一机器同一检出只能有一个 open |
| `inbox`、`questions`、`annotations`、`draft_approvals` | 上传、提问与答复（带提问机器）、草稿批注、草稿场景的同意（同一人在同一指纹上只有一条未撤销的同意） |
| `attention` | 待人处理（附产出机器与检出名） |

**本机状态**（只在 `board.sqlite3`）：

| 表 | 内容 |
| --- | --- |
| `meta` | 格式号与本机库标识（`instance`，运行在业务记录库里的唯一性用它区分同一机器上重建过的项目） |
| `checkouts` | 名称、槽位（均唯一）、kind main/worktree、路径、adapter 返回的变量、移除时间 |
| `local_work` | 运行与编写工作的本机部分：检出、会话、操作者、修复、启动、目录、范围、快照、分支、状态 prepared/submitted/abandoned、对应的业务记录编号；同一检出的 distill 只能有一个 prepared，run 可以有多个、按身份分开（已定决策 28） |
| `local_fixes` | 修复目录、文件基线、改动、复验运行与文件摘要 |
| `launches` | 无头启动：类型、runner、范围、会话、pid、日志、状态；同一检出只能有一个 running |
| `environments` | 各检出 adapter 环境：状态、pid、端口、ready 变量、快照、日志 |
| `browser_instances` | 身份浏览器（各检出共用，记下最近一次交给的检出）：实际调试端口（缺省端口被占用时为顺延后的端口）、pid、profile、有无界面、状态、租约持有者、退出码/信号、日志、崩溃报告、近期崩溃 |
| `service_events` | 运行中服务的停启与自动恢复；提交时随运行记录写进 `work.services` |
| `identity_events`、`fixture_runs` | 身份登录与验证码计数（不跨机器合并）、前置数据留痕 |

端口租约不在数据库中：守护进程用 `/tmp/sop-board-port-<uid>-<端口>.lock` 文件锁跨项目、跨检出租用端口，并在锁文件里记录占用的项目与检出。

## 12. 多人共享：共享库、产品与机器

**搭建共享库**：内网没有现成的 PostgreSQL 时，在一台常驻机器上用 docker 起一个，数据放在具名卷里；sop-board 自己不做备份，由维护的人按需要做每日 `pg_dump`：

```sh
docker volume create sopboard-pgdata
docker run -d --name sopboard-pg --restart unless-stopped -p 5432:5432 \
  -e POSTGRES_USER=sopboard -e POSTGRES_DB=sopboard -e POSTGRES_PASSWORD="$SOPBOARD_PG_PASSWORD" \
  -v sopboard-pgdata:/var/lib/postgresql/data postgres:16
# 每日备份（crontab 示例，凌晨 3 点；密码由 ~/.pgpass 提供）
0 3 * * * pg_dump -h <主机> -U sopboard -d sopboard -Fc -f ~/sopboard-backups/sopboard-$(date +\%F).dump
```

**每台机器**：`config.yaml` 写 `shared_database: {host: <主机>, port: 5432, dbname: sopboard, user: sopboard}`，可加 `password` 保存密码（文件权限 600），也可省略、由 libpq 使用 `~/.pgpass`（`<主机>:5432:sopboard:sopboard:<密码>`，权限 600；也可用 `PGPASSFILE` 指向别的文件）。主机、端口、库名只出现在本机命令输出与本机页面，不进报告、证据和内网页面。驱动 `psycopg[binary]` 随包安装。

**新建与加入产品**：写了 `shared_database` 而没绑定产品时，业务命令返回 `product-unbound`。`init <项目>` 列出共享库里的产品（名称、创建人、有结果的场景数、最近写入时间）和“本机 N 条场景中，有 M 条在该产品里已有结果（内容一致）”，以 `product-choice-required` 结束；由会话问使用者后，用 `init <项目> --join <ID>` 加入或 `init <项目> --new [--product <名称>]` 新建（名称缺省为本机项目名；空库时按当前版本建表）。`--join` 的 ID 不存在返回 `product-not-found`。绑定写在本机 `project.json`。绑定时另外提示两件事：本机已有的结果不带入共享库，已登记的守护会因验收结果不在共享库而失效、须重新验收后再沉淀；`regression.yaml` 引用、但本机没有登记的仓库别名。改绑重新运行 `--join` 或 `--new`，已写入原产品的记录留在原产品。新建、加入和共享都不读取、不比对仓库的 origin，不扫描目录，不往产品仓库写东西。

**连不上与版本**：连不上共享库时，读写业务记录的命令与看板页面返回 `database-unreachable`（本机写明主机），只用本机状态的命令（`env`、`browser`、`checkout`、`check` 的环境与运行时两项）照常工作；不做离线队列与本地缓存。版本不符时新旧两个方向都返回 `database-version`；这次没有升级命令。

**证据在别的机器上**：证据文件只在产出它的机器上。带 `--lan` 启动的看板把机器标识、显示名、内网地址与对等令牌登记到 `machines`（不带 `--lan` 或退出时撤下本端口的登记）。别的看板的证据地址仍是本看板的 `/evidence/<sha256>/<名称>`：本机没有文件时，看板凭登记的令牌从产出机器看板的 `/peer/evidence/<sha256>` 读取，核对 sha256 后转给页面，不在本机保存；读不到时场景详情写“证据在 <机器显示名> 的机器上”。`report --zip` 同样取回并核对；取不到的逐条写“证据在 <机器显示名> 的机器上，未包含”，输出 `missing_evidence` 为缺少的条数。

**机器与检出**：非结论按“机器标识 + 检出名”归属。同一个数据根目录（同一台机器）上绑定同一产品的两个项目，若检出名相同，会被当成同一个检出。

## 13. 设置接口（已定决策 27）

全部路径仅允许实际回环连接，内网返回 403 `local-only`。写入使用 JSON，沿用同源校验（403 `cross-origin`）。不返回密码及凭据取值。
每节 content 使用 config.yaml 原字段名：repositories 为 repos、scenarios、snapshot_exclude（identities 由服务登记）；verification 为 entries、skill；environment 为 adapter；fixtures 为 fixtures；agents 为 agents。省略字段表示删除该字段。

| 方法与路径 | 请求 | 成功响应 |
| --- | --- | --- |
| GET `/api/settings` | 无 | ok、digest（config.yaml 字节的 SHA-256）、sections、other_problems、notices、references、skills、credentials |
| POST `/api/settings/sections/{section}` | digest、content（上述五节之一） | 与读取相同的新状态 |
| POST `/api/settings/repositories/reregister` | digest、alias | 按当前 origin 登记身份，返回新状态 |
| POST `/api/settings/local` | operator、machine（均为非空单行文字，最长 64 字） | 新状态 |
| POST `/api/settings/shared/test` | connection、可选 password、clear_password | ok、version（空库 null）、expected_version、empty、products、local_results、unregistered_aliases、notes |
| POST `/api/settings/shared/connect` | digest、connection、可选 password、clear_password、action（join/new/keep）、product_id（join 必填）、name（new 可选） | 新状态 |
| POST `/api/settings/shared/disconnect` | digest | 新状态，回到本机业务库 |

connection 为 host、port、dbname、user 对象。password 省略表示保留当前保存的密码；页面的新密码框留空时省略 password，填入内容时使用新值。显式传空字符串返回 422 settings-invalid，field 为 password。只有 clear_password 为 true 时清除已保存密码、改用密码文件（同时提供非空 password 则使用新值）。两者都没有保存值时由 libpq 使用密码文件。测试连接只读，不保存。
repositories.content.repos 可为别名到路径的对象，也可为 `{path, alias?}` 列表；缺省别名按 init 规则生成。服务读取 origin，已登记仓库不能改名。重新登记身份也受仓库占用限制。

sections 每节含 content、problems（field、problem）、occupants（占用者列表）。local 的 content 含 operator、operator_source、machine、runtime、install_command、local_address、lan_addresses、data、machine_name_sync_pending（业务库中的机器名尚未同步）。shared 的 content 只含无密码连接，另含 password_saved 和 product。
references 为入口名到 `{count, scenarios}` 的对象（列前五条场景，遍历本机所有检出、按场景键去重）。skills 为数据目录 skills/ 下的技能目录 name 与校验 problems。credentials 每条为 `{name, present, source}`，同名变量按声明来源分别列出，仅表示启动看板的环境里是否有非空值。source 含 kind、field；入口声明的 kind 为 entry，另含 entry；身份声明的 kind 为 identity，另含 entry、identity（account_env）；前置数据声明的 kind 为 fixtures，另含 recipes（使用这组凭据的配方名称列表，凭据统一声明在 fixtures.credential_env）。不返回取值。
other_problems 为无法归入七节的 `{field, problem}` 列表，包括顶层格式外字段和只有产品绑定却没有连接的问题。notices 列出 config.yaml.prev 与残留临时文件的说明及人工修法；上一份副本本身是正常产物，不计为配置不合格。
occupants 含 kind、checkout；kind 为 environment、work、launch、fix、browser。工作与启动另含 id、work_kind；修复含 id、state（只列确认为 open 的修复）；环境含 state；浏览器含 identity，仅禁止删除正在用的身份。prepared 的运行、沉淀、合并、编写、草稿与交互地图，以及 open 修复和运行中的启动，均占用仓库与共享库。共享库连不上、无法确认本机已知修复是否结束时不占用、不返回 settings-locked；读取结果的 notices 逐条提示“共享库连不上，无法确认修复 #N 是否结束”，允许使用者修正连接或密码。其他已确认的占用仍照常生效。
products 的字段与 CLI init 的清单相同：id、name、created_by、created_at、scenarios_with_results、latest_write、local_overlap、hint。设置接口的 created_at、latest_write 使用与看板其他页面相同的本机时间格式 YYYY-MM-DD HH:mm:ss ±HH:mm（latest_write 没有记录时为 null），CLI init 的原始时间格式不变。页面将 ID 等于已绑定产品 ID 的条目标为“当前产品”，不提供加入按钮；其他产品仍可加入以切换产品。

| 错误码 | HTTP 与含义 |
| --- | --- |
| settings-stale | 409，磁盘摘要变化，须重新读取 |
| settings-locked | 409，problems 逐条用可读文字说明原因，错误体单列 occupants（格式同读取），保持现有工作与环境 |
| settings-invalid | 422，本节不合格、其他节新增问题或入口仍被引用；problems 含字段和说明 |
| database-authentication | 503，认证失败，本机说明主机、端口、库名与用户 |
| database-unreachable | 503，连不上共享库 |
| database-version | 409，表结构版本不符 |
| product-not-found / product-unbound | 409，目标产品不存在 / keep 时未绑定；须明确加入或新建 |

保存重写标准 YAML，原注释和排版不保留；上一份留为 config.yaml.prev。临时文件以 600 创建后替换。
接入按明确选择加入、新建或保持产品；发生错误恢复两个本机文件。共享库记录不迁移；断开尽力撤下当前看板的机器地址，即使共享库不可达也可断开。

本机姓名与机器名先保存在 operator 和 machine.json。只改操作者姓名或机器名未变时不访问业务库；机器名变更后尽力同步，失败仍保存成功，machine_name_sync_pending 为 true。下一次工作台请求触发机器登记，或看板重启并成功连接业务库时同步；带不带 --lan 都更新机器名。机器名未同步时不影响本机保存。

config.yaml 解析失败时，设置读取与保存返回 settings-invalid，保留解析器原始错误（含行号、列号和出错内容）。status/check 同样保留细节；内网只显示问题类别。测试连接不持跨进程文件锁；接入先在锁外建立连接，持锁再次核对摘要与占用，产品事务和文件写入仍持锁。

保存、接入、换产品、断开不提供崩溃恢复或跨文件、跨库原子性。status/check 指出有连接却未绑定、有绑定却没有连接、目标库无绑定产品等情况并给出设置接口、init --join/--new 或直接编辑配置的修法；不自动恢复副本或清理临时文件。普通步骤异常仍恢复原文件。命令行 init 只在数据库提交成功后写产品绑定。

本机响应与命令输出不包含 password 配置项，不对全文做密码取值替换。只有当前保存在 config.yaml 的密码参与内网文本、报告、文本证据及会话材料的遮蔽；测试连接与未保存的接入候选值不登记，替换或清除后移除旧值。静态资源、verbatim 对等响应、Range 片段及图片、视频等二进制内容保持原字节。密码须为非空文字，不设长度或单行限制，不裁剪空白。
