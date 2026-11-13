# 本仓真实验证手册：Flow Board

本页说明怎样经真实入口验收 `docs/sop/` 中的现行场景集。验收标准以 [docs/standard.md](standard.md) 为准，设计依据是 [docs/design.md](design.md)。预期来自已确认的设计与场景，不来自当前实现的输出。实现者自检只用于交付准备，独立验收由未参与实现的执行者完成；两者分开留证，过程材料与证据都放在仓库外。

## 环境

- Python 3.10+、uv、Git、`lsof`、支持 POSIX 进程组的本机环境；Node.js（浏览器运行时用）。
- 被测版本用 `uv tool install <本仓检出>` 安装，或在检出目录用 `uv run sop-board …`。
- 每轮使用新的临时数据目录（`--data`）、新的演示仓库和空闲端口；留证后停止 `sop-board serve` 及其环境和浏览器，再删除临时目录。
- 看板守护进程可以在用户终端或 agent 会话中启动（已定决策 12）；浏览器只由守护进程启动、缺省无头。

## 演示项目

场景集里的“演示项目”都由 `examples/demo-shop/` 生成：app（标准库 HTTP + SQLite 的退款接口与商家订单页）与 lib（退款规则）两个 Git 仓库，各有 origin 远端地址。每轮验收在新的临时目录生成：

```sh
python3 examples/demo-shop/make_demo.py $T              # 只生成 $T/app、$T/lib（SET-01-P01 由执行者自己 init）
python3 examples/demo-shop/make_demo.py $T --data $D    # 另外 init 为项目 demo，并把 data/ 模板装进 $D/demo
```

库存演示项目（environment.yaml 文件头，已定决策 22）用 `--with-stock` 生成第三个仓库 stock（库存服务），“只搭商城”用同一份生成结果另登记一个只含 app、lib 的项目：

```sh
python3 examples/demo-shop/make_demo.py $G --with-stock --data $S                     # app、lib、stock 都登记
python3 examples/demo-shop/make_demo.py $G --with-stock --reuse --shop-only --data $T  # 只搭商城：只登记 app、lib
```

两个数据目录登记了同一组仓库，命令都带 `--data`（`-p demo`）指定项目；两者要同时运行环境时，把其中一个 `config.yaml` 的 `adapter.ports` 改开，避免端口租约冲突。

`--data` 时脚本合并 init 写入的 `repos`、`identities` 与 `data/config.yaml`（库存演示项目用 `with-stock/config.yaml`），复制 `env.py`、`worktree.py`、`seed.py` 与验证技能 `skills/demo-verification/`；被测版本不在 PATH 上时加 `--sop-board "uv run --project <本仓检出> sop-board"`。启动看板守护进程时在同一环境设置凭据变量：`DEMO_SEED_TOKEN`（任意值，配方的写库凭据）与 `DEMO_MERCHANT_PASSWORD`（缺省 merchant-pass）与 `DEMO_MERCHANT2_PASSWORD`（缺省 merchant2-pass）。验证运行时安装或缺失（BRW-01-P01、SET-05-N01）时，看板与 CLI 都设同一个临时 `SOP_BOARD_HOME`。

### 双机项目（已定决策 21）

setup.yaml 文件头的“双机项目”在一台机器上模拟两台：一个 docker 起的 PostgreSQL、两个 `SOP_BOARD_HOME`（各自的数据根目录、`machine.json` 与 `evidence/`）、同一份演示仓库的两个 clone。只操作为验收新建的容器与卷，用不常见的端口和带前缀的容器名，结束时删除。

```sh
# 共享库：SET-06/07 用 PGPASSFILE；SET-09 经设置接口保存到本机 config.yaml（权限 600）
docker run -d --name sbv-pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=sopboard -e POSTGRES_DB=sopboard \
  -e POSTGRES_PASSWORD="$PW" -v sbv-pgdata:/var/lib/postgresql/data postgres:16
printf '127.0.0.1:55432:*:sopboard:%s\n' "$PW" > $T/pgpass && chmod 600 $T/pgpass && export PGPASSFILE=$T/pgpass
# 机器 A（操作者 林甲）与机器 B（操作者 周乙）
SOP_BOARD_HOME=$HA sop-board operator set 林甲
SOP_BOARD_HOME=$HB sop-board operator set 周乙
SOP_BOARD_HOME=$HA python3 examples/demo-shop/make_demo.py $TA --data $HA --shared-database 127.0.0.1:55432/sopboard/sopboard
SOP_BOARD_HOME=$HB python3 examples/demo-shop/make_demo.py $TB --clone-of $TA --project shop-b --ports 28300 --data $HB \
  --shared-database 127.0.0.1:55432/sopboard/sopboard
SOP_BOARD_HOME=$HA sop-board init demo --new            # 在 $TA/app 运行；输出产品 ID
SOP_BOARD_HOME=$HB sop-board init shop-b --join <ID>     # 在 $TB/app 运行
SOP_BOARD_HOME=$HA sop-board serve demo --port <端口A> --lan
SOP_BOARD_HOME=$HB sop-board serve shop-b --port <端口B> --lan
```

