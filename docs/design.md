> 状态：2026-11-10 使用者 blur.yin 批准。这是 v10 的现行设计依据；行号指 tag v9-final 中的旧代码，仅说明沿用来源。

# Flow Board v10 重写方案

## Context

用户在实际使用 sop-board（cw2-live-chat 等项目）中整理出 7 个问题，要求从根因出发重新设计，直接替换，不留双轨与兼容代码。调查结论：这些问题有一个共同根因：项目靠决定条目（D1～D117）逐条打补丁扩张，而不去掉产生问题的结构。结果是：

- 两条入口（看板后台调度 + 会话认领）共用一个 25 种状态的任务内核；
- 看板在数据库里另存一份场景真相，并会回写仓库；
- “只认场景、不写测试”让确定性规则全部变成需要真实入口重跑的场景；
- 出于对 AI 的不信任，每次登记都要真人署名、只接管用户已打开的 Chrome。

本方案在 `feat/session-board-friction` 上重写（先把当前未提交改动提交留档，再切新分支），SOP-BOARD-22/23 不单独发布，由重写整体替代。

## 七个问题：根因与新设计

| 问题 | 根因（有出处） | 新设计 |
| --- | --- | --- |
| 1 自身场景冗余复杂 | 每个需求一个 yaml、只增不并（22 个文件 731 条，45% 是反向拒绝分支）；v9 禁止写测试，规则只能写成场景；没有退役；docs/verification.md 按批次逐章写重跑清单 | 删除全部 22 个 sop-board*.yaml 与 verification.md 批次章节，按 v10 能力重写一份**现行场景集**（目标约 60 条、只写端到端业务流程）；新需求直接改现行文件。验收通过后按新的沉淀流程把场景转成 `tests/` 下的 pytest，开发期间不写测试 |
| 2 看板布局，测试同学难按场景复测 | 页面以批次、轮次、任务为中心（`pages.py:324` 批次页 11 列轮次表），场景详情须经 `/w/<ws>/b/<batch>/s/<id>` 进入；标注表单排在步骤与证据之前（`pages.py:541`）；测试同学没有重跑入口，满屏是 沿用、待同步、独立性未声明 这类内部术语 | 三栏**场景工作台**：左侧是 文件→节点→场景 树、状态筛选、搜索、检出切换；右侧是四要素、最新证据（截图、录像、trace）、复测标注、本机“AI 重跑”和历史。内网只看到业务字段 |
| 3 不支持多 worktree | 工作区由数据目录里 `repos:` 的绝对路径确定；`locate_workspace` 只做路径精确匹配（`workspace.py:254-276`），在 worktree 里直接失败；场景文件、产品快照、`batches.product_digest`、环境和端口锁都只有一份（`repositories.py:224`、`statistics.py:33-55`、`adapters.py:343-398`）；cw2 只好另登记 `cw2-live-chat-strands`，与主工作区混用仓库路径 | 新增**检出（checkout）**概念：一个项目可以有 main 与多个 worktree 检出，按 git common dir 自动识别；场景文件、快照、结果、环境和端口都按检出隔离；端口 = 基础端口 + 槽位×`port_step`（为 0 时各检出共用端口，按租约轮流使用，冲突时说清楚是谁占用） |
| 4 通过后没有沉淀低成本测试 | 沉淀需要另行授权、只能手动发起，关联“不改变结论”；复测路径从不运行关联测试（`plans.py:43`、`prompts/execute.md:34`），产品一变就整条重跑 | 闭环：**独立通过 → 待沉淀 → 写测试 → 守护**。产品变化后由 `sop-board retest` 直接运行守护测试（不需要 AI），全部通过记为“守护通过”，失败、缺失或过期的才交 AI 走真实入口 |
| 5 只能接管已打开的 Chrome | 技能与模板只做 CDP 接管（`playwright-acceptance/SKILL.md:13`、`templates/connection.mjs:128`），身份就是用户手动开好的 CDP 地址（`identities.py:42`），看板只有一个浏览器名额（`kernel.py:29` WAIT_BROWSER）；Chrome for Testing 未安装（1.64.1 需要 1248，缓存里只有 1234） | 新增 sop-board 自有的**浏览器代理**：看板守护进程用自带的 Playwright 运行时安装、启动 Google Chrome for Testing，按 检出×身份 使用独立持久 profile 和调试端口；执行者仍通过 CDP 连接；不再碰用户日常 Chrome。真实账号由用户在这个有界面的 CfT 窗口登录一次，profile 持久保存 |
| 6 Playwright quit unexpectedly | 崩溃报告显示崩的是 WebKit 的 `Playwright.app`（webkit-2336，属于 `chan-product` 的 Playwright 1.62.1 的 iphone-webkit 项目）。它在 Paseo 下启动后约 50ms 就在 `_RegisterApplication` 中止，11-09 没有验收时也崩过；11-05 还有一次 CfT 渲染进程中止。规律是：从 agent 进程树里启动的 GUI 浏览器会崩，而且多个项目共用同一份 ms-playwright 缓存 | sop-board 的浏览器只由看板守护进程启动，缺省无头运行（不需要窗口），只在真人登录时开有界面窗口；使用独立的 `PLAYWRIGHT_BROWSERS_PATH`，只用 CfT、不用 WebKit；监测浏览器退出信号，崩溃时把场景记为受阻并写明原因，下次请求时重启。chan-product 的 WebKit 崩溃不在本仓范围，会单独告知用户 |
| 7 反复确认“确认人” | 每个登记命令都要人名（`--by/--author/--confirmed-by/--uploaded-by`），姓名只在一次请求内有效（`skills/sop-board/SKILL.md:24`）；询问后必须结束本轮（`SKILL.md:8`）；默认确认人要用户另行同意才设置，两个 cw2 工作区都没设；`plans/atlas decide/specs promote` 甚至不接受默认值 | **操作者身份按机器设一次**（默认取 `git config user.name`，可用 `sop-board operator set` 修改），所有 CLI 登记自动署名；只有别人确认时才用 `--as <姓名>`。技能里删除所有询问姓名的规则；内网表单的姓名存在浏览器 localStorage |

