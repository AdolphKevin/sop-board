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

`--data` 时脚本合并 init 写入的 `repos`、`identities` 与 `data/config.yaml`（库存演示项目用 `with-stock/config.yaml`），复制 `env.py`、`worktree.py`、`seed.py` 与验证技能 `skills/demo-verification/`；被测版本不在 PATH 上时加 `--sop-board "uv run --project <本仓检出> sop-board"`。启动看板守护进程时在同一环境设置凭据变量：`DEMO_SEED_TOKEN`（任意值，配方的写库凭据）与 `DEMO_MERCHANT_PASSWORD`（缺省 merchant-pass）。验证运行时安装或缺失（BRW-01-P01、SET-05-N01）时，看板与 CLI 都设同一个临时 `SOP_BOARD_HOME`。

### 双机项目（已定决策 21）

setup.yaml 文件头的“双机项目”在一台机器上模拟两台：一个 docker 起的 PostgreSQL、两个 `SOP_BOARD_HOME`（各自的数据根目录、`machine.json` 与 `evidence/`）、同一份演示仓库的两个 clone。只操作为验收新建的容器与卷，用不常见的端口和带前缀的容器名，结束时删除。

```sh
# 共享库：密码只放 PGPASSFILE 指向的文件，不写进任何配置
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
| 浏览器端口 | 身份浏览器的缺省调试端口是 `9400 + 槽位×20 + 身份序号`，不分项目：main 的 merchant 缺省用 9401，槽位 1 用 9421。缺省端口被别的进程（例如使用者自己的看板的浏览器）占用时顺延到下一个空闲端口，实际端口见 `browser status` 的 `port`（`default_port` 是缺省端口）与 `run.env` 的 `PW_CDP_ENDPOINT`。BRW-02-N01 的占用进程：本机已有进程监听 9401 时直接用它并记下进程号；没有时用一个与项目无关的占位进程，例如 `python3 -c 'import socket,time; s=socket.socket(); s.bind(("127.0.0.1", 9401)); s.listen(); time.sleep(3600)'`，验收后结束它 |
| 合成账号 | merchant，密码取 `DEMO_MERCHANT_PASSWORD`；限额 每日登录 2 次、每日验证码 2 次 |
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
