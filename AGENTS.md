# AGENTS.md

This repository is the public distribution template for ai-memory, not a
personal vault. Do not add personal memories or secrets here.

## What changed / where things live

- `template/` is the empty vault skeleton: 4 root pages (记忆索引, 路由索引,
  近期工作动态, 项目状态), domain folders (规则, 项目, 知识, 人设与偏好, 日志),
  and two generic rules in `template/规则/`.
- `server.py` is the MCP server. It recursively scans a vault and auto-maintains
  `记忆索引.md` and `路由索引.md`.
- `docs/使用教程.md` and `README.md` teach end users how to install, configure,
  and use the vault.
- `install.sh` / `install.ps1` copy `template/` plus `server.py` into a new
  memory directory (default `~/ai-memory`).

## Rules for editing this repository

- Keep `template/` free of personal content, real usernames, local absolute
  paths, and machine-specific setup.
- If the file naming rules or routing behavior in `server.py` change, update
  `template/规则/规则_记忆写入格式规范.md` and `docs/使用教程.md` in the same
  change.
- Run `python -m py_compile server.py` after editing server code.
- Prefer Chinese for user-facing docs and rule files; keep code comments in
  English.
