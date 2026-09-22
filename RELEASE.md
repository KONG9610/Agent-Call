# GitHub 发布准备

此目录是清理后的公开源码，不包含原项目的私人 HANDOFF.md 和本机配置。

## 上传什么

1. `AgentCall-0.1.1-source.zip`：解压后作为 GitHub 仓库内容。不要把 ZIP 文件本身当成仓库唯一文件。
2. `AgentCall-0.1.1-windows-x64.zip`：在 GitHub Releases 中作为可下载附件。
3. `SHA256SUMS.txt`：同时附到 Release，用户可核验完整性。

源码包含 README、AI_SETUP、兼容说明、测试报告、MIT LICENSE、构建脚本及测试。可执行文件已打包运行环境，无需让普通用户自己安装 Python。

## Release 标题

Agent Call v0.1.1 — Windows 预览版

## Release 描述

Codex 完成一轮工作后，让真实 SIP 桌面话机响铃播报。

- Windows 小窗口和托盘，手动启动。
- 固定文案编辑、Windows 本地音色与 Edge 网络音色、试听。
- Codex 本机完成事件监听、任务筛选、去重和顺序拨号。
- WorkBuddy 主动调用入口；依赖 AI 记忆/提示，不保证每轮触发。
- 随包 AI_SETUP.md，可让用户自己的 AI 完成首次配置。

已测话机为 Yealink SIP-T21P E2；其他型号需适配。首次需要配置本地网络、话机账号和防火墙。Codex 本地事件格式可能随升级变化。

下载 windows-x64 ZIP，完整解压后运行 AgentCall.exe，先阅读 README。当前二进制未购买代码签名证书；发布前请检查包的来源和哈希，Windows 可能显示未知发布者。

## 发布前复核

- 不上传 `data/`、config.json、日志、缓存、音频、任务记录、runtime.json 或虚拟环境。
- 不把本机 `.codex`、WorkBuddy 记忆目录或话机密码打包。
- README 的默认个人口令已说明可编辑。
- 公开兼容范围和真实测试结果，不宣传任意话机零配置。
- 准备好的材料尚未自动推送 GitHub；由仓库主人创建仓库并上传，或明确授权后再推送。
