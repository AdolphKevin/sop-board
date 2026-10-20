# 通用 Web 参考

## 浏览器与连接

夹具通过 CDP 接管已打开的 Chrome。连接地址按以下顺序确定：

1. 环境变量 `PW_CDP_ENDPOINT`（`ws://…` 或 `http://127.0.0.1:9222`）。
2. Chrome 用户目录下的 `DevToolsActivePort` 文件（macOS `~/Library/Application Support/Google/Chrome`，Windows `%LOCALAPPDATA%\Google\Chrome\User Data`，Linux `~/.config/google-chrome`；用 `PW_CHROME_USER_DATA` 指定其他目录）。该文件在 Chrome 打开远程调试后才存在：地址栏打开 `chrome://inspect/#remote-debugging`，启用 "Allow remote debugging for this browser instance"。

连上之后，脚本能看到这个 Chrome 里的全部标签页，trace 也会录到其他标签页的网络请求。因此：

- 用测试专用的 Chrome profile（Chrome 的"添加个人资料"），只在其中登录测试账号，并在该 profile 打开远程调试。
- `PW_TAB` 填目标标签页 URL 的子串，夹具只操作匹配的那一个标签页；匹配到零个或多个都报错。
- 运行期间不要操作该窗口；测试会把该标签页导航走。

预检先确认调试端口可达，再验证 CDP 初始化、唯一目标页和可选 `PW_FRAME`。端口不可达先核对进程、地址、沙箱权限；端口可达但初始化超时可能是授权等待、兼容性或其他标签页未响应，不能直接断定用户未同意。用户已确认授权时不反复要求同意。定位原因后只做一次有依据的重试。

大量休眠标签页时可设置 `PW_CDP_MODE=target`（Node >=22），使用公开 CDP transport 只向 Playwright 暴露唯一匹配页及它打开的弹窗/子 frame；不关闭其他标签页，也不绕过 Chrome 授权。默认 `browser` 模式保持兼容。`PW_CONNECT_TIMEOUT_MS` 默认 60000，`PW_FRAME` 为已确认的 iframe 选择器；预检不导航或点击。

## 界面层次

先确定目标在哪一层，再选定位方式。不要写通用的重试循环：Playwright 的定位器和断言本身会等待。

| 类型 | 识别 | 处理 |
| --- | --- | --- |
| 页面内固定 iframe | `iframe[name=…]` 或 `iframe[src*=…]` | `page.frameLocator(选择器)`，每次操作自动等待 iframe 出现 |
| 操作后出现的 iframe 弹窗 | 名称或 `src` 带稳定前缀；可能有隐藏的预加载副本 | 弹出后换用新的 `frameLocator`；按名称前缀加 `src` 路径识别，不按出现顺序 |
| 同一文档内的对话框 | `getByRole('dialog')` | 普通元素 |
| 新标签页 | 点击前 `context.waitForEvent('page')` | 单独操作与录屏；夹具会自动为新标签页录制 |
| 浏览器原生对话框 | `page.on('dialog')` | 事先注册处理 |
| 已知且与业务无关的遮挡（引导、Cookie 提示） | 项目在界面地图中声明 | `page.addLocatorHandler(定位器, 关闭动作)`，由 Playwright 在需要时触发 |
| 人机验证、登录过期、未知遮挡 | 页面文字 | 抛出 `受阻：` 错误，等人处理 |

接管已有页面时，跨域 iframe 要过一两秒才出现在 frame 树里；配置里的 `expect.timeout` 已放宽到 15 秒，不要再写轮询。

## 只读探测

黑盒验收方常常没有产品源码。此时界面结构只能从真实页面取，用临时脚本（放仓库外，用完删除）连上同一个 Chrome 做以下几件事，把结果写进界面地图：

