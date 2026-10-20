# Shopify 参考

Shopify 项目与普通前端的流程相同，差别都在入口层。一个项目常有多个入口（后台内嵌应用、店面主题扩展、结账扩展、合作伙伴后台），各自在 `.tw/config.json` 的 `acceptance_entries` 里声明，场景用 `verify` 引用。

## 后台内嵌应用

- 应用内容在 `admin.shopify.com/store/<店铺>/apps/<应用>/…` 页面的 `iframe[name="app-iframe"]` 里：`const app = page.frameLocator('iframe[name="app-iframe"]')`。直接打开应用自己的域名会被重定向回后台，不要绕过 iframe。
- 后台导航与应用路由不同步：App Bridge modal 关闭后，`app-iframe` 的路由可能仍停在上一个页面；地址栏 URL 也可能滞后。判断"当前在哪个页面"只看页面内容。
- Polaris 的提示（toast）几秒内消失，断言它时用较短的等待并紧跟在动作之后；或改为断言持久的结果（列表项、状态标签）。
- 后台外层界面归 Shopify 维护，会不经通知改版。定位只在应用 iframe 内进行；必须操作后台外层时（例如主题编辑器）在界面地图中标明风险。

## App Bridge modal

`shopify.modal.show(id)` 打开的全屏弹窗是独立 iframe，不在 `app-iframe` 内：

- 名称形如 `frame://<应用句柄>/modal/<uuid>`，`src` 为应用页面地址。
- 页面上常同时存在一个 0×0 的预加载副本，按 `src` 路径区分：
  `page.frameLocator('iframe[name^="frame://<应用句柄>/modal/"][src*="/inbox/chat"]')`。
- 有源码时搜索 `shopify.modal.show`、`ui-modal` 确认哪些操作会打开 modal；没有源码时按[只读探测](web.md#只读探测)在操作前后对比 `page.frames()`，新出现且 `src` 指向业务路径的那个就是目标 modal。结论写进界面地图。

## 新标签页

"查看店铺"、主题编辑器、`activateAppId` 深链会在新标签页打开。点击前先 `const opened = page.context().waitForEvent('page')`，再在新页面上断言。夹具会为新标签页单独录屏。

## 人机验证与登录

- 短时间内反复打开主题编辑器等页面会触发 "Your connection needs to be verified before you can proceed"。遇到即抛 `受阻：Shopify 要求人机验证`，由用户在浏览器完成后重跑；不要重试或绕过。
- 真实账号（商家、合作伙伴、Google 登录）由用户在真实浏览器登录，脚本只复用会话。出现 "browser may not be secure" 立即停止。
- 开发店铺可在 `.tw/config.json` 的 `accounts` 声明为 `synthetic`，取得 `start --grant synthetic-login` 授权后才允许脚本登录。

## 数据与副作用

- 只在开发店铺操作。开测前核对当前标签页 URL 中的店铺句柄与场景 `initial_state` 一致，不一致即受阻。
- 订单、客户、metafield、webhook 这类副作用在界面上看不全，需要业务仓库提供 Admin API 只读查询脚本作为观察点；凭证走环境变量，不写进脚本。
- 客户在线与离线、会话由 AI 还是人工处理，都会改变同一按钮的文字与行为（例如 "Send" 与 "Send email"）。场景必须写明这些前置条件，脚本只按写明的分支执行，遇到另一分支即受阻。
- 测试店铺里的"测试客户"若填了真实邮箱，发消息会真的发邮件。`forbidden_effects` 要覆盖对外发送。

## 回归限制

依赖已登录后台的脚本只能在有人登录的本机运行。登记为回归检查时，`regression` 可用 `{check, cases}`，但项目验收方案要写明"仅本机执行，CI 不运行"，交付报告的 `automation` 按实际声明。
