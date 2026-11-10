# 前置数据、身份、待人处理、检出与报告

## 前置数据

运行中准备数据以运行目录的 brief.md 为准。命令：

- 配方：`sop-board fixtures run <配方> [--param 名=值 …] [--run <运行编号> --scenario <场景键>]`。
- 没有合适配方且项目允许临时写入时：`sop-board fixtures exec --reason <理由> [--run <运行编号> --scenario <场景键>] -- <命令>`。返回 `adhoc-not-allowed` 时只能用配方，缺配方就记受阻并转告使用者。

## 身份计数

`sop-board identity status` 查看各身份的登录与验证码次数。真实登录或请求验证码之前记一次：`sop-board identity record --identity <身份> --kind login|code [--run <运行编号>]`。返回 `identity-limit` 时停止该身份的操作，已自动登记待人处理，转告使用者。

## 待人处理

- 查看：`sop-board attention list`。
- 登记需要人做的事（登录、人机验证、授权、环境问题等）：`sop-board attention add --kind <种类> --text <需要人做什么> [--scenario <场景键>] [--run <运行编号>]`。种类取 人机验证、OAuth 授权、账号登录、第三方后台设置、权限放行、邮箱验证、环境问题、前置数据、浏览器崩溃、其他。
- 使用者说已处理：`sop-board attention resolve <编号> --note <处理结果>`。

## 检出与环境

- `sop-board checkout list|show` 查看检出；使用者要新建 worktree 检出时 `sop-board checkout create <名称> [--from <ref>] [--repo <别名> …]`，移除用 `sop-board checkout remove <名称>`（先停该检出的环境）。
- `sop-board env status|up|down` 查看、启停当前检出的产品环境（`env status --all` 列出全部检出）；`run prepare` 会自己拉起环境。停止其他检出的环境须经使用者同意。
- 浏览器运行时：`sop-board browser install`、`sop-board browser status`。

## 报告

`sop-board report [--file <场景文件>] [--zip] [--out <目录>]` 导出当前检出的 Markdown 报告，`--zip` 同时打包证据。
