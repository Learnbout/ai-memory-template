---
title: 规则_Git版本控制规范
tags: [rule, memory, git, sync]
summary: 用 git 管理记忆库时的通用约定：仓库位置、提交格式、同步节奏与安全注意事项。
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

## 注意事项

- 不要把本模板仓库 `Learnbout/ai-memory-template` 当自己的记忆库 remote，
  那只是发布模板的工程仓库
- 给 git 配好凭据后，AI 才能在你允许时自动提交
- push 失败时不要阻塞当前工作，记录原因，下次补推
