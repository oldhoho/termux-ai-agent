# 📱 Termux AI 手机管家 (Android Automation Agent)

基于 Termux + Shizuku + MCP 服务 + DeepSeek 的安卓自动化智能体。

## ✨ 核心特性
- **大模型驱动**：使用 DeepSeek 进行意图识别。
- **视觉无关锚点提取**：通过无障碍树（XML）提取 UI 文本特征与功能坐标。
- **指纹固化与归位**：采用 A/B/C 三级锚点体系，若不在主页则自动按返回键归位。
- **设备端端侧监控**：Web 控制台，在手机上实时监控大模型思考过程与 Token。
- **资产沉淀**：支持智能体将成功执行的动作序列自动沉淀为宏和元素图纸。

## 🚀 环境要求
- **设备**：Android 12+ (需支持 Shizuku)
- **环境**：Termux (Python 3.10+)
- [Shizuku](https://shizuku.rikka.app/)
- [termux-mcp](https://github.com/topjohnwu/termux-mcp)
- `rish` 软链接

## 🛠️ 快速开始
### 1. 配置 API Key
创建 `~/.config/termux-mcp/env` 并填入 DEEPSEEK_API_KEY=sk-xxxxxx

### 2. 启动服务
确保 termux-mcp 的 3000 端口正常运行。
运行 `python web_server.py`，访问 http://127.0.0.1:8080

## ⚠️ 免责声明
本项目仅供技术交流与个人自动化使用。请勿用于违反服务条款或法律的行为。
