# 演示商城真实验证手册

## 1. 准备与就绪

看板守护进程按检出运行数据目录的 `env.py`：启动 `shop/server.py`，端口 = 基础端口 + `SOP_PORT_OFFSET`，SQLite 在 `SOP_ENV_LOG_DIR/shop.sqlite3`，就绪后输出 `APP_URL` 与 `WEB_URL`。

## 2. 入口

- **api**：用 curl 调用 `APP_URL` 下的 `/api/orders`、`/api/refunds`。测试订单用数据配方 `seed-order`，或 `POST /api/orders {id, paid, delivered_days_ago}` 准备（只准备无关前置，不准备退款本身）。
- **web**：经看板浏览器（身份 merchant）打开 `WEB_URL`。合成账号 merchant 的密码在环境变量 `DEMO_MERCHANT_PASSWORD`；它是声明的测试账号，执行者直接登录。

商家的登录会话保存在同一 SQLite 的 `sessions` 表中，商城进程重启后原 cookie 继续有效。登录 cookie 不带到期时间，浏览器进程结束后失效。`POST /logout` 删除当前会话并清除 cookie；之后访问 `/orders` 跳转 `/login`。登录次数仍由看板 `identity record` 记录，重启不另记登录。

## 3. 观察与证据

接口保存请求与完整响应；页面保存截图；需要时只读查询 SQLite（`sqlite3 <db> 'select * from refunds'`）。

## 4. 隔离与清理

每个检出一份 SQLite；订单编号带运行编号前缀避免冲突。清理只删除本轮创建的订单与退款。
