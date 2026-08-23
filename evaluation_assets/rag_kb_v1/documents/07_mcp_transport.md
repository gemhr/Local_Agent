# Model Context Protocol Transport

## Name
MCP 的全称是 Model Context Protocol，用于模型客户端与外部工具或资源服务之间的标准交互。

## Boundary
MCP transport 负责协议传输，不拥有 AgentState、Run terminal 或 Evaluation Result。

## Failure
连接失败应保留明确 transport error，不应静默切换到未授权工具实现。