- 列出 `context.pages()` 的 URL 和目标页的 `page.frames()`（name、url），确认 iframe 层次与预加载副本。
- 对页面和每个相关 frame 取 `locator('body').ariaSnapshot()`，从中抄下按钮、标题、占位文字的准确文案；没有多语言文件时，脚本里的文字以快照为准。
- 需要看到下一层界面时，只执行场景里已经写明且没有副作用的步骤（进入页面、打开列表或弹窗、选中查看）；不点发送、保存、接管、删除、支付等控件，不改任何设置。
- 探测时顺便记录加载态文字（如"… is loading"）和出现时间，供设置等待阈值。

探测结论只代表探测当天的界面。界面地图里对应条目的"依据"列写"未核对源码，探测于 <日期>"；产品改版或脚本定位失效时先重新探测，不要凭记忆改选择器。

## 定位

优先级：`getByRole` 带可见名称 → `getByLabel`、`getByPlaceholder`、`getByText` → `data-testid` → 第三方库的稳定类名（如 `.ReactVirtualized__Grid__innerScrollContainer > div`）。按钮、标签文字从产品多语言文件取，没有源码时从 aria 快照抄；脚本与场景使用同一种语言；测试账号的界面语言固定。

同一文字出现多处时加 `exact: true` 或限定容器，不要用 `.first()` 掩盖歧义，除非场景本身写的就是"第一项"。

## 步骤与断言

- 一条场景步骤对应一个 `step(page, 标题, 动作)`；标题照抄场景，录屏里显示为章节，report 里显示为同名步骤。
- 每步结束留一张截图（`testInfo.attach`），`pass` 至少需要一份证据。
- 只断言用户看得见的结果：文字、状态、列表项、提示。URL 变化不能代替页面内容，也不要读 Redux、localStorage 代替界面。
- `forbidden_effects` 逐项核对：没发生的事也要有断言或截图说明。
- 等待可观察的条件（元素出现、文字变化、请求完成），`waitForTimeout` 只用于录屏停留。

## 副作用与重试

发送、保存、接管、支付这类动作只做一次。Playwright 的 `retries` 保持 0。失败后由人决定：恢复数据后重跑，还是记为失败。前一轮残留（草稿、已接管状态）会改变前置条件，重跑前先核对 `initial_state`。

## 证据与隐私

- `PW_OUT` 指向仓库外目录：`results/`（原始产物）、`html/`（report）、`report.json`、`junit.xml`。
- 录屏用 `page.screencast`，只录指定标签页；trace 录整个 context。证据里会有 cookie、令牌与客户数据，不要贴进 PR 或聊天，只在验收记录中引用。
- 单个文件超过 64 MiB 时看板拒绝接收，`to_scenarios.py` 会列出并停止；trace 过大时关闭 `snapshots` 或缩短场景。

## 禁止副作用的观察

在 surface-map 中记录与本轮有关的业务写入口、只读查询入口及依据；按入口的业务语义核对禁止副作用。不能用“所有 POST 都是写入”或“所有 GET 都没有副作用”代替接口核对。网络记录仅辅助判定；业务通过仍以场景指定的可见结果为准。

依赖预检使用当前项目已安装的 @playwright/test 和同包 CLI，不调用 npx 自动下载另一个版本。连接、录屏、trace 初始化中途失败也逐项停止录制并断开本轮 CDP 连接；业务失败与清理错误分别保留。


## 同一轮复用 CDP 授权

连接是 worker 级资源：`workers: 1`、`retries: 0` 时，正常执行的一轮共用一个 CDP 连接，最后释放。global setup 只做依赖、scope 和 TCP 端口检查，不为预检另开 CDP 会话；唯一目标页和 iframe 的实际检查在 worker 首次连接时完成。每条用例仍独立启动、停止和保存录屏与 trace，且逐项核对自己的前置条件，共享连接不等于重置业务状态。

不要在每条用例后 `browser.close()`，也不要在连接断开时静默重连。Playwright 在失败后可能重启 worker；手动重跑、worker 重启或 Chrome 断开都可能需要再次同意。不能保证跨连接永久免授权，也不修改 Chrome 的安全设置来消除提示。