- `--clone-of` 让 B 的仓库从 A 的仓库 clone：origin 保持演示地址（登记时核对的仓库身份），另有远端 `peer` 指向 A 的仓库；“A 提交并推送，B 拉取”就是在 A 提交、在 B 的仓库 `git pull peer main`。
- 两边的环境同时运行时用 `--ports` 错开 `adapter.ports`。机器显示名在第一次需要时生成为“操作者@主机名”，所以先设操作者再登记。
- 两个数据根目录都没有浏览器运行时：需要浏览器时在各自的数据根目录放一个指向本机 `~/.sop-board/runtime` 的符号链接，只读使用。两个看板的 main 检出（以及本机正在用的看板）的缺省调试端口相同，后启动的浏览器自动顺延到下一个空闲端口（已定决策 24），不用再把 Web 步骤挪到别的检出；实际端口看 `browser status` 与 `run.env`。
- “内网设备”在同一台机器上用 `--lan` 打印的实际非回环地址访问代替，记录里写明是替代做法。
- 共享库的只读查询：`psql -h 127.0.0.1 -p 55432 -U sopboard -d sopboard -c "…"`；停启共享库：`docker stop|start sbv-pg`；改表结构版本（SET-07-N02）：`UPDATE meta SET value='8' WHERE key='format'`。

场景集中的简写与演示项目的对应：