## 已定决策（2026-11-10 用户答复）

1. 单一路径：Agent 工作全部由会话经 CLI 完成；看板的“AI 重跑”只在本机可用，用同一技能在检出目录启动无头会话。删除任务内核、看板调度、认领与接管规则。
2. 保留：测试用例上传与合并、SOP 编写与草稿、交互地图与 spec 库、数据配方与身份池。
3. 场景以仓库文件为准：看板不存场景版本，也不回写场景文件；结果按内容指纹记录。
4. 不单独发布 22/23，在其分支上重写。
5. 看板采用三栏场景工作台。
6. 保留 adapter 脚本：看板守护进程用 adapter 按检出启停环境，加入端口偏移。
7. 守护测试由 `sop-board retest` 直接运行。
8. 沉淀时机：通过后进入“待沉淀”，由人在会话里或点看板按钮触发。
9. 本仓开发期间不写测试，先验收再沉淀成 pytest。
10. 内网不能发起 AI 重跑，只能人工复测标注、答复问题、批注草稿、上传用例。
11. 一并删除：问题台账与决定记录、旧方案与设计文档、执行计划与连续运行、只读巡检 patrol；Jira 联动删除。
12. 场景集确认时的补充（2026-11-10）：守护进程、服务与浏览器可以按验收需要在 agent 会话中启动；会话可用 `inbox add` 上传用例或 SOP；运行时与操作者文件始终在 `~/.sop-board`；specs 只保留 check；错误码 `run-prepared`、`launch-running`、`draft-not-runnable`、`project-format`、`browser-crashed`、`env-busy`、`browser-busy`；`fix close` 须独立复验通过且修复文件已提交并与复验时一致；漏检后重新沉淀须有新的独立通过。
13. 补回人工复核（2026-11-10）：重写时把“Agent 通过、等待人工复核”的队列和误判统计一并去掉了，与标准中“所需人工复核已完成”的交付条件不符。改为“待人工复核”徽标与筛选；统计 Agent 独立通过的人工确认率、复核覆盖与误判明细。人工标注不通过仍直接使场景显示为失败。
14. 不绑定产品快照的导入结果（2026-11-10）：cw2 从 v9 看板迁到 v10 时，用户决定把旧看板的执行结果与证据导入新看板，且产品变化后这些通过不转待复测。导入结果的 `product_digest` 为空，表示不绑定产品快照：最新结果为这种通过时状态保持“通过”，Agent 独立通过照常显示“待人工复核”，人工结论不论在哪个快照上标注都与它配对；该场景之后有新的执行或人工结论，就按新结果和正常规则判定。看板自身的运行与人工标注总是绑定快照。导入由一次性脚本直接写库，本仓不提供导入命令，也不读取旧格式数据。

## 目标架构

### 核心概念（只有这些）

| 概念 | 定义 | 存放 |
| --- | --- | --- |
| 项目 | 原“工作区”，由名称和若干仓库组成。仓库身份取规范化后的 origin | `~/.sop-board/<名称>/config.yaml`、`board.sqlite3`（格式 6，旧目录直接拒读，不迁移） |
| 检出 | 项目各仓库的一套工作副本。`main` 是登记路径；worktree 检出按 git common dir 自动识别：当前仓库和同一父目录下的兄弟 worktree 组成一套，缺的仓库回落到 main。每个检出有槽位号 | 数据库 `checkouts` 表 |
| 场景文件 | 产品仓库 `scenarios.path` 下的 yaml，**唯一真相**。从检出目录现读（按 mtime 缓存），格式沿用 `scenario_file.py`，元数据增加 `status: draft\|confirmed` | 产品仓库 |
| 场景键 | `<文件名>#<场景编号>`，指纹沿用现有算法（四要素） | 计算得出 |
| 运行 | 一次执行或复验：范围、检出、产品快照、执行者、实现者、独立性 | `work` 表（kind=run）+ `runs/<编号>/` 目录 |
| 结果 | 运行中某场景的结论与证据，绑定 指纹 + 快照 | `results` 表（kind=agent），证据按 sha256 存储 |
| 标注 | 测试同学对场景当前内容的复测结论（通过 / 不通过 / 阻塞 / 需求变更，可附缺陷链接、说明与姓名，均不必填） | `results` 表（kind=manual） |
| 守护 | 场景到低成本测试的关联，绑定场景指纹与测试文件摘要 | 产品仓库 `<scenarios.path>/regression.yaml` |
| 守护运行 | 一次在某快照上运行守护命令的原始结果 | `guard_runs` 表 + 输出文件 |
| 修复 | 失败场景 → 会话在当前检出改产品 → 独立复验运行 → 提交后关闭 | `fixes` 表 |

### 场景状态（按检出推导，不存储）

