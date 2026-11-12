
## 5. 下单与库存（库存演示项目）

库存演示项目多一个 stock 仓库：库存服务（`stocksvc/server.py`），提供库存查询与扣减。看板守护进程按 `config.yaml` 的 `adapter.services` 管理两个服务：

| 服务 | 业务说明 | 可停 |
| --- | --- | --- |
| shop | 商城网页与接口 | 否 |
| stock | 库存查询与扣减 | 是 |

- **就绪**：`env.py` 先启动库存服务（第二个端口），再启动商城并传入库存服务地址；就绪行另给出 `STOCK_URL`。本检出没有登记 stock 仓库时，stock 上报“本机未搭建”，商城照常启动，下单返回降级提示。各服务状态用 `sop-board env status --json` 查看。
- **api 入口的下单接口**：`POST $APP_URL/api/purchases {order_id, sku, qty, paid}`；`GET $APP_URL/api/orders?id=<订单号>` 查订单；`GET $APP_URL/api/stock/<商品>` 查库存（演示商品 DEMO-1、DEMO-2，启动时补足 100 件）。订单号带运行编号前缀。
- **库存查询不可用的场景**：起点写“库存查询不可用”。执行时用 `sop-board env stop stock` 停掉库存服务（只在运行中可用），执行完 `sop-board env start stock`，或交给提交时自动恢复。不直接结束进程，不改配置或代码。
- **SQLite**：库存在 `SOP_ENV_LOG_DIR/stock.sqlite3`，订单在 `shop.sqlite3`（表 `orders`、`order_items`）；日志在 `stock.log`、`shop.log`。
