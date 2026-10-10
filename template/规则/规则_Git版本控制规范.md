---
title: 规则_Git版本控制规范
tags: [rule, memory, git, sync]
summary: 用 git 管理记忆库时的通用约定：仓库位置、提交格式、同步节奏与安全注意事项；含隐私信息处理原则（私有库保留、公有库清除）、公有库提交前敏感信息扫描、以及 git push 无限挂死的根因与凭证旁路修法。
---

# Git 版本控制规范

> 索引：[[记忆索引]] ｜ 路由：[[路由索引]]

记忆库由纯 Markdown 组成，天然适合 git 做历史版本和多设备同步。本文件是
通用约定，具体 remote、推送策略请按自己的使用习惯调整。

## 基本约定

- 日常编辑的是本地 Markdown；git 只是历史与同步工具
- 不要在记忆库里提交 token、密码、密钥等敏感信息
- commit 后是否立即 push 由你选择：单人单机可以先 commit 后定期推送，
  多设备协作建议每次提交后立即推送

## 提交格式

```bash
git add -A
git commit -m "{source}: {动作} - {简要说明}"
git push
```

| 项 | 说明 |
| --- | --- |
| source | `codex` / `claude` / `workbuddy` / `cursor`，标识提交者 |
| 动作 | `新增` / `更新` / `修复` / `归档` 等 |
| 示例 | `codex: 更新项目_部署方案 - 完成 K8s 迁移决策` |

## 首次设置

```bash
cd ~/ai-memory
git init
git add -A
git commit -m "chore: 初始化记忆库"

# 在 GitHub 创建你自己的仓库后（公开或私有均可）：
git remote add origin https://github.com/你的用户名/ai-memory.git
git branch -M main
git push -u origin main
```

## 多台电脑

```bash
git clone https://github.com/你的用户名/ai-memory.git ~/ai-memory
```

每次开工前可以 `git pull`；写完后按上面格式提交。遇到冲突时先看远程版本，
本地改动手动合并，不要把冲突文件直接覆盖。

## 隐私信息处理原则

按仓库可见性区分处理：**私有库保留，公有库清除。**

| 仓库类型 | 处理方式 |
| --- | --- |
| 私有仓库（如你的私人备份库） | **不清除隐私数据**，原样保留即可：本机绝对路径、本机用户名、内网 IP、笔记中的设备口令等，方便自己查阅与排错 |
| 公有仓库（公开 GitHub 仓库、公开 gist、公开演示站点） | **必须清除隐私数据**，提交 / 推送前一律清除 |

公有库的清除范围（常见项）：

- 账号级凭据：GitHub token（`ghp_` / `github_pat_` 等）、各类 API key、私钥文件内容
- 本机信息：绝对路径（如 `C:\Users\...`）、本机用户名
- 网络信息：内网 IP 与网段、内网域名、数据库连接串（jdbc / mysql / redis）
- 口令：明文密码、设备 root 口令
- 个人与第三方信息：文档中的真实邮箱、手机号；图片 EXIF / XMP / IPTC 元数据里的作者、邮箱、电话、地址、GPS 坐标
- 统一替换为占位符：`你的密钥`、`%USERPROFILE%`、`<你的内网IP>`、`<你的口令>`、`<你的用户名>`

配套约定：

1. 私有库若要转为公开，**转公开前必须做一次全量清除**，含全部历史提交（必要时重写历史或重建仓库）。
2. 公有库推送前必须跑一次敏感信息扫描，见下节「提交前扫描」。
3. 公开仓库中的图片素材应剥离元数据，避免携带第三方个人信息或拍摄地定位。
4. 例外：GitHub token 等**仓库账号级凭据始终不写入任何仓库**（私有库也不例外），沿用「从本机 secrets 文件读取、推送后立即抹除」的做法。

## 提交前扫描

公有库在 `git push` 前，先跑一次本地敏感信息扫描：

```bash
python "<扫描脚本路径>/scan_secrets.py" "<仓库路径>" --history
```

扫描器覆盖密钥、凭据、本机路径、内网 IP、邮箱、手机号等模式，输出命中行；加 `--history` 连同全部历史提交一起查。图片元数据用同目录的 `exif_scan.py` 复核。

## 推送挂死与凭证旁路

现象：执行 `git push`（沙箱外、网络正常）时**无限挂死**——无超时、无报错，stderr 只打印一行 `Pushing to https://github.com/...` 后静止。curl 访问 `api.github.com` / `github.com/**/info/refs` 均秒回，token 长度正常，可排除网络与凭证本身。

隔离实验（同库同数据、每次只改一处配置，`timeout 30` 记 rc，冷 / 热连接各复现一次）：

| 变体 | 配置 | 结果 |
| --- | --- | --- |
| V1 / V4 | bare `git push`（默认 HTTP/2 + 默认凭证助手） | **rc=124 超时挂死**，无任何输出 |
| V3 / V5 | 加 `-c credential.helper=` 旁路凭证助手 | **rc=0 成功**（`* [new branch]`） |
| V2 | 仅加 `-c http.version=HTTP/1.1` | rc=128，schannel TLS 握手失败 |

结论（根因）：**Git Credential Manager（GCM）在非交互环境下会阻塞等待交互输入**，导致 push 挂死。与 HTTP/2、网络、token 均无关；强制 HTTP/1.1 反而会因 Windows schannel 后端触发 TLS 握手失败。

修法（在 `git push` 命令上做凭证旁路）：

```bash
GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git -c credential.helper= push origin "$BRANCH"
```

三条同时成立的原因：`credential.helper=` 直接旁路 GCM（token 已内嵌在 remote URL，凭证助手本就多余）；两个环境变量保证即便走到交互分支也**立刻失败而非阻塞**。**不要**加 `http.version=HTTP/1.1`。

## 注意事项

- 不要把本模板仓库 `Learnbout/ai-memory-template` 当自己的记忆库 remote，
  那只是发布模板的工程仓库
- 给 git 配好凭据后，AI 才能在你允许时自动提交
- push 失败时不要阻塞当前工作，记录原因，下次补推
