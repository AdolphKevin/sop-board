# 前置数据、身份、待人处理、检出与报告

## 前置数据

运行中准备数据以运行目录的 brief.md 为准。命令：

- 配方：`sop-board fixtures run <配方> [--param 名=值 …] [--run <运行编号> --scenario <场景键>]`。
- 没有合适配方且项目允许临时写入时：`sop-board fixtures exec --reason <理由> [--run <运行编号> --scenario <场景键>] -- <命令>`。返回 `adhoc-not-allowed` 时只能用配方，缺配方就记受阻并转告使用者。

## 身份计数

`sop-board identity status` 查看各身份的登录与验证码次数。真实登录或请求验证码之前记一次：`sop-board identity record --identity <身份> --kind login|code [--run <运行编号>]`。返回 `identity-limit` 时停止该身份的操作，已自动登记待人处理，转告使用者。

## 待人处理

- 查看：`sop-board attention list`。
- 登记只有人能做的事（真实账号登录、人机验证、只发到使用者本人设备的验证码、环境问题等；声明的测试账号登录不在此列）：`sop-board attention add --kind <种类> --text <需要人做什么> [--scenario <场景键>] [--run <运行编号>]`。种类取 人机验证、OAuth 授权、账号登录、第三方后台设置、权限放行、邮箱验证、环境问题、前置数据、浏览器崩溃、其他。
- 使用者说已处理：`sop-board attention resolve <编号> --note <处理结果>`。

## 检出与环境

- `sop-board checkout list|show` 查看检出；使用者要新建 worktree 检出时 `sop-board checkout create <名称> [--from <ref>] [--repo <别名> …]`，移除用 `sop-board checkout remove <名称>`（先停该检出的环境）。
- `sop-board env status|up|down|restart` 查看、启停当前检出的产品环境（`env status --all` 列出全部检出）；`run prepare` 会自己拉起环境。adapter 输出就绪即环境可用，`env status` 的 `services` 列出各服务的状态（运行中、已停止、本机未搭建、异常退出、未上报）与业务说明，异常退出附日志末尾。停止其他检出的环境须经使用者同意。
- `sop-board env stop|start <服务>` 只在本检出有 prepared 运行时用来模拟依赖故障（见 [run.md](run.md)「依赖故障的场景」）；运行之外要恢复环境，用 `sop-board env restart`。
- 停止看板：`sop-board stop`，环境、无头浏览器与启动的会话随之停止，有界面的窗口保留。重启看板（例如升级 sop-board 之后）：`sop-board restart`，浏览器、租约、启动的会话与 prepared 运行都保留，环境几十秒内重新拉起；在 Codex 沙箱里请使用者在终端运行。
- 浏览器运行时：`sop-board browser install`、`sop-board browser status`。看板停止与重启时有界面窗口的保留、认回与 `browser-unrecovered` 见 [run.md](run.md)「账号与真人操作」。

## 报告

`sop-board report [--file <场景文件>] [--zip] [--out <目录>]` 导出当前检出的 Markdown 报告，`--zip` 同时打包证据。

## 产品备注

执行会话只读看板给的材料。使用者说到“以后验收要知道……”“这个现象不用管”“这个账号可以临时改”这类该告诉执行者的事实时，记成产品备注；备注按产品共享，同一产品的其他成员不用另装任何东西。

- `sop-board note add --text <一条事实> [--entry <入口> | --file <场景文件>]`：一条写一件事。只写事实，不写点击路线和元素定位。挂得越窄越好：只和某个入口有关的挂入口，只和某个场景文件有关的挂文件；全产品备注每一轮都带，合计超过 3000 字时命令会提醒，用 `note move <编号> --entry|--file` 改挂。只对某一次启动有效的安排不写备注，用 `run launch --note`。
- `sop-board note list`：`confirmed_by` 为空的是执行会话在运行里提出的，`disputes` 非空的是有人报告与实际不符。把这两类逐条报给使用者，按他的决定 `note confirm <编号>`、`note edit <编号> --text <改后的事实>` 或 `note remove <编号>`；不替使用者确认。

## 前置缺口与临时改动

- `status --json` 的 `gaps` 按缺的对象归并了因前置数据受阻的场景：把缺口和各自挡住的场景报给使用者，由他决定补数据配方、在备注里写明可以临时改哪些对象，还是由人准备。
- `run wait` 与 `run submit` 输出的 `unrestored_changes` 是执行会话改了测试对象却没恢复的项：逐条转述改了什么和恢复办法，它们已登记为待人处理。

