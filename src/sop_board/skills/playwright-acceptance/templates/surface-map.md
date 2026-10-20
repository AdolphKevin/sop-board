# 界面地图：<项目名>

记录每个操作会打开哪一层界面、怎么识别。放在业务仓库的验收目录，随产品改版更新；写场景和写脚本的人共用。

## 入口

| 入口名（`verify`） | 地址 | 登录方式 | 隔离与清理 |
| --- | --- | --- | --- |
| `shopify-admin` | `https://admin.shopify.com/store/<开发店铺>/apps/<应用>/` | 用户在 Chrome 测试 profile 登录 | 只用测试客户与测试订单；清理脚本 `<路径>` |

## 层次

"依据"列写源码位置；没有源码时写"未核对源码，探测于 <日期>"，产品改版后重新探测。

| 界面 | 识别方式 | 由什么操作打开 | 依据 |
| --- | --- | --- | --- |
| 应用主体 | `iframe[name="app-iframe"]` | 进入应用 | App Bridge 嵌入 |
| 收件箱弹窗 | `iframe[name^="frame://<应用句柄>/modal/"][src*="/inbox/chat"]`（排除 0×0 预加载副本） | 首页 "Go to inbox"、左侧 "Inbox" | `src/features/chat/Chat.tsx` 的 `shopify.modal.show('chat-modal')` |
| 主题编辑器 | 新标签页，URL 含 `/themes/…/editor?context=apps` | 首页 "Activate Now" | 未核对源码，探测于 2026-10-19 |

## 组件备注

| 组件 | 定位方式 | 说明 |
| --- | --- | --- |
| 会话列表项 | `.ReactVirtualized__Grid__innerScrollContainer > div` | 虚拟列表，无可访问角色；建议补 `data-testid` |
| 消息输入框 | `[contenteditable="true"]` | Lexical 编辑器；回车行为随用户设置变化，发送用按钮 |
| 发送按钮 | `Send` / `Send email` | 客户离线或邮件会话时为 `Send email`（`replyChannel.ts`） |

## 已知遮挡

| 遮挡 | 识别 | 处理 |
| --- | --- | --- |
| 应用嵌入未启用的提示条 | 文字 "Activate Chat on Your Store" | 与业务无关，可忽略；不要点击 |
| Shopify 人机验证 | 文字 "Your connection needs to be verified" | 受阻，等用户处理 |

## 多语言

界面文案来源：`src/i18n/locales/en.json`；没有源码时来源写"aria 快照，探测于 <日期>"。测试账号语言固定为英文。

## 步骤与业务入口映射

| 原场景编号/步骤 | 可见控件、iframe 与定位器 | 查询/写入接口及副作用观察点 | 依据 |
| --- | --- | --- | --- |
| `<编号>/<原步骤>` | `<只读探测后的定位>` | `<按业务语义区分查询与写入，POST 也可能是查询>` | `<源码或探测日期>` |

批次及场景指纹保存在仓库外 scope.json，不在此处重新生成场景编号。