- **未执行**：当前指纹没有结果。
- **通过**：最新结果为通过，指纹相同，快照等于当前快照（包括按影响判定沿用的结果）；或最新结果是不绑定产品快照的导入通过（见已定决策 14），产品变化不使它转待复测。
- **守护通过**：此前在同一指纹上经真实入口独立通过；产品变化后，当前快照上的守护运行全部通过。
- **待复测**：通过发生在旧快照上，且当前快照没有守护通过；或测试同学标注不通过后修复已关闭。
- **失败 / 受阻**：最新结果如此（测试同学标注不通过按失败、标注阻塞按受阻显示）。
- **需求变更**：测试同学复测时标为需求变更。场景文件按新需求修改后指纹变化，回到未执行。
- **待沉淀**（徽标，可与“通过”并存）：已有独立真实入口通过，但没有有效守护。
- **待人工复核**（徽标，可与“待沉淀”并存）：状态为通过，且最新结果是 Agent 经真实入口的独立通过（不是保留通过、执行期间产品无变化），之后还没有人工标注。测试同学标注任一结论后徽标消失；产品变化后状态离开通过，徽标随之消失，因为人工复核须与 Agent 看的是同一产品快照。
- **Agent 独立通过的人工确认率**：样本是每条 Agent 独立真实入口通过（同上条件），配对同一产品快照上（样本是不绑定快照的导入结果时不限快照）、关联该结果的最近一条人工“通过”或“不通过”；分子为人工通过，分母为人工通过加不通过。人工阻塞与需求变更不进分母，单列计数。复核覆盖 = 有配对的样本数 / 全部样本数。人工不通过的样本列为误判明细，供每周复盘逐条归因（Agent 判错、场景缺漏、环境等）。

### 进程与入口

- `sop-board serve [项目] [--lan] [--port]`：**看板守护进程**，可以在终端或 agent 会话中启动（使用者 2026-11-10：可以根据验收需要启动服务和浏览器）。负责：网页、按检出管理 adapter 环境、浏览器代理、本机无头会话启动器。CLI 需要环境或浏览器时，通过回环 HTTP 调用守护进程（数据目录里的本机令牌文件鉴权）；守护进程没运行时直接报错并给出启动命令。
- **会话 CLI** 是 Agent 工作的唯一入口。看板上的“AI 重跑”“沉淀”按钮（仅本机）用同一份 `sop-board` 技能在检出目录启动 `codex exec` 或 `claude -p`（按 `agents` 配置），只记录进程号、日志和退出码；同一检出同时只运行一个。

### 检出与环境（问题 3）

- **识别**：`gitops` 对 cwd 运行 `git rev-parse --show-toplevel --git-common-dir`。common dir 等于某个已登记仓库时，该仓库就属于当前检出；同一父目录下的兄弟目录，如果是其他已登记仓库的 worktree，也并入同一检出；剩下的仓库回落到 main。检出第一次被看到时登记并分配槽位，main 的槽位固定为 0。`checkout create <名称> [--from ref]` 沿用 adapter.worktree 和 cleanup 契约，复用 cw2 的 `worktree.py`，并沿用 `worktrees.validate_path` 的三项核对。
- **环境**：守护进程按检出运行 `adapter.env`，传入 `SOP_CHECKOUT`、该检出各仓库的 `SOP_REPO_<别名>` 和 `SOP_PORT_OFFSET = slot × port_step`。实际端口为 `ports + offset`；入口地址变量从 adapter 的 ready 输出里读取。
  - `port_step: 0` 时各检出共用同一组端口。此时租约属于持有端口的检出，另一检出的 `run prepare` 和 `env up` 直接返回 `env-busy`，并写明占用的检出、运行编号和 `env down` 命令。不排队，也不互相覆盖。
  - 环境记录启动时的快照，快照变化后由 `run prepare` 重启。
  - 守护进程重启时，清理记录在案、但已成为孤儿的进程组。
- **check**：只做三件事：校验配置；在当前检出拉起环境并探测就绪；核对浏览器运行时。删除五步自检中的工作树建立与清理步骤，也不再把它当作执行门槛，因为 `run prepare` 拉起环境本身就是核对。
- **快照**：`gitops.product(checkout)` 沿用分批 git 读取，按 各仓 HEAD + `git status --porcelain` 输出缓存。场景目录、`snapshot_exclude` 以及 `regression.yaml` 引用的测试文件不计入快照：守护按测试文件的 sha256 判断有效，所以沉淀出的测试不会让所有通过都变成待复测。

### 浏览器代理（问题 5、6）

- **运行时**：`src/sop_board/assets/runtime/` 带 `package.json`（固定 `@playwright/test` 1.64.1）和 lock 文件。`sop-board browser install` 在 `~/.sop-board/runtime/` 中 `npm ci`，再设置 `PLAYWRIGHT_BROWSERS_PATH=~/.sop-board/runtime/browsers`，执行 `playwright install --no-shell chromium ffmpeg`。可执行文件路径取自 `playwright-core` 的 `chromium.executablePath()`，与 CfT 版本一起写入 `runtime.json`。此后不再读 `~/.sop-board/node_modules` 和共享的 `~/Library/Caches/ms-playwright`。
- **启动**：只有守护进程能启动浏览器，执行者不自己启动。守护进程可以在 agent 会话中运行；浏览器缺省无头，启动即崩溃时按下面的崩溃规则记受阻并写明信号与日志。
  - 每个 检出×身份 一个浏览器：`--user-data-dir=<数据目录>/browsers/<检出>/<身份>/profile --remote-debugging-port=<端口> --no-first-run --no-default-browser-check --use-mock-keychain`。
  - 不加 `--enable-automation`。默认无头运行（`--headless=new`）；`browser open --headed` 和真人登录使用同一份 profile。
  - 端口 = `9400 + slot×20 + 身份序号`，被占用时直接报错。
  - `/json/version` 15 秒内有应答才算就绪。
