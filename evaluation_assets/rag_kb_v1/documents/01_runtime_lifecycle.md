# Coordinated Runtime Lifecycle

## Admission
AdmissionGate 在新 Run 开始前检查服务是否接受请求。关闭 admission 后，新请求返回 RUNTIME_SHUTTING_DOWN，已经开始的 Run 仍按原生命周期完成。

## Terminal ownership
RunCoordinator 是 terminal owner。每个 Run 最多产生一个 terminal，Transport、Report 和 Shutdown 都不能制造第二个 terminal。

## Output delivery
OutputGate 保证最终输出 at-most-once。状态一旦离开初始态，系统不会自动重发已交付输出。
