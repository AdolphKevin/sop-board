---
title: "浏览器层 spec 的保真基线连同它的本地导入一起取自 git HEAD"
scope: ["self:src/sop_board/specs.py", "self:src/sop_board/guards.py", "self:src/sop_board/skills/playwright-acceptance/scripts/spec-assets.mjs", "self:src/sop_board/skills/playwright-acceptance/scripts/spec-ast.mjs"]
source: ["统一验收 2026-11-12 ATS-02-N01：distill submit 与 specs check 都报“spec 静态分析失败：ENOENT … sop-board-baseline-*/acceptance/specs/fixtures.mjs”"]
verified: {"self": {"commit": "f6a021cc16d990c96529af290b929dcc7937915b", "date": "2026-11-12"}}
guards: ["self:docs/sop/atlas-specs.yaml#ATS-02-N01"]
tags: ["spec", "保真检查", "基线", "fixtures.mjs", "specs check", "distill submit", "ENOENT", "浏览器层守护"]
---
# 浏览器层 spec 的保真基线连同它的本地导入一起取自 git HEAD

现象：spec 按规定只经 `./fixtures.mjs` 导入并连同夹具一起提交后，对没改动的 spec 运行 `specs check`、或删掉断言后 `distill submit`，都报 ENOENT，保真检查根本跑不起来；浏览器层守护全部受影响。

根因：基线只把 HEAD 里那一个 spec 文件写进临时目录，分析器在临时目录里跟随导入找 `./fixtures.mjs` 时找不到。分析器还要求导入只能是 `./fixtures.mjs`、`./library.mjs`，所以合规的 spec 必然触发。

修复与防护：`specs.head_baseline` 只写 spec 本身，分析基线时设 `SOP_SPEC_BASELINE_REPO`；`spec-assets.mjs` 的 `fromHead` 在跟随导入、读取任务资产（`scenarios/library|pages|support|data`）时，把缺的文件按需从 HEAD 取出写进临时目录（HEAD 里的符号链接照样还原，由已有检查拒绝）。基线因此是 HEAD 时的整套 spec，而不是 HEAD 的 spec 配上工作树里的夹具。临时目录用完即删（原来每次检查都留下一个）。

发现方法：在一个提交了 spec 与夹具的仓库里，对未改动的 spec 运行 `specs check`，应全部为 `same`。

适用条件：改动保真检查、基线来源或 spec 的导入约定时。签名包含 `execution`（各依赖文件去掉断言后的形状）与 `preceding_assertions`，所以夹具改动或删掉前面的断言，会让同文件其他断言也列为“改动”。
