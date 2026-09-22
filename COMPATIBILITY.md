# 兼容性与边界

## 已知可用基线

- Windows 11 x64；Windows 10 x64 作为目标系统，未在独立 Windows 10 机器验收。
- Yealink SIP-T21P E2，固件 52.82.0.20，SIP UDP + G.711 PCMA。
- 本机 Codex 任务记录包含 `session_meta` 及 `event_msg/task_complete`。
- Windows SAPI 与 Edge TTS 代码均源自已跑通的 Vibe Phone。

发布测试结果见 `TEST_REPORT.md`，不把继承的测试记录当作当前构建已经通过。

## 不承诺

- 所有 SIP/IP 话机都能按相同方式自动接听。
- 所有 Codex 版本、远程云任务、禁用本地记录的任务均被监听。
- WorkBuddy 模型每次都记得执行通知命令。
- Edge 非官方接口永久可用。
- 电脑休眠、断网、程序退出、话机离线时继续完成来电。
- 停机期间完成的任务恢复后补打。本版本有意不补打。
- 对话语音输入、VB-CABLE、ASR、多分机同时拨号。本版为单个目标分机串行通知。

## 升级 Codex 后的适配核查

检查最近 JSONL 的外层 type 与 payload.type，确认仍有带 turn_id 的 task_complete；不需要读取或上传用户对话正文。若结构改变，更新 monitor.py 和回归测试，并做真实任务验收。当前实现不利用“最后一条 assistant 文本”猜测任务结束。