- **租约**：`run prepare` 为本轮 Web 场景用到的身份取得租约，确保对应浏览器已启动，并写出 `runs/<编号>/run.env`，其中有 `PW_CDP_ENDPOINT`、`PW_IDENTITIES`（每个身份带 endpoint）、`PLAYWRIGHT_BROWSERS_PATH`、`PW_NODE_ROOT`。身份被真人登录占用时，直接返回 `browser-busy`。删除 `WAIT_BROWSER`，也删除身份的 `cdp` 字段。
- **真人登录**：`browser open --identity X --headed` 在有界面的 CfT 窗口打开入口地址，并持有 human 租约。用户自己输入密码、完成 2FA，profile 保留登录态；之后用 `browser close` 释放。出现 `browser may not be secure` 时停止，该身份的场景记为受阻并写明原文。不回退到用户日常 Chrome，也不改 UA 或参数绕过。
- **崩溃**：监视线程 `wait()` 浏览器进程。异常退出时，记录信号、`chrome.log` 末尾，以及启动之后写出的 `DiagnosticReports/Google Chrome for Testing*.ips`。持有租约的运行在提交时，相关场景记为受阻，finding 写成“浏览器进程 <时间> 因 <信号> 退出，日志 <路径>”，同时加一条待人处理。租约持有期间不自动重启，下一次取得租约时再启动。30 分钟内异常退出两次，状态记为 crashed，须由人运行 `browser open --reset-crash`。模板里补上 `page.on('crash')` 和 `/Target crashed/`，渲染进程崩溃也按受阻处理。
- **playwright-acceptance 技能改动**：
  - `templates/connection.mjs` 只读 `PW_CDP_ENDPOINT`，删掉 7-19 行（DevToolsActivePort、`PW_CHROME_USER_DATA`）和 39-114 行（`targetTransport`、`PW_CDP_MODE`），保留 `connectOverCDP :128`。
  - `identity.mjs:25-28` 改用 `spec.endpoint`。
  - `fixtures.mjs` 补上崩溃处理；`global-setup.mjs` 只检查 endpoint。
  - `SKILL.md` 和 `references/{web,workflow,shopify,commands}.md` 改成“连接看板启动的 Chrome for Testing；执行者不启动浏览器，也不运行 playwright install”。
  - 删除 `scripts/patrol.mjs`，`references/spec-library.md` 改名为 `spec-fidelity.md`。

### 守护与沉淀（问题 4）