| 场景集写法 | 演示项目中 |
| --- | --- |
| `shop#X` | `shop#SHOP-R1-N01`：签收 8 天的订单申请退款应返回 422；预埋缺陷让它返回 201，场景失败 |
| `shop#A` | `shop#SHOP-R1-P01`：签收 3 天的订单退款成功，金额等于实付 |
| `shop#B` | `shop#SHOP-R1-N02`：已退款订单再次申请返回 409 |
| `shop#W` | `shop#SHOP-W1-P01`：商家在订单页看到退款状态；Web 入口，身份 merchant |
| 预埋缺陷与修复 | lib `refundlib.can_apply` 写成 `<= APPLY_DAYS + 1`；修复为 `<= APPLY_DAYS` |
| `draft-a`、`draft-b` | app `docs/sop/` 下 `metadata.status: draft` 的两份场景文件，各一个节点（`DRAFT-A-1`、`DRAFT-B-1`）、两条场景（P01 正向、N01 反向），每条场景的 `refs` 为 `refund.md#R2` |
| 演示 PRD | `examples/demo-shop/materials/prd-partial-refund.md`（部分退款需求） |
| 业务 SOP 与待确认问题（AUT-03） | app `docs/sop/refund.md`（含段落内换行与 Mermaid 流程图）与 `docs/sop/refund-待确认问题.md`（Q1 已答复，Q2、Q3 待答复） |
| 演示用例文件 | `materials/cases.yaml`（一条与 shop 对应的细节用例、一条新流程用例）；`materials/cases-invalid.yaml` 缺 expected_results |
| 数据配方 | `seed-order`：参数 `order_id`、`paid`、`days`，写入本检出的 `$D/demo/env/<检出>/shop.sqlite3`；输出含凭据取值，用来核对留痕遮蔽 |
| 守护命令 | app `docs/sop/regression.yaml` 的 `unit`：`python3 tests/junit_runner.py --junit {junit}`（unittest，测试放 `tests/test_*.py`） |
| 端口 | 基础端口 28080，`port_step` 100：main 用 28080，槽位 1 的检出用 28180；`APP_URL` 与 `WEB_URL` 取自 ready 行 |
| 浏览器端口 | 身份浏览器每个身份一个、各检出共用，缺省调试端口是 `9400 + 身份序号`，不分项目：merchant 缺省用 9401，merchant2 用 9402（已定决策 28）。缺省端口被别的进程（例如使用者自己的看板的浏览器）占用时顺延到下一个空闲端口，实际端口见 `browser status` 的 `port`（`default_port` 是缺省端口）与 `run.env` 的 `PW_CDP_ENDPOINT`。BRW-02-N01 的占用进程：本机已有进程监听 9401 时直接用它并记下进程号；没有时用一个与项目无关的占位进程，例如 `python3 -c 'import socket,time; s=socket.socket(); s.bind(("127.0.0.1", 9401)); s.listen(); time.sleep(3600)'`，验收后结束它 |
| 合成账号 | merchant，密码取 `DEMO_MERCHANT_PASSWORD`；merchant2（第二个商家账号，也能执行 `shop#W`，决策 28 的场景用），密码取 `DEMO_MERCHANT2_PASSWORD`；限额都是 每日登录 2 次、每日验证码 2 次 |
| 静态脚本缓存（BRW-02-P02） | 商城首页引用的静态脚本响应带 `Cache-Control: max-age=86400`，首页显示脚本里的标记文字；改这份脚本即可核对换检出时是否清空了 HTTP 缓存 |
| 环境日志 | `env.py` 把收到的 `SOP_*` 变量逐行写进 `$D/demo/env/<检出>/<序号>.log`，并存 `received.json`（变量名中的别名为大写，如 `SOP_REPO_APP`）；商城与库存服务的输出在同目录 `shop.log`、`stock.log` |
| `stock#K` | 库存演示项目 `stock#STOCK-O1-P01`：库存可用时 `POST /api/purchases` 下单成功（201），订单生成，DEMO-1 库存减 1 |
| `stock#S` | 库存演示项目 `stock#STOCK-O1-N01`：起点“库存查询不可用”，下单返回 503 与“库存服务暂不可用，请稍后再试”，不生成订单 |
| 服务声明（库存演示项目） | `adapter.services`：`shop`（商城网页与接口，不可停）、`stock`（库存查询与扣减，可停）；`adapter.ports` 为 `[28080, 28090]`，库存服务用第二个端口，就绪行另给出 `STOCK_URL`。状态文件在 `$S/demo/env/<检出>/services/<名称>.state`，停止请求是同目录的 `stock.request`。只搭商城时 stock 上报 `absent`（本机未搭建）。基础演示项目没有 `adapter.services` |
| 验收开关 | 取自启动守护进程时的环境：`DEMO_STOCK_IGNORE_STOP=1` 时 env.py 忽略对 stock 的停止请求（ENV-06-N03）；`DEMO_STOCK_CRASH=1` 时库存服务启动后立即异常退出（RUN-05-N02）。开关在 `sop-board serve` 启动前设置，改开关须重启守护进程 |
| 结束库存服务进程 | ENV-05-N01、RUN-05-N01、GRD-03-N03 用 `kill -9` 结束 stocksvc 进程：`pgrep -f "stocksvc/server.py --port <端口>"` 取进程号 |
| 守护命令 `api`（库存演示项目） | app `docs/sop/regression.yaml` 另声明 `api`：`python3 tests/junit_runner.py --junit {junit} --pattern api_*.py`，测试经运行中的商城接口（读 `APP_URL`），依赖库存服务；`unit` 运行 `tests/test_*.py` |
| 停启记录 | 运行中的停启记录在本机 `board.sqlite3` 的 `service_events` 表（只读查询 `SELECT * FROM service_events WHERE work=<本机运行编号>`），提交后写进运行摘要（本机 `local_work.result`）和业务记录库 `work.services`（`SELECT services FROM work WHERE local_id=<运行编号>`） |
| 旧版数据目录（SET-01-N01） | 在 tag v9-final 的检出运行 `uv run --project <该检出> sop-board init old $T/app --data $D/old` |

## 入口

场景的 `verify` 引用以下入口：

| 入口 | 操作 | 观察 | 证据 |
| --- | --- | --- | --- |
| `cli` | 在临时项目中运行安装后的 `sop-board` 命令 | 标准输出、错误输出、退出码、监听地址与端口、数据目录与产品仓库中的文件 | 命令原文、完整输出、目录清单与文件摘要 |
| `api` | 用 curl 调用运行中的看板，分别从本机地址和 `--lan` 打印的实际非回环地址发出（地址变量 `SOP_BOARD_URL`） | 状态码、响应正文、`board.sqlite3` 中的相关行、证据目录 | 请求与响应原文、只读 SQLite 查询与输出 |
| `web` | 用浏览器打开看板并按场景操作；内网访问用另一台机器上的浏览器（地址变量 `SOP_BOARD_URL`） | 页面显示的数值、列表与提示，浏览器发出的请求，数据库中的相关行 | 截图、页面文本、请求记录、只读查询与输出 |
| `agent` | 在临时项目中新开 Codex 或 Claude Code 会话，用自然语言提出请求，按需回答 | 会话记录、数据目录与产品仓库中的文件、命令输出、`git status` | 会话记录导出、生成的文件（凭据脱敏）、命令输出 |

## 留证与结论

- HTML/JSON 自检与浏览器操作分开留证；交验收前，回环地址和实际非回环地址都要冒烟。
- 每条场景记录执行者、实现者、产品快照与场景指纹，原始证据逐项对应 expected 与 forbidden_effects。
- 如实区分通过、失败、受阻、跳过、未执行；只通过了子集时，不写成整批完成。
- 真实模型、浏览器或沙箱外操作无法运行时，保留未执行范围；模拟材料逐项注明，不能当作独立验收。

