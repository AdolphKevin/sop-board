---
title: "正在执行的轮次不因别人改代码而暂停，修复提交后先复验再发起下一轮"
scope: ["self:src/sop_board/skills/sop-board/references/run.md", "self:src/sop_board/skills/sop-board/references/fix.md", "self:src/sop_board/skills/sop-board/SKILL.md"]
source: ["用户 blur-yin，2026-11-14，对 cw2 分轮验收为修复暂停的质疑"]
guards: ["self:docs/sop/run.yaml#RUN-08-P01", "self:docs/sop/run.yaml#RUN-08-N01"]
verified: {"self": {"commit": "36c885d8fc7a471b5f5e005905afc94e8b06b389", "date": "2026-11-14"}}
tags: ["product_changed", "修复", "复验", "并行", "分轮验收", "协调会话", "launch-running", "暂停", "旧记忆"]
related: ["decisions/2026-11-11-business-driven-acceptance-scope.md", "decisions/2026-11-13-one-environment-per-machine.md", "decisions/2026-11-14-unattended-hardening.md"]
---
# 正在执行的轮次不因别人改代码而暂停，修复提交后先复验再发起下一轮

现象：2026-11-13 晚 cw2 分轮验收，协调会话逐轮 `run launch`。第 2 轮进行中，使用者另开的修复会话改了 front-b 与 Core，`run wait` 输出带 `product_changed`。协调会话停下第 3 轮，后台轮询等修复会话 10 分钟无动作、代码 5 分钟无改动，并告诉使用者“修复和场景执行不并行”。之后又在复验结束、修复会话再有动作时停了一次。使用者质疑这不是 sop-board 的设计。

根因：已定决策 18 起，执行期间的代码变化只作版本记录，标准第 6 节写明产品代码可在执行期间继续修改和提交、本轮不作废；但随包技能没写协调会话见到 `product_changed`、或别的会话正在修复时该怎么做。协调会话于是沿用 cw2 项目记忆里 v9 时期的两条旧做法：一条是“轮次进行中不改受版本管理的文件”（理由是 v9 的 fix land 要求结果对应固定版本），另一条是内存护栏“修复的构建、测试只在两轮之间做”被索引缩写成了“修复不与执行并行”。

修复与防护：run.md 的「执行期间代码在变」写明：轮次已经在执行时别人可以改代码，结果照常登记，已登记的轮次保持原样；失败场景与变化文件相关时在转述里写明。发起下一轮之前按本检出修复的状态处理：`open` 且 `run launch` 返回 `fix-in-progress` 时等修复提交（已定决策 29，起因是 11-14 上午新一轮按改到一半的后端重启、8 条全记受阻）；`submitted` 时先 `run launch --fix` 复验再发起下一轮，因为同一检出同时只有一个运行中的启动。fix.md 写明修复会话遇到 `launch-running` 时等那个启动结束，那个启动的 `scope` 正是本修复的问题场景时转述它的结果、不重复发起。cw2 的两条旧记忆已改写。

发现方法：协调会话因 `product_changed` 暂停或重跑已登记的轮次、轮询等待别的会话安静，或要求使用者“两轮之间再改”时，对照本条。

适用条件：同一检出上协调会话分轮执行场景，同时有修复会话或开发者改产品代码。机器内存吃紧时，重型构建与测试不与执行叠加属于项目自己的约束，与本条无关。
