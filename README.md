# ai-memory-template

> 多平台 AI 共享记忆库：一套空的知识库结构 + MCP 服务，克隆即可部署。

![License: MIT](https://img.shields.io/badge/License-MIT-green)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-1.x%20%7C%202.x-green)

很多 AI 工具各自拥有独立的上下文，互相不知道对方做过什么。这个模板把
“长期记忆”做成一堆带规则和索引的 Markdown 文件，并通过 MCP 暴露给所有 AI：
一个工具写入的知识，其他工具能直接读到。

## 特点

- **零外部依赖**：只需 Python 3.10+ 和 `pip install mcp`，不需要数据库或云服务
- **纯文件存储**：全部记忆是 Markdown，可用 Obsidian 浏览、git 做版本管理
- **md 为唯一事实源 + SQLite 只读快照**：索引库可随时从 md 全量重建（`脚本/sync_all.py`），
  `memory_sql` 用只读 SQL 窄查询取摘要/计数，比拉整篇全文省 99% 上下文
- **热重载**：supervisor/worker 双进程，改代码后 `脚本/reload_mcp.py` 写哨兵即平滑替换，
  宿主连接不断
- **SDK 兼容层**：`mcp` 包 v1.x 与 v2.x 双分支适配，升级 SDK 无痛迁移
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
4. 复制 template 目录内容、server.py 与 memory_*.py / mcp_compat.py / 脚本 目录到记忆库
5. 按我的操作系统配置 MCP，让我可以用 memory_write / memory_search 等工具
6. 最后告诉我怎么验证，并读一下记忆库里的规则文档
```

## 验证

配置好后，在任意 AI 对话里执行：

- `memory_stats`：看到记忆库健康统计
- `memory_list`：看到 记忆索引 / 路由索引 / 近期工作动态 / 项目状态 等初始页
- `memory_write`：写一条测试记忆（例如“我刚刚部署好了 ai-memory”）
- `memory_search`：搜索刚才写的内容
- `memory_sql`：`SELECT title, summary FROM notes` 用 SQL 直接查索引快照

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
├─ 脚本/                  # sync_all / check_frontmatter / reload_mcp / probe_hot_reload
├─ server.py              # 入口（8 行）：转发到 memory_runtime.main
├─ memory_runtime.py      # MCP runtime + supervisor 热重载 + 工具注册
├─ memory_store.py        # 文件扫描 / frontmatter / 缓存 / 文件锁 / SQLite 索引
├─ memory_tools.py        # 22 个 MCP 工具的实现
├─ mcp_compat.py          # mcp SDK 兼容层（v1.x / v2.x 双分支）
├─ smoke_test.py          # 11 项冒烟测试
└─ .workbuddy/
   └─ index.sqlite3       # SQLite 只读快照（自动生成，可随时删掉重建）
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

## 提供的 MCP 工具（22 个）

| 分类 | 工具 |
| --- | --- |
| 读写 | `memory_write` `memory_read` `memory_delete` `memory_update_metadata` |
| 查询 | `memory_search` `memory_smart_search` `memory_list` `memory_recent` `memory_sql` `memory_context` `memory_stats` |
| 图谱 | `memory_graph` `memory_orphans` `memory_rebuild_links` |
| 维护 | `memory_archive` `memory_restore` `memory_archive_old` `memory_batch_tag` `memory_batch_tier` `memory_heat_suggest` |
| 审计 | `memory_audit` `memory_index_draft` |

默认全部暴露。设置环境变量 `AI_MEMORY_CORE_TOOLS=1` 只启用 6 个高频核心工具
（search / read / write / stats / sql / context），把每轮对话占用的工具 schema
压到最小；去掉该变量重启即恢复全部。

其他可调环境变量：

| 变量 | 作用 |
| --- | --- |
| `AI_MEMORY_DIR` | 记忆库目录（默认 `~/ai-memory`） |
| `AI_MEMORY_NO_RELOAD=1` | 关闭热重载 supervisor，单进程直跑 |
| `AI_MEMORY_CORE_TOOLS=1` | 只注册 6 个核心工具 |

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

自动标签映射、核心页清单、目录路由集中在各模块顶部附近，方便按需调整：
工具行为在 `memory_tools.py`，存储/索引在 `memory_store.py`，启动与热重载在
`memory_runtime.py`。改完可跑 `python smoke_test.py` 自检，热重载用
`python 脚本/reload_mcp.py` 触发。改代码后记得同步修改 `template/规则/` 与
教学文档，让规则与实现保持一致。

## 致谢

本项目的结构、规则与代码设计参考了
[dpkg-s/ai-memory-template](https://github.com/dpkg-s/ai-memory-template)
的早期版本，感谢原作者 dpkg-s 的贡献。当前仓库由 Learnbout 维护与重构，
完整名单见 [CONTRIBUTORS.md](CONTRIBUTORS.md)。

## License

MIT
