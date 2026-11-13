# 验收场景草稿：生成、审阅与确认

草稿是场景目录下 `metadata.status: draft` 的场景 YAML。草稿不进入执行（`run prepare` 返回 `draft-not-runnable`），确认后才进入运行范围。测试同学在看板工作台“草稿”筛选中对照五项检查清单与 `refs` 指向的 SOP 规则原文审阅每条场景：有问题写批注，没有问题点「同意」。

## 生成草稿

1. `sop-board checkout show` 的 `checkout.scenarios` 是场景目录；已登记的 SOP（`.md`）和现有场景文件都在这里，现有场景文件是最直接的样例。
2. 核对业务交叉，列出交叉清单：按 SOP 写明的交叉，逐个读现有场景，把验证交叉规则的每条已有场景记下来（场景键 `<文件>#<场景编号>`），分成两类：
   - 预期随新规则改变：直接修改它的四要素（内容一变即回到未执行；看板只按场景内容决定哪些场景要重新验收）。改已确认文件里的场景前，先向使用者说明改哪些场景、改成什么。
   - 预期不变：保持原样，记下不变的理由（哪条口径没变）。

   两类场景都留在原文件里继续承担验证，新草稿不另写与它们起点、步骤、预期都相同的场景。这份清单在第 6 步交给使用者。
3. 依据 SOP 写 `<场景目录>/<名称>.yaml`。写法遵循 sop-board 仓库 [docs/sop-guide.md](https://github.com/AdolphKevin/sop-board/blob/main/docs/sop-guide.md) 的“四要素”“场景粒度”“正向与反向”三节：预期只来自 SOP 原文和使用者的答复；SOP 没写明的结果在对话中问使用者，代码现状不当作预期；每个节点至少一条正向与一条反向场景，确实没有反向时写 `negative_exempt` 理由。
4. 结构如下。`status: draft` 单独一行、`sources:` 用块格式，`draft confirm` 依赖这两点：

   ```yaml
   metadata:
     requirement: "<这批场景验证的需求>"
     version: 1
     owner: "<负责人>"
     status: draft
     sources:
       - ref: "<SOP 文件名>"
         version: "<SOP 确认日期>"
         confirmed_by: "<SOP 文末的确认人>"
   sops:
     - id: R1
       title: <节点标题>
       actor: <角色>
       entry: <入口说明>
       scenarios:
         - id: R1-P01            # 不含 #
           polarity: positive    # 反向写 negative，并加 negative_type
           title: <标题>
           actor: <角色>
           priority: P1
           verify: <config 中声明的入口名>
           initial_state: ["…"]
           steps: ["…"]
           expected: ["…"]
           forbidden_effects: []
           refs: ["<SOP 文件>#R1"]
   ```

   每条场景的 `refs` 写 `<SOP 文件名>#R<n>`：看板据此在审阅页展开该规则的原文。
5. 运行 `sop-board status --json`，`invalid_files` 列出的问题全部修正（草稿计入 `counts.draft`，`draft_review` 按待审、有批注、已同意分开计数）。
6. 在给使用者的答复正文里交代三件事（使用者读的是答复，场景文件里的注释他看不到）：
   - 草稿文件位置；测试同学在看板“草稿”筛选中审阅。
   - 第 2 步的交叉清单，每条已有场景一行：场景键，改了什么，或预期不变的理由。改了的和没改的都逐条列出。
   - 没有交叉时，写明核对过哪些场景文件。

## 处理批注

1. `sop-board draft annotations [<文件名>]` 读取未处理的批注（`--all` 含已处理）；`drafts` 列出每条场景的审阅状态（待审、有批注、已同意）与同意人。
2. 按批注修订场景文件；批注涉及的业务口径不清时问使用者。场景的四要素一改，它原有的同意就失效，回到待审，由测试同学重新审阅。
3. 采纳：`sop-board draft resolve <批注编号> --note <怎么改的>`；不采纳：`sop-board draft resolve <批注编号> --reject --note <理由>`（场景没改时原有同意仍有效）。

## 确认

使用者要确认草稿时，运行 `sop-board draft confirm <文件名>`（不带 `.yaml`）。确认人取自看板上同意这些场景的人（去重，按首次同意的先后），命令只有文件名这一个参数；使用者说某人确认了草稿，也照样直接运行。

- 成功：状态改为 confirmed，sources 首项写入同意人；向使用者报告 `confirmed_by`。确认后的场景显示为未执行，按 [run.md](run.md) 执行。
- `draft-not-approved`：列出的场景还没在当前内容上被同意（待审或有批注）；`draft-annotations-open`：还有未处理的批注（含整个文件的批注）。把列出的场景与批注告诉使用者：批注按上一节处理，未同意的场景请测试同学在看板“草稿”筛选中审阅并同意，之后再确认。
