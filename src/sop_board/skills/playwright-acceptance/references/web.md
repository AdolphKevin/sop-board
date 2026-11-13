# 通用 Web 参考

## 浏览器与连接

看板守护进程（`sop-board serve`）为每个身份启动一个 Chrome for Testing（各检出共用；运行时由 `sop-board browser install` 固定版本安装，默认 `--headless=new`），持久 profile 保存登录态。`run prepare` 租用本轮的身份并把地址写进运行目录，同一身份同一时刻只给一个运行：

- `PW_CDP_ENDPOINT`：默认身份浏览器的调试地址，`connection.mjs` 只读它；缺失时报错并记 blocked。
- `PW_IDENTITIES`：`identities.json`，列出每个入口的身份及各自的 `endpoint`；`identity.mjs` 的 `as(身份)` 据此连接。
- `PW_TAB`（可选）：目标标签页 URL 的子串；匹配到一个则复用，零个则新开一页，多个则报错。`PW_FRAME`（可选）是 surface-map 已确认的唯一 iframe 选择器。`PW_CONNECT_TIMEOUT_MS` 缺省 30000。

浏览器状态用 `sop-board browser status` 查看。brief.md 的"浏览器问题"一节列出启动失败的身份，用该身份的 Web 场景直接记 blocked，finding 照抄。

`global-setup.mjs` 在开跑前检查 Playwright 版本、scope、调试端口与（仅本轮需要录屏时）ffmpeg，不另开 CDP 会话；唯一目标页与 `PW_FRAME` 在 worker 首次连接时核对。任何一项失败，整轮不执行业务步骤。

### 账号登录与人做的验证

