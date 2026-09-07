# ai-memory-template

> 多平台 AI 共享记忆库：一套空的知识库结构 + MCP 服务，克隆即可部署。

![License: MIT](https://img.shields.io/badge/License-MIT-green)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-1.29-green)

很多 AI 工具各自拥有独立的上下文，互相不知道对方做过什么。这个模板把
“长期记忆”做成一堆带规则和索引的 Markdown 文件，并通过 MCP 暴露给所有 AI：
一个工具写入的知识，其他工具能直接读到。

## 特点

- **零外部依赖**：只需 Python 3.10+ 和 `pip install mcp`，不需要数据库或云服务
- **纯文件存储**：全部记忆是 Markdown，可用 Obsidian 浏览、git 做版本管理
- **领域目录 + 路由索引**：先读紧凑的 `路由索引` 定位，再按需读取叶子笔记，
  减少 AI 上下文浪费
- **跨平台、跨 AI**：Codex、Claude Desktop、WorkBuddy、Cursor 等支持 MCP
  stdio 的客户端都能接入
- **自动维护**：自动标签、自动双链、自动更新 `记忆索引` 与 `路由索引`、
  自动归档冷条目

仓库里不包含任何个人内容，只有可开箱使用的空知识库结构、通用规则、MCP
服务与教学文档。

## 快速开始（约 3 分钟）

Linux / macOS：

```bash
git clone https://github.com/Learnbout/ai-memory-template.git
cd ai-memory-template
bash install.sh            # 默认在 ~/ai-memory 创建记忆库
```

Windows PowerShell：

```powershell
git clone https://github.com/Learnbout/ai-memory-template.git
cd ai-memory-template
.\install.ps1              # 默认在 %USERPROFILE%\ai-memory 创建记忆库
```

也可以把自定义目录传给脚本，例如 `bash install.sh ~/my-brain` 或
`.\install.ps1 -TargetDir D:\my-brain`。

脚本会安装 `mcp`、创建知识库、复制 `server.py`，并在结尾打印各 AI 工具的
MCP 配置。把其中一段粘进你的 AI 工具配置后重启即可。

想让 AI 自己完成部署？把下面这段话发给任意一个 AI：

```text
请帮我部署 ai-memory-template。仓库地址：
https://github.com/Learnbout/ai-memory-template
1. 克隆或下载项目
2. 安装依赖：pip install mcp
3. 在我的电脑上创建 ~/ai-memory 记忆库（Windows 用户用 %USERPROFILE%\ai-memory）
4. 复制 template 目录内容与 server.py 到记忆库
5. 按我的操作系统配置 MCP，让我可以用 memory_write / memory_search 等工具
6. 最后告诉我怎么验证，并读一下记忆库里的规则文档
```

## 验证

配置好后，在任意 AI 对话里执行：

- `memory_stats`：看到记忆库健康统计
- `memory_list`：看到 记忆索引 / 路由索引 / 近期工作动态 / 项目状态 等初始页
- `memory_write`：写一条测试记忆（例如“我刚刚部署好了 ai-memory”）
- `memory_search`：搜索刚才写的内容

也可以直接用 Obsidian 打开记忆库目录，查看双链图谱。

## 初始知识库结构

```text
ai-memory/
├─ 记忆索引.md            # MOC：自动维护，按类型分组的全量索引
├─ 路由索引.md            # 自动维护：标题 | 目录 | 日期 | 标签 | 摘要
├─ 近期工作动态.md        # 只留最近 7 天，超期滚入 日志/
├─ 项目状态.md            # 各项目状态总表
├─ 规则/                  # 规则_*：长期使用与写入规范
├─ 项目/                  # 项目_*：按项目主题的长期笔记
├─ 人设与偏好/            # 用户画像、AI人格 等身份与偏好
├─ 知识/                  # 知识_*：调研、对比、排障等知识叶子
├─ 日志/                  # 工作动态_YYYY-MM.md：月度历史时间线
└─ server.py              # ai-memory MCP server
```

新笔记写入时会按标题前缀自动路由到对应目录：

| 标题前缀或标题 | 目标目录 |
| --- | --- |
| `规则_` | `规则/` |
| `项目_` | `项目/` |
| `知识_` | `知识/` |
| `用户画像` / `AI人格` | `人设与偏好/` |
| `日志_` / `工作动态_` / 日期开头 | `日志/` |
| 其他 | 库根 |

## 提供的 MCP 工具（20 个）

| 分类 | 工具 |
| --- | --- |
| 读写 | `memory_write` `memory_read` `memory_delete` `memory_update_metadata` |
| 查询 | `memory_search` `memory_smart_search` `memory_list` `memory_recent` `memory_stats` |
| 图谱 | `memory_graph` `memory_orphans` `memory_rebuild_links` |
| 维护 | `memory_archive` `memory_restore` `memory_archive_old` `memory_batch_tag` `memory_batch_tier` `memory_heat_suggest` |
| 审计 | `memory_audit` `memory_index_draft` |

## 文档

- [使用教程](docs/使用教程.md)：从安装到日常维护的完整教学
- [写入规则](template/规则/规则_记忆写入格式规范.md)：AI 写记忆时遵守的规范
- [Git 规则](template/规则/规则_Git版本控制规范.md)：记忆库版本控制约定

## 自定义

默认记忆库路径是 `~/ai-memory`。想用自己的目录时，设置环境变量
`AI_MEMORY_DIR` 再启动 server：

```bash
AI_MEMORY_DIR=/path/to/my-vault python server.py
```

自动标签映射、核心页清单、目录路由都集中在 `server.py` 顶部附近，方便按需
调整。改 `server.py` 后记得同步修改 `template/规则/` 与教学文档，让规则与
实现保持一致。

## License

MIT
