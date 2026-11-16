# DeepShare · DeepSeek 分享抓取记忆库

> 把 DeepSeek Chat 的分享链接一键存成本地 Markdown 记忆库，离线也能搜索和回看。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Streamlit](https://img.shields.io/badge/streamlit-%E2%89%A51.49-ff4b4b)
![Playwright](https://img.shields.io/badge/playwright-%E2%89%A51.40-green)

## ✨ 功能

- **粘贴链接即抓取**：复制 DeepShare 链接 → 粘贴 → 一键抓取。Playwright 自动处理虚拟列表滚动、懒加载、重试。
- **本地 Markdown 存储**：每条记忆自动落盘到 `my_memories/` 目录，带 YAML front matter 记录标题、来源、抓取时间。
- **智能去重**：同一分享链接重复抓取会自动覆盖旧文件，不会堆积副本。
- **全文搜索**：侧边栏关键词实时过滤，支持按标题和正文匹配。
- **AI 一句话总结**：接入任意 OpenAI 兼容接口（默认 DeepSeek），一键为长对话生成 120-220 字摘要，写回文件持久保存。
- **暗色 / 亮色 / 琥珀三主题**：侧边栏下拉切换，自动持久化。
- **零后端、零账号**：纯本地 Streamlit 应用，不联网数据库，不收集任何数据。

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

> 如果 `playwright install` 下载浏览器慢，可以用 `playwright install chromium --with-deps`（Linux）或指定镜像 `PLAYWRIGHT_DOWNLOAD_HOST=...`。

### 2. 启动应用

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。

### 3. 使用

1. 在 DeepSeek Chat 里点右上角「分享」，复制生成的链接（形如 `https://chat.deepseek.com/share/xxx`）。
2. 打开 DeepShare，在左侧粘贴链接，点 **「开始抓取」**。
3. 抓取完成后自动渲染为 Markdown，右侧即可搜索、浏览、收藏。

### 4. 开启 AI 总结（可选）

1. 侧边栏「✨ AI 总结 → 接口配置」填入 **API Key**（DeepSeek 平台申请，`sk-` 开头）。
2. Base URL 默认 `https://api.deepseek.com`，模型默认 `deepseek-chat`；换成任何 OpenAI 兼容端点（Kimi、Qwen、本地 Ollama 等）也可以。
3. 点「保存配置」，然后在任意记忆卡片上点 **「✨ AI 总结」**，摘要会写入该 Markdown 的 front matter，下次打开直接显示。

> API Key 只保存在本机 `.deepshare_ai.json`，该文件已被 `.gitignore` 排除，不会随代码上传。

## 🗂 目录结构

```
.
├── app.py                 # 主程序（抓取内核 + Streamlit UI）
├── requirements.txt       # 依赖清单
├── .streamlit/
│   └── config.toml        # 主题配置（可改 base="light"）
├── my_memories/           # 抓取结果（自动创建，已被 .gitignore 忽略）
└── debug_artifacts/       # 调试产物（抓取失败时自动保存 HTML/网络响应）
```

## 🛠 技术栈

| 组件 | 选型 |
|---|---|
| Web 框架 | Streamlit |
| 抓取引擎 | Playwright（同步模式，Chromium） |
| AI 总结 | OpenAI 兼容 `/chat/completions`（默认 DeepSeek，标准库 urllib 调用，零额外依赖） |
| 存储 | 本地 Markdown + YAML front matter |
| 内容清洗 | 自研 DOM / 接口双重降级抓取 + THINK 片段识别 |

## 🔍 抓取架构（为什么稳）

DeepShare 采用 **阶梯式降级** 设计，任何一层成功即返回，不会因单点失败崩溃：

| 层级 | 策略 | 用途 |
|---|---|---|
| 第 1 层 | DOM 选择器多路探测 | 快速、轻量 |
| 第 1.5 层 | 页面内嵌 `__NEXT_DATA__` / `self.__next_f` | SSR 直出 |
| 第 2 层 | Network 监听抓 API JSON | 兜底接口方案 |
| 第 3 层 | 重试 + 主站预热 + 虚拟列表滚动 + 有头浏览器 | 最难的情况 |

关键细节：
- **虚拟列表整页滚动**（`_tier_sweep`）：长对话不会因为只渲染可见区而截断，临时拉高视口到 4800px，总高度连续两轮不变才判定"到底"。
- **接口树断链容错**：当 API 返回的消息树出现 `parent_id` 指向缺失节点时，放弃树还原，滚动方案优先。
- **内容校验兜底**（`_content_problem`）：检测"分享失效"、"资源加载异常"、消息数 < 2、总字 < 60 等假成功，确保无效链接不会入库。

## 🎨 主题

侧边栏顶部下拉可切换三种风格：**🌙 赛博 Dark**（默认）、**☀️ 清爽 Light**、**🟠 琥珀 Amber**。选择后即时生效，并写入 `.streamlit/config.toml` 与 `[deepshare] theme_key` 持久化，下次启动自动恢复。

## 📝 License

MIT — 随取随用，二次修改随意。
