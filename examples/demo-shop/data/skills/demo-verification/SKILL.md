---
name: demo-verification
description: 演示商城（examples/demo-shop）的真实入口验证方法：api 入口用 curl 调接口，web 入口经看板浏览器以 merchant 身份打开订单页。
---

# 演示商城验证

执行前读 app 仓库 `docs/operations/verification.md`。入口地址取运行目录 `run.env` 中的 `APP_URL`、`WEB_URL`，它们来自本检出环境的就绪输出，每个检出端口不同。

## api 入口

1. 按手册第 2 节准备订单：配方 `sop-board fixtures run seed-order --run <运行> --param order_id=<运行编号>-<字母> --param paid=<金额> --param days=<天数>`，或 `POST $APP_URL/api/orders`。退款本身属于被测行为，经 `POST $APP_URL/api/refunds` 产生。
2. 按场景步骤用 curl 调用，保存请求与完整响应（`curl -sS -i`）。
3. 按手册第 3 节只读查询本检出的 SQLite 核对退款记录与禁止副作用。

## web 入口

1. 用随包 playwright-acceptance 技能，经 `PW_CDP_ENDPOINT` 连接看板为身份 merchant 启动的 Chrome for Testing；执行者不自行启动浏览器。
2. 未登录时在登录页以 merchant 和环境变量 `DEMO_MERCHANT_PASSWORD` 的取值登录；这是声明的演示合成账号，直接登录，不另要授权。登录前运行 `sop-board identity record --identity merchant --kind login`。
3. 订单页 `$WEB_URL/orders` 截图并保存页面文本。
