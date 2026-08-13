---
status: accepted
---

# Run Metadata 与受保护的 Run Payload 分离

Run Store 把可查询的 Run Metadata 与包含模型内容、Context Item、工具参数及结果的 Run Payload 分离，Payload 必须通过上层应用显式配置的 Payload Codec 后持久化；明文 Codec 只用于本地开发和测试，且不宣称提供静态加密。API Key、访问令牌等凭据始终由 Adapter 从外部配置读取，不进入 Definition Snapshot 或 Run Payload，Trace 默认只记录元数据和脱敏摘要，以便恢复能力不要求运行时假装拥有通用密钥管理方案。
