# 执行场景

场景键写作 `<文件>#<场景编号>`（编号唯一时可省文件）。范围取 `pending`（缺省：未执行、待复测、失败、受阻）、`all`，或列出场景键。

## 独立执行（首选）

1. `sop-board run launch --scope <范围> [--implementer <实现者>]`：守护进程按项目 `agents` 配置启动一个新的无头会话。它只拿到 prompt 文件，会话编号由启动器取得，启动记录就是独立性的证据。
2. `sop-board run wait [--timeout <秒>]` 等它结束（缺省 1800 秒；退出码 3 表示超时、启动仍在运行，可再次 wait）。`sop-board launch status` 查看启动记录与日志；使用者要求停止时 `sop-board launch stop --launch <编号>`。
3. 把 wait 输出中的提交结果（登记、作废、未执行的场景）如实转述。

协调会话只发起和转述：结果由执行会话写出并提交，协调会话保持原样转述。同一检出已有运行中的启动时返回 `launch-running`，报告该启动并等它结束。

## 当前会话执行

使用者要求当前会话执行时：

1. `sop-board run prepare --scope <范围>`。
2. 读输出的 prompt.md 与 brief.md，按其执行、留证、写结果。
3. `sop-board run submit --run <编号> [--implementer <实现者>]`，如实转述提交输出。

## 实现者与独立性

`--implementer` 填改了被验收产品代码的会话编号（人写的填姓名）。与执行者不同记为独立验收，相同记为实现者自检，不填记为独立性未声明。当前会话改过这些产品代码又自己执行时，填自己的会话编号，报告为实现者自检。不清楚实现者时不填，报告为未声明。修复复验的实现者由系统自动取修复会话。

## 准备时的拒绝

| error_code | 处理 |
| --- | --- |
| `run-prepared` | 本检出已有 prepared 运行：报告编号；是本会话的就完成并 submit；使用者同意放弃后 `sop-board run abandon --run <编号>` |
| `env-busy` | 端口被另一检出的环境占用，输出写明占用者与 `env down` 命令：转告使用者，由他决定 |
| `browser-busy` | 该身份正被真人登录占用：等使用者用完并 `browser close` 后再准备 |
| `draft-not-runnable` | 范围含草稿场景：先按 [draft.md](draft.md) 确认 |

## 真人登录

身份需要真实账号登录时：`sop-board browser open --identity <身份> --headed` 在 Chrome for Testing 窗口打开入口，请使用者自己输入密码、完成验证，登录态保存在该身份的 profile；完成后 `sop-board browser close --identity <身份>`。出现 `browser may not be secure` 时停止，相关场景记受阻并照录原文。身份处于 crashed 状态时，经使用者同意后 `sop-board browser open --identity <身份> --reset-crash`。

## 人工复测结论

使用者转述测试同学的复测结论时：`sop-board mark <场景键> pass|fail [--defect <缺陷链接>] [--note <说明>]`，不通过时 `--defect` 必填；复测人是别人时加 `--as <姓名>`。
