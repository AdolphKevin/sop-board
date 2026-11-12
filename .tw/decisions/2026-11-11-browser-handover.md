---
title: "真人完成后用 browser release 交还浏览器，窗口不关"
scope: ["self:src/sop_board/browsers.py", "self:src/sop_board/skills/**"]
source: ["用户 blur-yin，2026-11-11，cw2 实际使用复盘的需求", "self:docs/design.md"]
status: "accepted"
tags: ["浏览器", "真人登录", "人机验证", "交还", "browser close", "browser release"]
verified: {"self": {"commit": "aa7649d6d20d6129db420c527fc9ba00453e8217", "date": "2026-11-13"}}
---
# 真人完成后用 browser release 交还浏览器，窗口不关

背景：原流程要求真人在 `browser open --headed` 的窗口里登录后运行 `browser close`，它结束浏览器进程；下一次运行再拉起无头浏览器。2026-11-10 cw2 的会话照做，用户问“你关了那我不白登录了？”。之后连续几轮：Cloudflare 对无头浏览器再次要求人机验证，有界面窗口里允许过的本地网络访问与证书例外也随进程消失；会话只好绕过 CLI，直接调用本机接口 `/local/browser/release` 只解除真人占用。另外，不带 `--headed` 的 `browser open` 会把运行中的有界面窗口重启成无头，会话用它“核对登录态”时又关掉了用户刚登录好的窗口。

决定（用户 blur-yin 2026-11-11 提出的需求）：
- 新增 `sop-board browser release [--identity]`：只解除 human 租约，浏览器进程、窗口与页面保持运行；被运行持有时返回 `browser-busy`。本机接口 `/local/browser/release` 改为按身份交还，不再接受任意持有者。
- 下一次取得租约的运行直接沿用这个进程（仍是有界面窗口），不重启；运行提交或放弃也只释放租约。
- 不带 `--headed` 的 `browser open` 遇到运行中的有界面窗口时沿用它；只有要窗口而现有进程是无头时才重启。
- `browser close` 只在要结束浏览器时使用；`browser-busy` 的提示、技能与文档都改为“完成后 `browser release`”。

## 备选
- 运行取得租约时直接接管真人占用的窗口：人可能还在窗口里输入，BRW-03 的“真人占用时返回 browser-busy”要保留。
- 记住身份上次的模式，之后重启时也开有界面窗口：守护进程或浏览器重启后的启动方式与崩溃规则都要改，这次的问题只靠不关窗口就能解决，暂不采用。
- 给 CfT 加 `--allow-insecure-localhost` 等启动参数省掉证书提示：窗口不关后例外本来就保留，不为此改启动参数。

## 后果
- 真人处理过的身份之后以有界面窗口运行，执行时窗口会被自动操作；使用者在运行期间不要点它。
- `browser close` 会结束浏览器：cookie 留在 profile，但与浏览器标识绑定的人机验证结果、会话内的证书例外会失效，之后可能要再验证一次。按已定决策 26，守护进程停止保留有界面窗口、重启后认回，人做过的验证随该进程保留（见 [重启认回](2026-11-13-browser-recovery.md)）。
