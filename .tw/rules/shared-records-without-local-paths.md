---
title: "共享的业务记录不带任何一台机器的本机路径，引用证据用名称与 sha256"
scope: ["self:src/sop_board/fixes.py", "self:src/sop_board/runs.py", "self:src/sop_board/guards.py", "self:src/sop_board/ledger.py", "self:src/sop_board/browsers.py", "self:src/sop_board/redaction.py"]
source: ["统一验收 2026-11-12 FIX-03-N01：共享库 fixes.problems 保存了产出机器的 /private/tmp/…/evidence/… 路径", "self:docs/design.md"]
verified: {"self": {"commit": "f6a021cc16d990c96529af290b929dcc7937915b", "date": "2026-11-12"}}
guards: ["self:docs/sop/fix.yaml#FIX-03-N01"]
tags: ["共享库", "本机路径", "证据路径", "fixes.problems", "problems.json", "内网遮蔽", "崩溃报告", "空格"]
related: ["decisions/2026-11-12-shared-acceptance-records.md"]
---
# 共享的业务记录不带任何一台机器的本机路径，引用证据用名称与 sha256

写进业务记录库（PostgreSQL 或本机库的业务表）的内容，别的机器也会读到：只记证据的名称与 sha256，需要本机位置时在读取的那台机器上按 sha256 现算（`store.evidence_path`），写进本机文件。修复的 `fixes.problems` 只记名称与 sha256；给修复会话读的本机 `problems.json` 由 `fixes.local_problems` 补上本机路径，本机没有的写“证据在 <机器> 的机器上”。

现有例外（审核方 2026-11-12 确认保留）：浏览器崩溃时由 sop-board 写的受阻说明（`results.actual/finding`）、“浏览器崩溃”待人处理（`attention.text`）、守护运行原因里的浏览器问题（`guard_runs.reason`）带 chrome.log 与 DiagnosticReports 的本机路径；BRW-05-P01、BRW-05-N01 的预期要求 finding 写明日志路径，所以保留：这些路径只在产出机器上有意义，记录本身带产出机器，内网页面照常遮蔽。

内网页面照常遮蔽本机路径；路径里有空格（`Application Support/…`、`Google Chrome for Testing-….ips`）时整段遮蔽：空格之后 255 字节内经路径字符还有斜杠，或以文件扩展名结束，就算路径的一部分（`redaction.PATH_SPACE`）。

## 依据

设计“内网不显示本机路径”的方向与已定决策 21“证据留在产出机器，别的看板按 sha256 中转”；产出机器的路径对别的机器没有意义，还会把用户名、目录布局带到别人的页面上。