- **声明的测试账号**（项目手册或技能列出、凭据在 `credential_env` 变量中，含为验收注册的第三方账号与测试邮箱）：声明即授权。未登录时用凭据变量直接在页面登录，需要验证码时申请并从声明的测试邮箱读取；每次先按[使用次数](#多身份与使用次数)登记。登录放在录制之外：`playwright test` 之前在运行目录用一次性脚本经同一身份的 endpoint 登录，登录态留在 profile，场景里只核对已登录。trace 会记下填写的值与登录请求正文，录屏会拍到输入框，所以不在场景用例里填密码；登录本身就是被验收的步骤时，提交前逐个检查 trace 与录屏，里面不能出现取值。
- **人做的部分**：真实账号与未声明账号的登录、人机验证、只发到使用者本人设备（手机、身份验证器）的验证码。开始前或运行中遇到时记 not-executed：`throw notExecuted('<身份> 需要真人<做什么>（<哪一页>）')`，并 `sop-board attention add` 登记，写明哪个身份、在哪一页、需要人做什么。未执行不改变场景状态。人在看板窗口完成后交还，窗口保持打开，下一轮直接沿用：

  ```bash
  sop-board browser open --identity <身份> --headed   # 打开入口，环境没在运行时先拉起；窗口已开着时只取得真人租约，不重启
  sop-board browser release --identity <身份>          # 人完成后交还，窗口不关
  ```

  人持有租约期间 `run prepare` 返回 `browser-busy`。`browser close` 会结束浏览器，人在窗口里做过的验证随之作废，只在要关掉浏览器时用。
- **测试环境的浏览器提示**：测试环境自身地址触发的本地网络访问请求、localhost 自签名证书警告等，由执行者处理（页面上的“继续”、该浏览器的站点设置）；处理不了再按人做的部分登记。
- 遇到 "browser may not be secure" 立即停止并记 blocked。

看板停止与重启时有界面窗口的保留与认回（`browser-unrecovered`）由发起运行的会话处理，见 sop-board 技能的 [run.md](../../sop-board/references/run.md)「账号与真人操作」；执行者始终只连 run.env 与 identities.json 给出的地址。

### 崩溃

- 浏览器进程在运行中退出：`run submit` 把该身份相关且未通过的 Web 场景记为 blocked（已写出的 pass 保留），finding 含信号与 chrome.log 路径，并登记待人处理。
- 渲染进程崩溃（`page.on('crash')`、错误含 `Target crashed`）：夹具按 blocked 处理并停止本轮。
- 30 分钟内异常退出两次，该身份进入 `browser-crashed`，须人检查后用 `sop-board browser open --identity <身份> --reset-crash` 重置，再用最小范围另开一次运行确认。
- 租约持有期间看板不自动重启浏览器；执行者也不在用例中重连，不自行启动浏览器。

### 多身份与使用次数

每个身份是独立的浏览器与 profile，切换身份就是换浏览器：`const page = await as('<身份>')`，结束时 `release()`。每次真实登录或请求验证码之前先登记：

```bash
sop-board identity record --identity <身份> --kind login|code --run <N>
```

超限时被拒绝并已自动登记待人处理；停下，把该身份的场景记 not-executed。

### 连接共用

连接是 worker 级资源：`workers: 1`、`retries: 0` 时整轮共用一个 CDP 连接，最后释放，不在每条用例后 `browser.close()`，也不在断开时静默重连。每条用例按入口开关独立启动、停止和保存录屏与 trace，并逐项核对自己的前置条件；共享连接不等于重置业务状态。模板把业务失败标为预期失败（worker 不重启、连接保留），`maxFailures: 1` 在首个意外失败后停止，避免连续重连。连接问题经排查并确认恢复后，用最小范围确认，再另开一次运行。

## 界面层次

先确定目标在哪一层，再选定位方式。不要写通用的重试循环：Playwright 的定位器和断言本身会等待。

| 类型 | 识别 | 处理 |
| --- | --- | --- |
| 页面内固定 iframe | `iframe[name=…]` 或 `iframe[src*=…]` | `page.frameLocator(选择器)`，每次操作自动等待 iframe 出现 |
| 操作后出现的 iframe 弹窗 | 名称或 `src` 带稳定前缀；可能有隐藏的预加载副本 | 弹出后换用新的 `frameLocator`；按名称前缀加 `src` 路径识别，不按出现顺序 |
| 同一文档内的对话框 | `getByRole('dialog')` | 普通元素 |
| 新标签页 | 点击前 `context.waitForEvent('page')` | 单独操作；启用录屏时夹具为新标签页录制 |
| 浏览器原生对话框 | `page.on('dialog')` | 事先注册处理 |
| 已知且与业务无关的遮挡（引导、Cookie 提示） | 项目在界面地图中声明 | `page.addLocatorHandler(定位器, 关闭动作)`，由 Playwright 在需要时触发 |
| 人机验证、真实账号登录过期 | 页面文字 | `throw notExecuted(…)`，登记待人处理 |
| 未知遮挡 | 页面文字 | `throw blocked(…)`，保留截图与页面结构 |

连接后，跨域 iframe 要过一两秒才出现在 frame 树里；配置里的 `expect.timeout` 已放宽到 15 秒，不要再写轮询。

## 当前状态与必要导航

每次导航前，按项目界面地图检查目标视图的可见内容、唯一容器和对象身份：

- 目标已就绪：复用现有页面、对话框、iframe 或标签页，不再次点击外层入口。
- 目标未就绪且状态已知：只执行映射中必要的导航，再等待目标内容可见。
- 当前层次不明、定位多义或出现未知遮挡：停止动作，保留截图及页面结构，重新只读探测；不按旧选择器连续点击，不通过强制点击或擅自关闭弹窗来跑通。

URL 和 iframe 的 src 只用于辅助识别。单页导航可能不改变 iframe 元素的 src；需要结合当前 frame 与可见业务内容识别。项目提供目标就绪条件、容器特征和入口，通用技能不固定页面名称、菜单、路由或 iframe 选择器。

这里只省略重复的导航准备动作。场景明确要求刷新、关闭重开、重置或再次进入时仍须执行；不能为了复用页面而跳过受验证的业务步骤。

## 只读探测

黑盒验收方常常没有产品源码。此时界面结构只能从真实页面取，用临时脚本连上同一个运行目录浏览器做以下几件事，把结果写进界面地图。临时脚本放在本次运行目录（那里的 `node_modules` 能解析 `@playwright/test`），用完删除：

- 列出 `context.pages()` 的 URL 和目标页的 `page.frames()`（name、url），确认 iframe 层次与预加载副本。
- 对页面和每个相关 frame 取 `locator('body').ariaSnapshot()`，从中抄下按钮、标题、占位文字的准确文案；没有多语言文件时，脚本里的文字以快照为准。
- 需要看到下一层界面时，只执行场景里已经写明且没有副作用的步骤（进入页面、打开列表或弹窗、选中查看）；不点发送、保存、接管、删除、支付等控件，不改任何设置。
- 探测时顺便记录加载态文字（如"… is loading"）和出现时间，供设置等待阈值。

探测结论只代表探测当天的界面。界面地图里对应条目的"依据"列写"未核对源码，探测于 <日期>"；产品改版或脚本定位失效时先重新探测，不要凭记忆改选择器。

## 定位

优先级：`getByRole` 带可见名称 → `getByLabel`、`getByPlaceholder`、`getByText` → `data-testid` → 第三方库的稳定类名（如 `.ReactVirtualized__Grid__innerScrollContainer > div`）。按钮、标签文字从产品多语言文件取，没有源码时从 aria 快照抄；脚本与场景使用同一种语言；测试账号的界面语言固定。

同一文字出现多处时加 `exact: true` 或限定容器，不要用 `.first()` 掩盖歧义，除非场景本身写的就是"第一项"。

## spec 的导入

spec 文件（`scenarios/*.spec.mjs`）里只写两种导入：`./fixtures.mjs` 与 `./library.mjs`，路径用静态字符串。其余依赖都放进自建的 `scenarios/library.mjs`，由它导入再导出给 spec：

```js
// scenarios/library.mjs
import {execFileSync} from 'node:child_process';
export {as, release} from './identity.mjs';   // 多身份；有地图时同样转出 atlas.mjs
export const query = (...args) => execFileSync('<只读查询命令>', args, {encoding: 'utf8'});
```

```js
// scenarios/<名称>.spec.mjs
import {test, expect, step, shot, blocked, notExecuted, tags} from './fixtures.mjs';
import {as, query} from './library.mjs';
```

沉淀时，产生独立通过的这份 spec 是保真检查的基线，静态分析按这条规则读它；spec 直接导入 `node:child_process`、`./identity.mjs` 等时基线不合格，`distill submit` 拒绝，场景只能按规则重写 spec、重新验收后再沉淀。

## 步骤与断言

- 一条场景步骤对应一个 `step(page, 标题, 动作)`；标题照抄场景，录屏里显示为章节，report 里显示为同名步骤。
- 按入口 `evidence` 声明保存截图及只读查询等证据（`testInfo.attach`），实际执行的 `pass`/`fail` 至少需要一份证据。
- 只断言用户看得见的结果：文字、状态、列表项、提示。URL 变化不能代替页面内容，也不要读 Redux、localStorage 代替界面。
- `forbidden_effects` 逐项核对：没发生的事也要有断言或截图说明。
- 等待可观察的条件（元素出现、文字变化、请求完成），`waitForTimeout` 只用于录屏停留。
- 保存、提交等动作须在操作前注册响应或页面事件等待，避免漏掉快速事件；完成条件由项目映射提供。点击返回、当前单选项变化或按钮恢复可点不等于保存成功；HTTP 2xx 也不能代替业务成功状态。先核对成功提示、状态或列表变化，再执行场景要求的刷新及重新进入核对。
- 完成信号超时或响应不明确时，保留现场并停止；不得立刻刷新中止尚未完成的提交，也不得再点一次来碰运气。

## 前置数据

无关前置数据指 initial_state 中不由本场景步骤产生、也不验证其产生过程的对象或状态。brief.md 的"前置数据"一节说明本项目是否声明了配方：有配方时用 `sop-board fixtures run <配方> --run <N> --scenario <场景key>`；没有合适配方且 adhoc 为 true 时，才在声明环境内用 `fixtures exec --reason <说明> -- <命令>` 临时写入，两者都留痕。项目没有声明配方时，前置不满足就记 blocked，不自行写库。

被验收流程应产生的对象与 `real_only` 对象只能经真实入口产生；禁止 DROP、TRUNCATE、无条件 DELETE/UPDATE 及范围外写入。无法准备时记 blocked，finding 写明缺什么。准备记录只说明数据来源，预期来自场景，结论来自真实入口的观察。凭据只经环境变量传入，不写入脚本或证据。

## 副作用与重试

发送、保存、接管、支付这类动作只做一次。运行器配置保持 `workers: 1`、`retries: 0`、`maxFailures: 1`。失败后由人决定：恢复数据后重跑，还是记为失败。上一次运行的残留（草稿、已接管状态）会改变前置条件，重跑前先核对 `initial_state`。

## 证据与隐私

- `PW_OUT`（run.env 已设为运行目录内的 `playwright/`）：`results/`（原始产物）、`html/`（report）、`report.json`、`junit.xml`。
- 录制开关取本轮 scope 中按入口固定的 `web_evidence`，缺省均开启，关闭项不启动、不保存。开启时，录屏用 `page.screencast`，只录指定标签页；trace 录整个 context。证据里会有 cookie、令牌与客户数据，不要贴进 PR 或聊天，只在验收记录中引用。
- 单个文件超过 64 MiB 时看板拒绝接收，`sop-board playwright convert` 会列出并停止（`--skip-large` 跳过该文件）；trace 过大时关闭 `snapshots` 或缩短场景。

## 禁止副作用的观察

在 surface-map 中记录与本轮有关的业务写入口、只读查询入口及依据；按入口的业务语义核对禁止副作用。不能用“所有 POST 都是写入”或“所有 GET 都没有副作用”代替接口核对。网络记录仅辅助判定；业务通过仍以场景指定的可见结果为准。

依赖预检使用运行目录 `node_modules` 中看板运行时的 @playwright/test 与同包 CLI，不用 npx 另行下载。连接、录屏、trace 初始化中途失败也逐项停止录制并断开连接；业务失败与清理错误分别保留。

## 报告口径

业务失败、执行受阻和未执行分别报告：未执行是本机暂时不具备（等人处理、缺凭据变量、服务没在运行），受阻是意外故障。提前停止不代表剩余场景已运行；保留原始报告，通过部分转换及 `coverage.json` 说明实际范围。
