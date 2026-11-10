# 📱 Termux AI 手机管家 (Android Automation Agent)

基于 Termux + Shizuku + MCP 服务 + DeepSeek 的安卓自动化智能体。

## ✨ 核心特性
- **AI 意图驱动**：使用 DeepSeek 将自然语言转化为可执行的 UI 动作。
- **视觉无关锚点提取**：通过底层交互元素提取 UI 文本特征与坐标，自动清洗动态噪音。
- **指纹固化与智能归位**：采用三级锚点体系，自动判断页面状态并按返回键归位。
- **设备端实时监控**：Web 控制台，实时监控思考过程与 Token 消耗。

## 🧠 核心架构演进 (V2.0 终极版)
1. **AI 智能选片**：提取当前屏幕文字列表发给大模型，大模型根据意图挑选最匹配的选项并返回序号。
2. **坐标精准制导**：直接从底层交互元素提取坐标 `(x,y)`，实现物理点击。
3. **双引擎降级**：遇到 UI 树卡死时，自动切换至 MCP 原生极速通道，绝不挂起。
4. **三层防瞎点熔断**：页面层级打分、特征黑名单、候选序号强校验，没有目标宁可报错也不乱点。
5. **全域自适应**：依靠锚点特征动态生成动作链，不写死特定 App 的逻辑。

## 🚀 环境要求
- **设备**：Android 12+ (需支持 Shizuku)
- **环境**：Termux (Python 3.10+)
- **前置依赖**：
  - [Shizuku](https://shizuku.rikka.app/)
  - [termux-mcp](https://github.com/topjohnwu/termux-mcp) (提供 3000 端口的 MCP 服务)

## 🛠️ 快速开始
### 1. 配置 API Key 与包名
创建配置文件 `~/.config/termux-mcp/env` 并填入你的 DeepSeek Key：
```env
DEEPSEEK_API_KEY=sk-xxxxxx
```
在 `app_packages.json` 中配置你要控制的应用包名映射。

### 2. 启动 Web 控制台
```bash
python web_server.py
```
浏览器访问 `http://127.0.0.1:8080`，即可体验前端控制台。

## ⚠️ 免责声明
本项目仅供技术交流与个人自动化使用。
