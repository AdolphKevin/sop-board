# 浏览器层 spec 保真

适用于产品仓库里已有的浏览器层守护 spec（Playwright，由 regression-distill 沉淀，索引在 `<场景目录>/regression.yaml`，由 `sop-board distill submit` 写入，格式见 [regression-distill 的 report.md](../../regression-distill/references/report.md)）。场景执行会话不写这类 spec；改写或撤销其断言的会话用本检查保证业务断言不被悄悄放宽。静态检查不代替实际运行。

## 检查

```bash
sop-board specs check --repo <仓库别名> <仓内相对路径> [--ledger ledger.json]
```

以 git HEAD 中的同一 spec 连同它的本地导入（`./fixtures.mjs`、`./library.mjs` 及其导入）为基线，用 `scripts/spec-ast.mjs` 比较断言与数据核对；夹具与库文件也要提交，基线才完整。输出 JSON：`same`、`new`、`review`、`violation`、`no_baseline`、`baseline`；有 `violation` 时退出码为 1。

spec 还没进 git HEAD 的首次沉淀由 `sop-board distill submit` 检查：基线是产生独立通过的那次运行目录里覆盖该场景的 spec（按场景认，不按文件名），基线本身不合[导入规则](web.md#spec-的导入)时拒绝并指出基线。

首次比较把两边的辅助文件路径都按各自 spec 所在目录识别，改名或搬到 spec 库的其他子目录不改变断言身份。spec 直接导入的 `./fixtures.mjs` 是可替换的运行适配接口，其依赖（随包 connection、recording、scope、identity、atlas 和运行材料）不进入首次断言签名；这按导入边界识别，不按全局文件名过滤。`library.mjs` 及其可达本地导入仍完整比较，包括同名的 scope.mjs 等文件。删除、削弱 spec 断言或改变项目查询仍须账本；首次检查不证明替换夹具自身的正确性，夹具必须保持真实 expect、执行步骤并传播失败，按 [守护命令契约](../../regression-distill/references/report.md#commands) 实际运行并核对检出能力。HEAD 已有 spec 时仍比较其完整依赖，夹具变化照常查出。

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

先运行 `specs check` 直到没有 `violation`，再经真实入口实际运行该 spec 并保留新证据。测试文件一改，原守护随即失效（摘要不同）；按 [regression-distill](../../regression-distill/SKILL.md) 带账本重新 `sop-board distill submit` 登记。
