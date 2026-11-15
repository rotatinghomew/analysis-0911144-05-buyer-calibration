# 本地接口约定

所有接口使用 UTF-8 JSON，错误响应为带 error 字段的对象。GET /health 返回进程健康状态。POST /imports 接收 request_key 与 data，data 遵循业务资料契约；新导入返回 201，完全相同的请求重放返回 200。相同请求号对应不同资料返回 409；非法资料返回 400，不留下部分版本。

GET /revisions 列出 revision 和内容摘要。GET /records 返回业务原始记录；GET /summary 返回已有概览，两者都返回 revision、digest 和 result。可加 revision 查询参数固定历史资料，不提供时读取最新资料；资料不存在为 404。摘要对对象键顺序不敏感，对数组顺序敏感；资料内容相同可共享版本，导入请求回执相互独立。

未知路径为 404。现有客户端依赖这些路径和包装字段；后续增加业务分析接口时保留既有行为，不能把资料导入成功等同于业务分析已经形成结论。每个分析产物应声明所用数据版本，不能在客户端读取时偷偷替换成最新资料。

## 校准分析

GET /calibration 在指定版本上运行边际校准，返回与 /summary 相同的外层包装（revision、digest、result）。revision 参数缺省时读取最新资料，但产物内部同时写入 data_digest 与 analysis_digest；之后导入新资料不会改变已返回结果。可加 settings 查询参数，值是 URL 编码的 JSON 对象，字段见 docs/data-contract.md。settings 非法返回 400，版本不存在返回 404；约束冲突不是请求错误，仍在 200 的 result.diagnosis 中说明。

POST /analyses 把一次分析固化为可复核记录，正文为 {request_key, settings}，可用 revision 查询参数固定资料版本。新记录返回 201，同一请求号重放相同产物返回 200，同一请求号对应不同产物返回 409；版本不存在为 404。回执含 analysis_id、revision、data_digest、analysis_digest、settings_digest。服务不会用最新资料替换记录中的版本，也不会在重放时重新计算。

GET /analyses 列出已保存分析（可加 revision 过滤），每项含 analysis_id、revision、digest、settings_digest。GET /analyses/{id} 返回该记录，含保存时的 settings 与完整 result；不存在为 404。分析记录与资料版本同样持久化，重启后可按编号取回。
