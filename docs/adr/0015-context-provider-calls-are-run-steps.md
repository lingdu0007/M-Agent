---
status: accepted
---

# 每次 Context Stage invocation 形成 Context Step

冻结的线性 Context Plan 中，每个由 Scope trigger 产生的 Context Stage invocation 都形成独立 Context Step，并以稳定的 scope、trigger 与 stage identity 定位；输入引用和结构化 Context Stage Result 写入 Run Store 并形成 Checkpoint，重试只新增 Step Attempt，恢复从第一个未完成 Stage 继续。现有单一 Context Provider 等价于只有一个 Provider Stage 的 Plan；该选择增加了 Step 与 Checkpoint 数量，但避免筛选、裁剪、外部读取或动态触发被藏进不可检查的黑盒，并保证同一 invocation 恢复时不因重新获取外部数据而漂移。
