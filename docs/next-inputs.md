# 星魔接入清单

当前本地演示站已启动，使用模拟流式回复。要切换到客户物理机上的真实 7B 模型，需要补充以下信息：

1. **模型接口 Base URL**：例如 `http://192.168.1.20:8000/v1`。
2. **接口协议**：确认是否兼容 OpenAI `POST /v1/chat/completions`。
3. **真实模型 ID**：例如 `Qwen2.5-7B-Instruct`。该字段只会保存于星魔服务端，不会返回给前台或合作方。
4. **模型接口鉴权**：是否需要 Key；如果需要，提供一个仅供星魔网关调用的服务 Key。
5. **流式支持**：确认请求 `stream: true` 时是否返回 Server-Sent Events。
6. **部署条件**：客户服务器的 Docker Engine / Docker Compose 版本、目标公网域名，以及 HTTPS 证书由谁配置。

## 已固定的对外契约

- 网页聊天：`https://<星魔域名>/chat`
- API Base URL：`https://<星魔域名>/v1`
- 模型名称：`xingmo-chat`
- 鉴权：`Authorization: Bearer <星魔 API Key>`
- 兼容接口：`POST /v1/chat/completions`

客户端、浏览器和合作方都不会接触 NewAPI 管理后台、真实上游模型名、物理机地址或内部服务 Token。
