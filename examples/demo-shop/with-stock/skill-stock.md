
## 库存演示项目的下单与库存

库存演示项目在 api 入口另有下单接口，按 app 仓库 `docs/operations/verification.md` 第 5 节操作。场景起点要求“库存查询不可用”时，对应服务 stock（库存查询与扣减），用 `sop-board env stop stock` 停、`sop-board env start stock` 启；stock 本机未搭建时，依赖库存的场景记未执行。