- **`<scenarios.path>/regression.yaml`**：放在产品仓库，随检出走。

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
      coverage: {expected: [{index: 0, text: "…", tests: [t1]}], forbidden_effects: [{index: 0, text: "…", tests: [t1]}]}
      detection: {method: mutation, assertion: "…", evidence: <证据 sha256>}
      ledger: []   # 浏览器层测试的保真账本
  ```

- **守护有效的条件**，缺一不可：
  - 场景存在，当前指纹相同；
  - 各测试文件是仓库内的普通文件，且 sha256 一致；
  - coverage 逐条、按顺序覆盖全部 expected 和 forbidden_effects，文字与当前一致；
  - 检出能力的证据存在；
  - `acceptance` 在数据库中是同指纹的独立通过；
  - 没有登记过漏检。

  只覆盖了部分预期的守护不写进索引，这类场景照常重跑。
- **`sop-board retest`**：由守护进程执行，不需要 AI。
  1. 记录快照，列出待复测场景。
  2. 运行有效守护引用的命令：只用 argv，不经 shell；工作目录是检出对应仓库下的 `cwd`；超时后结束整个进程组；环境变量是该检出的 adapter 环境加上运行时变量；声明了 `browser:` 的命令先取得租约，再注入 CDP 变量。
  3. 每条命令退出码为 0，声明的测试名都出现在 junit 里且结果为通过（跳过或缺失都不算），并且运行前后快照不变，才写入一条“守护通过”的 `guard_runs`，原始输出存为证据。
  4. 其余情况把场景留在待复测，并附上原因。

  只运行一次 retest 的范围由 `--dry-run` 预览；仓库里有未提交改动也可以运行，因为快照包含工作区内容。
- **漏检**：某场景最近一次是守护通过，之后真实入口执行或人工复测发现失败时，记一条漏检。该守护随即失效，直到重新沉淀，且测试摘要发生变化。
- **沉淀**：
  - 同一指纹上已有独立真实入口通过、但没有有效守护的场景，显示为“待沉淀”。
  - `distill prepare [F#ID…]` 在当前检出准备工作目录。会话在当前检出写测试（不提交），写出 `guard.json` 后运行 `distill submit`。
  - submit 交给守护进程处理：按上面的有效条件核对；浏览器层测试另跑保真检查（沿用 `spec_library.ast/check_one/qualified :109-122,465-565`），基线是 `git show HEAD:<path>`，首次以产生独立通过的那份 spec 为准；然后按 retest 的规则实际运行一次；全部合格后合并进 `regression.yaml`，留给开发者审核提交。
  - 看板上的“沉淀”按钮仅本机可用，作用是启动一个无头沉淀会话。
- **spec 库并入守护的浏览器层**：`regression.yaml` 取代 `INDEX.json`，成为唯一索引。`seed()` 改为从 `regression.yaml` 预置：守护有效就预置，过期的只作参考。删除 `index/version_digest/promote/import_cw2`、`_history` 和 `baseline` 目录。
- **regression-distill 技能改动**：
  - `SKILL.md:8` 改为“完整守护可以在产品变化后记为守护通过”；
  - 第 15-16 行改为只认独立通过，删除“独立验收未完成可以沉淀”；
  - 检出能力核对改为必需；
  - 产出改为 `guard.json` 加 `distill submit`，删除 CI 段落和按提交重新登记；
  - `references/report.md` 改成上面的结构。

### 看板：三栏场景工作台（问题 2）

- 路由：`/` 是项目列表（只有一个项目时直接进入）。`/p/<项目>?checkout=<名称>` 是工作台：左栏是 文件→节点→场景 树，每条带状态点；顶部是检出切换、搜索和状态筛选（待复测、失败、待人工复核、待沉淀、通过、守护通过、未执行、草稿），各筛选带计数；概况页另显示 Agent 独立通过的人工确认率与复核覆盖。右栏顺序固定：
  1. 标题和徽标（P 级、入口、正反向）；
  2. 当前状态和原因（例如“产品已变化，守护未覆盖”）；
  3. 起点、步骤、预期、禁止（预期和禁止逐条列出，附守护覆盖标记）；
  4. 最新结果和证据画廊：截图、内联视频（沿用 Range 播放）、trace、请求响应；
  5. 复测标注表单：通过、不通过、阻塞、需求变更；缺陷链接、说明与姓名都可不填，每个输入框独占一行；姓名在顶栏填一次，各页面与场景共用，使用者主动修改才变；
  6. “AI 重跑此场景”“运行守护”“沉淀”三个按钮，仅本机；
  7. 历史（折叠）。

  URL 带 `#<场景键>`，可以直接分享。
- 其他页面只保留：收件箱和待答复（上传用例、回答问题）、草稿批注、SOP 原文、交互地图、待人处理、运行记录（本机可以看日志）、报告导出。
- 内网可以：查看、复测标注、回答问题、批注草稿、上传用例或 SOP。不显示本机路径、会话编号、命令和凭据变量名。内网的写入只限上面几种；其他写入，以及发起运行、沉淀、守护，都只接受实际回环连接。POST 继续核对同源和 JSON 类型。
- 删除的界面术语：批次、轮次、待同步、已作废、沿用（改为“按影响判定保留通过”，放在历史里）、执行入口、看板与会话之分、任务状态。独立性只在历史里显示。

### 保留能力的简化形态

- **用例上传与合并**：内网上传的文件进收件箱（`inbox`，先用 `scenarios.cases` 校验格式）。会话运行 `merge prepare [--inbox ID…]` 后，直接编辑仓库里的用例和场景文件，原来的看板版本、配对、缺口、增量和全量合并都不再需要：
  - 有疑问时，在对话里问用户，或用 `ask --about inbox:ID|F#ID` 把问题挂到看板“待答复”；测试同学答复后，下一次合并读取。
  - `merge submit` 用 `load_scenarios` 校验改后的文件，输出场景差异（新增、移除、改动编号，以及角色或入口的变化），并把收件箱条目标为已合并。
  - 场景的 `refs` 和 `metadata.sources` 记录来源与确认人；确认人自动取操作者，或取答复人。
- **SOP 编写与草稿**：
  - `sop prepare --source` 复制资料、给出提示词；会话在仓库里写 SOP Markdown，有问题在对话中问；`sop submit` 校验六个标题和规则编号（沿用 `sop_markdown`），并在文件头写入确认人。
  - 草稿就是带 `metadata.status: draft` 的场景文件，不进入执行。测试同学在看板批注，批注存数据库；会话按批注修订后 `draft resolve`；`draft confirm <文件>` 把状态改为 confirmed，并写入确认人。
- **交互地图**：地图文件放在产品仓库的 spec 库目录下，记录前端源码摘要，`atlas stale` 按摘要判断过期。沿用 `atlas.py` 的过期、校验和渲染逻辑，删除版本表、绑定、界面对照决定和看板任务。
- **数据配方与身份**：配方在 config.yaml 中声明，`fixtures run/exec` 的留痕写进运行目录和 `fixture_runs`。身份对应浏览器代理的 profile，`identity status` 显示登录与验证码计数。“待人处理”只保留 列表、新增、解决 三个操作。
- **报告**：`report [--file F] [--zip]` 导出某个检出的 Markdown 报告（场景状态、证据、独立性、标注、守护通过、Agent 独立通过的人工确认率与误判明细、待人工复核、未执行范围）和证据 zip。

### 独立性（取代交接仪式）

- 首选做法：协调会话运行 `sop-board run launch …`，由守护进程启动一个新的无头会话执行。这个会话只拿到 prompt 文件，会话编号由启动器取得（Claude 用 `--session-id`，Codex 读 `thread.started`），启动记录就是独立性的证据。原来的两段交接、handoff 文件与替代标签规则全部删除。
- 当前会话自己 `run prepare` 时，执行者取自 `CODEX_THREAD_ID` 或启动器注入的 `SOP_BOARD_SESSION`，取不到就要求 `--session`。实现者有三个来源：修复复验时自动取修复会话；否则用 `--implementer` 声明；不声明记为“未声明”。仍然分三类：独立验收、实现者自检、未声明。

### 本仓自身的场景、演示项目与文档

- **演示项目入仓**：新增 `examples/demo-shop/`，包含 app（stdlib HTTP 加 SQLite 的 Web 与 API，预埋一个缺陷）、lib 两个仓库的生成脚本，以及 adapter 的 `env.py` 和 `worktree.py`（支持 `SOP_PORT_OFFSET`）、`config.yaml` 样例和验证手册。验收前用一条命令在临时目录生成两个 Git 仓库并 `init`，不再每轮在仓外临时搭建。
- **现行场景集**：删除 `docs/sop/sop-board*.yaml`（22 个），新写 `docs/sop/` 能力文件，每条都对应 v10 的端到端业务流程，正反向都有，目标约 60 条：
  `setup`（安装、登记、操作者、检出识别与创建、check）· `environment`（按检出启停、端口偏移与租约冲突）· `browser`（CfT 安装与启动、身份 profile、真人登录、崩溃记受阻）· `workbench`（场景树、筛选、详情、证据、内网范围、复测标注）· `run`（准备、提交、影响判定、独立性、启动器）· `guard`（待沉淀、沉淀登记、retest 守护通过与回落）· `fix`（修复、独立复验、关闭）· `cases`（上传、合并、问答）· `authoring`（SOP 编写、草稿批注与确认）· `atlas-specs`（地图过期、spec 保真）· `fixtures`（配方、待人处理）· `report`。
- **验证手册**：`docs/verification.md` 重写为一页，内容是演示项目的搭建方法，以及四类入口（CLI、HTTP、浏览器、真实 Codex/Claude 会话）的执行与留证方法。删除按批次分章的写法。
- **新需求流程**（写进 AGENTS.md）：直接修改现行能力文件中受影响的场景，并在版本说明里记来源；被取代的场景直接删除（历史在 Git）；不再每个需求新开一个 yaml。
- **测试**：开发期间不写测试。现行场景集经独立真实入口验收通过后，按新的沉淀流程把 sop-board 自己的场景转成 `tests/`（pytest，`[dependency-groups] dev`），守护索引写在 `docs/sop/regression.yaml`。AGENTS.md 中“不恢复根 tests/”一句改为这条规则。
- **删除文档**：`docs/designs/`、`docs/proposals/`（含 11-09 未跟踪方案）、`docs/acceptance/`，以及 `.tw/overview.md`、`.tw/rules/*` 中的 D 编号沿革，重写为现行事实。`docs/reference.md` 只描述 v10 现行行为，不带 D 编号。`docs/standard.md` 修改第 1、5、6 节：“守护通过”可以作为产品变化后的复核依据，首次验收、场景内容变化和缺陷复验仍须经真实入口。README 与 `guide.md` 同步改写。

## 代码结构：约 22k 行 → 约 9.3k 行，24 个模块

开始重写前给当前代码打 tag `v9-final`，要沿用的代码用 `git show v9-final:<path>` 取出。下表是新模块和它们沿用的旧代码（行号以 v9-final 为准）：

| 新模块 | 职责 | 主要沿用 |
| --- | --- | --- |
| `cli` | 全部命令、JSON 输出、操作者署名 | `cli.py:37 version()` |
| `config` | 项目标记 `project.json`（`sop-board-project-6`）、config 校验、init、操作者文件 | `workspace.py` 的 `digest :22`、`origin_identity :167`、`ConfigLoader :184`、`repository :200`、`initialize :220`、校验 `:315-457`；`entries.py:41-113,130,150` |
| `gitops` | git 调用、产品文件清单、快照（按 HEAD 加 `git status` 缓存） | `repositories.py:15,26,47,109,169`；`workspace.py:513-537 product()`，改为接收检出目录 |
| `checkouts` | 从 cwd 识别检出、合并兄弟 worktree、分配槽位与端口偏移、create/remove | `worktrees.py:15,19,42,54,58-82 validate_path,106-167 build,171-195 cleanup` |
| `store` | 格式 6 的表结构、事务、证据存储 | `storage.py:359 WorkspaceLock`、`:409-442` |
| `scenarios` | 现读场景文件、计算指纹、校验场景与用例、草稿状态 | `scenario_file.py:15-163`；`cases.py:19,26-168`；`service.py:33-44 fingerprint/canonical` |
| `status` | 按检出推导场景状态与统计 | `statistics.py:12,20,28,33 stale,167-173,181-186` |
| `runs` | `run prepare/submit/abandon/launch`、人工标注 | `executions.py:32-85 write_brief,369-455,406-439 changes,457-552 read_results(影响判定、沿用、证据路径),554`；`validation.py:15,167-204` |
| `fixes` | 修复的 prepare/submit/close/abandon | `session_fixes.py:16,20,141-156,418-489 land→close` |
| `guards` | `regression.yaml`、`retest` 运行器、distill prepare/submit | `regressions.py:69-117,120,144-169,172-247,330` |
| `envs` | 按检出运行 adapter.env、端口租约与偏移 | `adapters.py:25-47 stop_process,50-81 port_owners,84,257-291,300-335,343-373 _acquire_ports(+偏移),389-464` |
| `browsers` | Chrome for Testing 代理（见下节） | 新写 |
| `launcher` | 无头 codex/claude 会话，同一检出一次一个 | `kernel.py:462,937,942,979-990,1019-1039,1042-1066`；`claude_runner.py` 原样保留 |
| `web` | 路由、本机与内网权限、视频 Range、遮蔽 | `http.py:22,42-60,151-191,463-474` |
| `pages` | 工作台外壳、SOP 页、地图页 | `pages.py:78 layout,431-461 evidence_preview,613,1455,1505` |
| `authoring` | 收件箱、合并、问答、SOP 编写、草稿批注与确认 | `author_sop.py:17,24-44`；`generations.py:66-79` |
| `atlas` | 地图文件放在仓库里，负责过期判断、校验、渲染 | `atlas.py:70,83,97,117,135,164-200,795,942`；`drafts.py:159-186 schema_problems` |
| `specs` | spec 库预置、保真检查、promote | `spec_library.py:74,96,109,186,492-557,666-797`（删掉 `import_cw2`） |
| `ledger` | 待人处理、身份计数、数据配方 | `attention.py:13,19-50`；`identities.py:22,58-94`；`fixtures.py:29,49-103,126,230-328` |
| `reports` | Markdown 报告与证据 zip | `reports.py:16-28,75-92`；`service.py:902-925` |
| `support` | 遮蔽、凭据环境、启动进程、网络、错误、时间 | `credentials.py:16-345`；`defect_links/errors/time_format/network/evidence_types` 原样；`tasks.py:27-38` |
| 保留模块 | `sop_markdown`、`playwright`（删掉 `_submit :101-180`）、`skill_install`、`claude_runner` | |

**整体删除**：`kernel`（只取上表列出的函数）、`task_types`、`tasks`、`session`、`service`、`task_visibility`、`result_files`、`scenario_edits`、`case_amend`、`case_export`、`drafts`、`generations`、`merge_conversation/delta/progress/state`、`fixes`、`fix_validation`、`repair_repositories`、`distill*`、`issues`、`jira`、`patrol`、`plans`、`validation`，以及旧 `repositories.Discovery` 后台扫描。

**提示词**：`execute.md` 与 `fix-verify.md` 合并成 `run.md`，保留影响判定与沿用、结果格式、Web、凭据各段。`fix.md`、`distill.md`、`atlas.md`、`merge.md` 重写：合并改成直接编辑文件，向测试同学提问用 `ask`。`generate.md` 改名 `draft.md`；`author-sop-guide.md` 并入 `author-sop.md`。新增 `launch.md`，作为无头会话的外层提示。只保留 `atlas.schema.json`，其余 schema 与 `merge-delta.md`、`atlas-bind.*` 删除。

### 数据库（格式 6，`board.sqlite3`）

`meta` · `checkouts`（name、slot 唯一，paths_json，kind main/worktree）· `work`（kind 为 run/fix/distill/merge/author/draft/atlas，检出、会话、操作者、目录、范围、快照、实现者、状态 prepared/submitted/abandoned；同一检出的 run 和 distill 各只能有一个 prepared）· `results`（运行 + 文件 + 场景唯一；按 检出、场景、指纹、快照 建索引；kind 区分 agent 与 manual，带独立性字段）· `evidence` + `result_evidence` · `guard_runs` + `guard_misses` · `fixes` · `launches`（同一检出只能有一个 running）· `leases`（端口、浏览器、human 租约）· `browser_instances`（检出、身份、端口、pid、profile、退出信号、日志、状态）· `inbox` · `questions` · `annotations` · `attention` · `identity_events` · `fixture_runs`。共 18 张表，没有任务状态机。

### CLI 一览

| 类别 | 命令 |
| --- | --- |
| 搭建 | `init`、`guide`、`check`、`operator [show\|set]`、`skills install`、`serve [--lan] [--port]`、`status [--json]` |
| 检出 | `checkout list\|show\|create <名称> [--from ref]\|remove <名称>` |
| 守护进程 | `env status\|up\|down\|restart`；`browser install\|status\|open --identity I [--headed]\|close` |
| 执行 | `run prepare [--scope pending\|all\|F#ID…] [--fix N] [--session S]`、`run submit --run N [--implementer X]`、`run abandon`、`run launch [--scope …]`、`run wait`；`retest [--dry-run]`；`mark F#ID pass\|fail\|blocked\|changed [--defect URL] [--note 说明] [--as 姓名]` |
| 修复 | `fix prepare [F#ID…]`、`fix submit`、`fix close`、`fix abandon` |
| 沉淀 | `distill prepare [F#ID…]`、`distill submit --report r.json` |
| 编写 | `inbox add <文件> [--kind cases\|sop]\|list\|show\|done\|reject`、`merge prepare/submit`、`ask --about F#ID\|inbox:ID --text`、`answers`、`sop prepare/submit`、`draft annotations\|resolve\|confirm <文件>` |
| 其他 | `atlas prepare/submit/stale`、`specs check`、`fixtures run\|exec`、`identity status`、`attention list\|add\|resolve`、`report [--zip]`、`launch status\|stop`、`playwright guide\|convert\|tags` |

除 `status` 的文字输出外，所有命令都输出 JSON：成功退出 0，业务失败退出 1，参数错误退出 2。

### 署名

`~/.sop-board/operator`（缺省取 `git config user.name`）是本机操作者。CLI 的所有登记都自动以操作者署名，只有别人确认时才加 `--as <姓名>`，因此 `--by/--author/--confirmed-by/--uploaded-by` 参数全部删除。会话可以问业务内容（“这些预期是否确认”），但不再问“是谁”。内网表单的姓名在顶栏填一次后存在浏览器 localStorage，各页面共用；复测标注不填姓名时记为未署名（本机页面缺省为操作者）。

技能文本要删掉以下规则：`skills/sop-board/SKILL.md:8` 中“询问后结束本轮”对署名的适用、`:24` 与 `:29` 两段署名规则、`references/ledger.md:21`、`merge.md:27`、`draft.md:5,8`，以及 `prompts/author-sop.md:29`。

## 实施阶段（同一分支，每个阶段结束时包都能导入，CLI 可用）

0. **留档与切分支**：把 `feat/session-board-friction` 的 14 个未提交文件提交（只用于留档），打 tag `v9-final`，切出 `v10-rewrite`。旧数据目录（`~/.sop-board/cw2-*`）保持原样，不动。
0.5 **先定验收场景**：由不参与实现的审核方新会话按本方案写出 `docs/sop/` 现行能力场景集（约 60 条），用户确认预期后，实现会话只读这些文件。这样验收目标在动手前就确定，不会按实现结果反推。
1. **删除与地基**：删掉“整体删除”所列模块和 `docs/designs|proposals|acceptance`，新建 `support/gitops/config/store/scenarios/checkouts`（先做识别）。CLI 先有 `init/guide/operator/checkout list|show/status`（此时只能显示未执行）。风险：检出识别的边界情况，包括符号链接、兄弟目录属于其他项目、同一路径登记两次。
2. **守护进程**：`envs`（偏移与租约）、`checkout create/remove`、最简版 `web` 加守护进程客户端（`daemon.json` 与本机令牌）、`env` 命令、精简后的 `check`。风险：`port_step: 0` 时的租约交接，以及守护进程重启后清理孤儿进程组。
3. **执行与浏览器**：`runs`、`status`、`marks`、`reports`、`browsers`（安装、启动、租约、崩溃处理）、playwright-acceptance 模板改动、`run.md` 提示词。风险：快照缓存；运行中途场景文件被改时，只作废指纹变化的场景；profile 锁。
4. **工作台与启动器**：`pages`、`board.js/css` 重写、内网标注、`launcher`、`run launch/wait`、“AI 重跑”按钮。风险：无头 codex/claude 的沙箱参数要能写检出和数据目录、能访问回环地址上的守护进程，同时不让会话进程树去启动浏览器。
5. **修复、守护、沉淀**：`fixes`、`guards`、`retest`、`distill`、`specs` 并入守护，regression-distill 技能改写。风险：retest 运行的是仓库文件里声明的命令，因此只允许 argv、不经 shell、使用过滤后的环境，且只接受本机触发。
6. **编写类能力与收尾**：`authoring`（收件箱、合并、问答、SOP、草稿）、`atlas`、`ledger`；`sop-board` 入口技能与全部提示词重写（技能文本按 writing-for-agents 规范）；README、`guide.md`、`docs/reference.md`、`docs/standard.md`、`docs/sop-guide.md`、`docs/playwright-acceptance.md`、AGENTS.md、`.tw/` 重写。
7. **演示项目与验证手册**：`examples/demo-shop/` 与一页式 `docs/verification.md`。场景集已在 0.5 定稿，实现与返工会话只读；内容与方案不一致时写进仓库外报告，由审核方修改。
8. **独立验收，然后沉淀**：未参与实现的新会话按 `docs/verification.md`，经真实入口执行现行场景集，留证放在仓库外。通过之后，用新的沉淀流程把 sop-board 自己的场景转成 `tests/` 下的 pytest，并写 `docs/sop/regression.yaml`。合入 main 的时机由用户决定。

## 验证

- **每阶段实现者自检**（只用于交付准备，不冒充验收）：`uv run sop-board …` 对 `examples/demo-shop` 生成的临时项目走通本阶段新增的命令；HTML/JSON 自检与浏览器操作分开留证，留证放在仓库外 `~/sop-board-runs/v10/`。
- **端到端冒烟**（阶段 4、5 完成后）：
  1. 运行 `sop-board serve demo --lan`，从另一台设备通过实际的非回环地址打开工作台。
  2. `checkout create wt1`，验证 main 与 wt1 的环境：`port_step` 不为 0 时能同时运行；为 0 时返回 `env-busy` 并写明占用者。
  3. `browser install`，确认安装的是 CfT 1248；`browser open --headed` 弹出有界面的 CfT 窗口，在其中登录合成账号；之后无头运行时登录态仍在。
  4. 用 `run launch` 启动真实的 Codex 和 Claude 执行会话，结果在工作台出现，并显示独立性。
  5. 在内网标注一条不通过，填写缺陷链接，期间不被要求填姓名以外的身份。
  6. 沉淀一条场景，再修改产品：`retest` 显示守护通过；对预埋的缺陷做受控改错后，`retest` 把该场景回落为待复测，并交给 AI 重跑。
  7. 走一遍修复流程：`fix prepare/submit`，由另一会话运行 `run prepare --fix`，然后 `fix close`。
  8. 浏览器崩溃：用 `kill -ABRT` 结束 CfT 进程，相关场景记为受阻，并写明信号和日志位置。
  9. 检查整个过程的会话记录：会话没有询问过“确认人是谁”。
- **独立验收**：见阶段 8。没有真实入口证据或独立执行的部分，如实报告为未完成；回归测试建设与实际运行情况单独报告。

## 风险与说明

- **不兼容**：v10 不读旧数据目录。cw2 需要重新 `init` 新项目，旧结果和证据只能在旧目录与 Git 历史中查阅。cw2 有轮次在运行时不升级已安装的工具，到时把升级命令交给用户执行。
- **Paseo 下的崩溃对话框**：chan-product 的 WebKit 崩溃不在本仓范围，完成后单独告知用户（例如在 agent 场景下停用它的 `iphone-webkit` project）。
- **守护进程可以在 agent 会话中启动**：浏览器缺省无头以避开窗口注册失败，启动崩溃会如实记受阻；`status` 会显示守护进程是否在运行，并给出启动命令。
- **工作量**：约 9k 行新代码、技能与文档全部重写，按阶段推进；每个阶段结束先自检，再进入下一阶段。