## 设置接口的演示材料

只登记的演示项目：运行 `python3 examples/demo-shop/make_demo.py "$T/repos" --data "$D" --registered-only`。
生成器只用 init 写入 repos 和 identities，同时复制 env.py、worktree.py、seed.py 与 skills/；完整的候选配置参考 examples/demo-shop/data/config.yaml。
设置接口契约见 reference.md“设置接口”。

设置页接入前的双机：按上文生成 A、B，但两条生成命令都省略 --shared-database，并且不运行 init --new/--join。
两边配置合格但未绑定共享库。让两边所有命令及看板的 PGPASSFILE 指向同一个空文件（权限 600）。
另起空 PostgreSQL；密码随机生成，只存临时文件，通过容器的 POSTGRES_PASSWORD_FILE 读入，不把取值放进命令行或证据。
测试者从临时文件取得密码，经共享库测试和接入接口提交；接口响应与截图中核对密码不出现。
验收者自行选择未被占用的容器名、卷名、数据库端口、看板端口与环境端口；结束时只清理自己创建的资源。

本机配置与 config.yaml.prev 含密码时为 600；不读取或修改 ~/.pgpass。
浏览器操作、真实 HTTP 自检与独立验收分别留证，按实际执行入口和执行者报告覆盖范围。

### SET-08：从只登记到配置合格

用上面的 `--registered-only` 材料启动 `serve demo --data "$D" --lan`，启动环境设好 DEMO_SEED_TOKEN、DEMO_MERCHANT_PASSWORD。记录 PID、status 的 configured=false 与初始配置摘要。浏览器经看板守护进程提供的 Chrome for Testing 操作，按随包 playwright-acceptance 连接运行目录 run.env 的地址。

从本机工作台的配置提示进入设置页，按 examples/demo-shop/data/config.yaml 逐节填场景目录与排除项、验证入口全部字段及验证技能、环境命令、前置数据与缺省代理。每节保存前后保留截图及不含凭据取值的请求、响应；数组一行一项、命令一行一个参数。核对每次只改变这一节、问题数归零、其他节尚有问题仍可保存；凭据变量名称、来源与是否已设置可见。保存后把配置解析成对象逐项对照演示模板（端口如为隔离而调整，单列差异与原因），核对 prev 为上次保存前内容。最后保存 status、check，确认配置与环境两项、工作台场景清单及 PID 未变。

内网权限用 --lan 实际打印的非回环地址：浏览器截图顶栏没有设置，页面直访与 GET/POST 设置接口均为 403 local-only，不带配置；另存跨源本机 POST 的 cross-origin 响应和配置摘要不变的证据。陈旧页面用编辑器改临时项目的 ready_timeout 后从旧页面保存，保存 settings-stale 截图与响应；重新载入后保存，核对编辑器的改动保留。无效字段、引用与占用按 SET-08 其余场景原文执行，不能以这些主线代替全组。

### SET-09：页面接入、断开与重连

使用上述“设置页接入前的双机”材料，两个数据根目录、空 PostgreSQL、空 PGPASSFILE；密码只在临时文件中生成，通过密码框填写。密码填写和 POST 正文不录入 trace 或请求证据；截图中密码框只显示遮罩，保存后为空且仅写“已保存”。截图记录 A 的空产品清单与新建、B 的产品统计和两条绑定提示、加入后的产品名称与 ID。A 经演示真实入口产生并提交结果，B 从工作台历史核对产出机器；实现者自检如实登记，独立验收另由未参与实现者执行。

断开前给 B 留一条本机标注，接入后再产生共享标注，记录共享库各表行数。保存断开确认框的四条后果、断开及重连后的 status、配置与产品绑定（密码项遮蔽），核对本机旧结果重现、共享结果恢复且表行数不变。停掉本轮 PostgreSQL 前经真实 fix prepare 留一个本机修复；停库后设置读取应在 notices 提示无法确认修复结束，occupants 不包含该未知修复。可将自己创建的同一数据卷在另一空闲端口启动，随后从设置页改为可用连接，核对不因旧连接下未知修复而锁住；确认为 open 的修复仍须锁住。不要停用外部数据库。

分别收集页面源码、设置响应、status/init 输出、serve 启动输出、内网工作台 HTML 与 /api/board，程序从临时文件读密码，逐文件搜索只输出命中文件与计数，不输出密码。另核对测试账号凭据取值。含密码的原始请求、配置、浏览器追踪材料不放进证据包；保留脱敏副本、权限、摘要、截图清单、操作脚本与运行记录。真实 Codex/Claude 会话引导属于 SET-09-P04，未新开客户端执行时明确记未执行。
