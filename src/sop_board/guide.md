# 为项目搭建 Flow Board

这份指引随 Flow Board（命令 `sop-board`）发行，Codex 与 Claude Code 共用，在开发者自己打开的会话里完成搭建。业务验收标准以 [docs/standard.md](https://github.com/AdolphKevin/sop-board/blob/main/docs/standard.md) 为准；凭据只经环境变量传递，配置、手册与报告只写变量名。

## 1. 安装与登记

```sh
uv tool install <看板的本地检出或发行来源>
sop-board skills install          # 把 sop-board、playwright-acceptance、regression-distill 链接到两个客户端
sop-board browser install         # 固定版本的 Playwright 与 Chrome for Testing，装在 ~/.sop-board/runtime
sop-board init <项目> <仓库目录>... [--data <根目录>]
```

询问开发者项目名称和全部 Git 仓库目录（可分散在任何位置）。init 逐个核对工作树根目录与 origin，全部合格才建立 `<根目录>/<项目>/`（缺省根目录 `~/.sop-board`），其中有 `project.json`、`config.yaml`、`board.sqlite3`。旧版数据目录直接拒绝，不迁移。本机操作者缺省取 `git config user.name`，需要时用 `sop-board operator set <姓名>` 设置一次，之后所有登记自动以它署名。

当前会话不能写数据目录时，Codex 用 `--add-dir <数据目录>`，Claude Code 用 `/add-dir <数据目录>`。

## 2. 查清实际环境并写真实验证手册

从仓库、启动配置、文档和开发者说明查清服务、依赖、启动顺序、端口与入口地址，用实际监听端口和服务响应交叉核对。按标准第 2、3 节把事实写进产品仓库的真实验证手册（如 `docs/operations/verification.md`）：环境与依赖、可重复数据、执行与观察、证据、清理、账号类别。手册在有产品仓库写权限的会话中编写。

## 3. 写 config.yaml

```yaml
repos:                      # 别名 → main 检出的绝对路径（init 已写好）
  app: /absolute/app
  lib: /another/lib
identities:                 # 别名 → 规范化的 origin 身份（init 已写好，origin 变了即不合格）
  app: github.com/acme/app
  lib: github.com/acme/lib
scenarios: {repo: app, path: docs/sop}     # 场景文件、业务 SOP 与 regression.yaml 所在目录
skill: project-verification                 # 数据目录 skills/<名称>/SKILL.md，每个入口一个二级标题
entries:
  api:
    surface: http
    address_env: APP_URL
    operate: "按 app 仓库 docs/operations/verification.md 第 2 节 api 入口操作"
    observe: ["按同手册第 3 节核对响应、数据和禁止副作用"]
    evidence: ["按同手册第 3 节保存请求响应与数据查询"]
    isolation: "按同手册第 4 节准备隔离数据，留证后清理"
    credential_env: [APP_TOKEN]             # 可选：只写变量名
  web:
    surface: web
    address_env: WEB_URL
    operate: "按同手册第 2 节 web 入口操作"
    observe: ["按同手册第 3 节核对页面与数据"]
    evidence: ["截图与请求记录"]
    isolation: "按同手册第 4 节"
    identities: [{id: merchant, label: 商家, limits: {logins_per_day: 5}}]   # 可选：每个身份一份浏览器 profile；real: true 表示真实账号
    spec_library: app:acceptance            # 可选：浏览器层守护 spec 与交互地图的仓内目录
    atlas: {repos: [app], languages: [zh]}  # 可选：交互地图
    web_evidence: {video: true, trace: true}
adapter:
  env: [python3, env.py]                    # 必填：按检出启动环境（相对数据目录的脚本）
  ports: [8000, 3000]                       # 环境监听的基础端口
  port_step: 100                            # 可选：每个检出的端口偏移步长；0（缺省）表示各检出共用端口、轮流使用
  worktree: [python3, worktree.py, create]  # 可选：sop-board checkout create 用它建立多仓 worktree
  cleanup: [python3, worktree.py, cleanup]
  ready_timeout: 180
snapshot_exclude: {app: [docs/test-cases/, tests/]}   # 可选：不影响产品运行的材料
agents: {run: {runner: claude, model: sonnet}, default: {runner: codex}}   # 可选：看板启动无头会话用哪个代理
fixtures:                                   # 可选：前置数据配方
  environment: 本机测试库
  scope: [测试订单]
  recipes:
    - {name: seed-order, title: 准备测试订单, params: {order_id: 订单号}, creates: [订单], run: [python3, seed.py]}
```

`snapshot_exclude` 只放不参与产品运行、但会写进仓库的文件，例如被测服务生成的缓存或日志：它们不计入产品快照，环境不会因此重启，守护运行也不会因“运行前后快照变化”而不通过。守护测试文件会自动排除。

## 4. 写适配

`adapter.env` 由看板守护进程按检出在前台运行，结束时收到 SIGTERM（整个进程组），须停止自己启动的服务。输入变量：

| 变量 | 含义 |
| --- | --- |
| `SOP_CHECKOUT` | 检出名称（main 或 worktree 名） |
| `SOP_REPO_<别名>` | 本检出各仓库目录（别名大写，`-` 换成 `_`） |
| `SOP_BASE_<别名>` | 登记的 main 仓库目录 |
| `SOP_PORT_OFFSET`、`SOP_PORTS` | 端口偏移，以及偏移后的端口 JSON 数组 |
| `SOP_ENV_LOG_DIR` | 本检出的日志目录 |

服务就绪后输出一行 `sop-board: ready`，可在同一行附 JSON 给出本检出的入口地址：`sop-board: ready {"APP_URL": "http://127.0.0.1:8100"}`。项目的服务无法换端口时保持 `port_step: 0`，看板会让检出轮流使用同一组端口，冲突时直接报告占用者。

`adapter.worktree` / `cleanup`（可选，须同时声明）沿用既有契约：输入 `SOP_TASK_DIR`、`SOP_FIX_BRANCH`、`SOP_REPOS`（逗号分隔）、`SOP_BASE_<别名>`，可选 `SOP_BASE_COMMITS`，清理时另有 `SOP_WORKTREES`。worktree 最后一行输出 `{"repos": {别名: 路径}, "env": {变量: 值}}`。不声明时用 `git worktree add`。开发者自己建的 worktree 不需要适配，在其中运行命令即自动识别为检出。

## 5. 写项目验证技能

数据目录 `skills/<skill>/SKILL.md`：文件头有 name、description；每个入口一个含入口名的二级标题，写明执行前读哪份手册的哪几节、本机参数怎样取得、声明了哪些测试账号与凭据变量。Web 入口缺省使用随包 playwright-acceptance。

## 6. 启动与核对

```sh
sop-board serve <项目> [--lan]   # 守护进程：网页、按检出的环境、浏览器、无头会话
sop-board check                  # 配置、当前检出的环境就绪、浏览器运行时
sop-board status
```

`--lan` 让测试同学从内网访问工作台：他们能查看、复测标注、答复问题、批注草稿、同意草稿场景或撤销自己的同意、上传用例或 SOP；发起执行、守护与沉淀只在本机。草稿在工作台“草稿”筛选中审阅，每条场景都在当前内容上被同意、批注都处理后，会话运行 `sop-board draft confirm <文件>`，确认人是同意人。

向开发者报告：已验证的事实、推断及依据、待回答的问题、适配依赖的外部工具、验证技能摘要。开发者确认后开始使用。

## 产品仓库约定（写进产品仓库的 AGENTS.md）

> 新需求写 SOP 或场景前，先核对已有 SOP 与场景的业务交叉，预期随之改变的已有场景一并修改。宣布可以验收之前，运行 `sop-board status`，说明待复测、失败与未执行的场景数，由独立会话经真实入口执行（`sop-board run launch`）；有沉淀的守护时先运行 `sop-board retest` 检查代码回归。场景文件在 `docs/sop/`，改场景要经使用者确认；登记由本机操作者自动署名，不必询问确认人。
