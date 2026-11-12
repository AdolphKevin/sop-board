# 本仓真实验证手册：Flow Board

本页说明怎样经真实入口验收 `docs/sop/` 中的现行场景集。验收标准以 [docs/standard.md](standard.md) 为准，设计依据是 [docs/design.md](design.md)。预期来自已确认的设计与场景，不来自当前实现的输出。实现者自检只用于交付准备，独立验收由未参与实现的执行者完成；两者分开留证，过程材料与证据都放在仓库外。

## 环境

- Python 3.10+、uv、Git、`lsof`、支持 POSIX 进程组的本机环境；Node.js（浏览器运行时用）。
- 被测版本用 `uv tool install <本仓检出>` 安装，或在检出目录用 `uv run sop-board …`。
- 每轮使用新的临时数据目录（`--data`）、新的演示仓库和空闲端口；留证后停止 `sop-board serve` 及其环境和浏览器，再删除临时目录。
- 看板守护进程在用户终端里启动，不由 agent 会话启动。

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
| 浏览器端口 | 身份浏览器的调试端口是 `9400 + 槽位×20 + 身份序号`，不分项目：main 的 merchant 用 9401，槽位 1 用 9421。本机同时开着别的看板（例如使用者自己的项目）时，相同槽位的同一身份会冲突，`run prepare` 报“调试端口已被其他进程占用”；这时 Web 场景改在没有冲突的 worktree 检出执行并在记录中注明，或先在占用的项目运行 `sop-board browser close` |
| 合成账号 | merchant，密码取 `DEMO_MERCHANT_PASSWORD`；限额 每日登录 2 次、每日验证码 2 次 |
| 环境日志 | `env.py` 把收到的 `SOP_*` 变量逐行写进 `$D/demo/env/<检出>/<序号>.log`，并存 `received.json`（变量名中的别名为大写，如 `SOP_REPO_APP`）；商城与库存服务的输出在同目录 `shop.log`、`stock.log` |
| `stock#K` | 库存演示项目 `stock#STOCK-O1-P01`：库存可用时 `POST /api/purchases` 下单成功（201），订单生成，DEMO-1 库存减 1 |
| `stock#S` | 库存演示项目 `stock#STOCK-O1-N01`：起点“库存查询不可用”，下单返回 503 与“库存服务暂不可用，请稍后再试”，不生成订单 |
| 服务声明（库存演示项目） | `adapter.services`：`shop`（商城网页与接口，不可停）、`stock`（库存查询与扣减，可停）；`adapter.ports` 为 `[28080, 28090]`，库存服务用第二个端口，就绪行另给出 `STOCK_URL`。状态文件在 `$S/demo/env/<检出>/services/<名称>.state`，停止请求是同目录的 `stock.request`。只搭商城时 stock 上报 `absent`（本机未搭建）。基础演示项目没有 `adapter.services` |
| 验收开关 | 取自启动守护进程时的环境：`DEMO_STOCK_IGNORE_STOP=1` 时 env.py 忽略对 stock 的停止请求（ENV-06-N03）；`DEMO_STOCK_CRASH=1` 时库存服务启动后立即异常退出（RUN-05-N02）。开关在 `sop-board serve` 启动前设置，改开关须重启守护进程 |
| 结束库存服务进程 | ENV-05-N01、RUN-05-N01、GRD-03-N03 用 `kill -9` 结束 stocksvc 进程：`pgrep -f "stocksvc/server.py --port <端口>"` 取进程号 |
| 守护命令 `api`（库存演示项目） | app `docs/sop/regression.yaml` 另声明 `api`：`python3 tests/junit_runner.py --junit {junit} --pattern api_*.py`，测试经运行中的商城接口（读 `APP_URL`），依赖库存服务；`unit` 运行 `tests/test_*.py` |
| 停启记录 | 运行的停启记录在 `board.sqlite3` 的 `service_events` 表（只读查询 `SELECT * FROM service_events WHERE work=<运行>`），提交后也写进 `work.result` 的 `service_events` |
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
