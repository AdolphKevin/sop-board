# 浏览器层 spec 保真

适用于产品仓库里已有的浏览器层守护 spec（Playwright，由 regression-distill 沉淀，索引在 `<场景目录>/regression.yaml`，由 `sop-board distill submit` 写入，格式见 [regression-distill 的 report.md](../../regression-distill/references/report.md)）。场景执行会话不写这类 spec；改写或撤销其断言的会话用本检查保证业务断言不被悄悄放宽。静态检查不代替实际运行。

## 检查

```bash
sop-board specs check --repo <仓库别名> <仓内相对路径> [--ledger ledger.json]
```

以 git HEAD 中的同一文件为基线，用 `scripts/spec-ast.mjs` 比较断言与数据核对。输出 JSON：`same`、`new`、`review`、`violation`、`no_baseline`；有 `violation` 时退出码为 1。

| 情况 | 结果 |
| --- | --- |
| 断言签名不变 | `same` |
| 新增断言 | `new`（首次无基线时全部断言列入 `new`，`no_baseline` 为 true） |
| 断言的对象、匹配器、参数或条件变化 | 无账本为 `violation`；有 rewrite 账本为 `review` |
| 断言或数据核对缺失 | 无账本为 `violation`；有 withdraw 账本为 `review` |

`review` 不是业务通过，须由未参与改写的人逐条核对去向。

## 账本

`ledger.json` 是数组，按 `assertion_id` 关联：

```json
[
  {"assertion_id": "A_ROLE", "kind": "rewrite", "reason": "引用已确认的同义文案变更"},
  {"assertion_id": "A_OLD", "kind": "withdraw", "reason": "该预期已被上游删除", "ref": "确认来源位置"}
]
```

- 改写：`reason` 具体写明为什么预期变了；笼统的"跟随 UI"不构成理由。
- 撤销：`reason` 与 `ref` 都必填，`ref` 指向业务预期变更的确认来源。
- 不放宽、删除或改成当前输出来取得通过；账本只记录已确认的变更。

## 断言标识

在断言语句前写 `// sop-assert: A_ROLE`，同一文件内不重复，移动时保留。没有标记时标识为 `auto:<文件>:<类别>:<序号>`，新增或删除断言会让后续序号漂移，所以维护时用显式标识。SQL 字面量核对在显式标识后追加 `:sql`。

```js
// sop-assert: A_ROLE
await expect(page.getByText('Member')).toHaveText('Member');
```

## 改写后

先运行 `specs check` 直到没有 `violation`，再经真实入口实际运行该 spec 并保留新证据；回归关联由 `sop-board distill submit` 登记，不改变验收结论。
