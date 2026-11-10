# 验收场景草稿：生成、批注与确认

草稿是场景目录下 `metadata.status: draft` 的场景 YAML。草稿不进入执行（`run prepare` 返回 `draft-not-runnable`），确认后才进入运行范围。

## 生成草稿

1. `sop-board checkout show` 的 `checkout.scenarios` 是场景目录；已登记的 SOP（`.md`）和现有场景文件都在这里，现有场景文件是最直接的样例。
2. 依据 SOP 写 `<场景目录>/<名称>.yaml`。写法遵循 sop-board 仓库 [docs/sop-guide.md](https://github.com/AdolphKevin/sop-board/blob/main/docs/sop-guide.md) 的“四要素”“场景粒度”“正向与反向”三节：预期只来自 SOP 原文和使用者的答复；SOP 没写明的结果在对话中问使用者，代码现状不当作预期；每个节点至少一条正向与一条反向场景，确实没有反向时写 `negative_exempt` 理由。
3. 结构如下。`status: draft` 单独一行、`sources:` 用块格式，`draft confirm` 依赖这两点：

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

4. 运行 `sop-board status --json`，`invalid_files` 列出的问题全部修正（草稿计入 `counts.draft`）。告诉使用者草稿位置，测试同学可在看板“草稿”筛选中批注。

## 处理批注

1. `sop-board draft annotations [<文件名>]` 读取未处理的批注（`--all` 含已处理）。
2. 按批注修订场景文件；批注涉及的业务口径不清时问使用者。
3. 采纳：`sop-board draft resolve <批注编号> --note <怎么改的>`；不采纳：`sop-board draft resolve <批注编号> --reject --note <理由>`。

## 确认

使用者确认草稿内容后，运行 `sop-board draft confirm <文件名>`（不带 `.yaml`）：状态改为 confirmed，并在 sources 写入确认人（操作者；使用者说明是别人确认的，加 `--as <姓名>`）。返回 `draft-annotations-open` 时先处理列出的批注。确认后的场景显示为未执行，按 [run.md](run.md) 执行。
