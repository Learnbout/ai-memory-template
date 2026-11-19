# 部署指南（白话版）

> 假设你**不是运维**。每一步都写了「为什么」和「怎么确认它成了」。照着做，别跳步。

## 目录
1. [先选一条路](#一先选一条路)
2. [本地版：自己电脑上常驻](#二本地版自己电脑上常驻)
3. [中心化版：装到 Linux 服务器](#三中心化版装到-linux-服务器)
4. [MCP 接进 AI 客户端](#四mcp-接进-ai-客户端)
5. [备份与升级](#五备份与升级)
6. [安全清单（必读）](#六安全清单必读)
7. [排错速查](#七排错速查)

---

## 一、先选一条路

| 你的情况 | 走哪条 |
|---|---|
| 就我一个人，笔记不出这台电脑 | **本地版**（第二节） |
| 想手机也能看 / 几个人一起用 | **中心化版**（第三节） |

---

## 二、本地版：自己电脑上常驻

最简单：双击安装器 → 走完四步 → 完事。

想命令行跑：

```bash
python -m venv .venv && ./.venv/bin/pip install -r graphite/backend/requirements.txt
echo '{"mode":"local"}' > backend/data/backend.json
./.venv/bin/python graphite/backend/run.py        # 默认 127.0.0.1:8787
```

**Windows 后台常驻（不弹黑框）**：用 `pythonw.exe` 跑 `graphite/backend/run_resident.pyw`，
日志在 `backend/data/resident.log`；想开机自启，把快捷方式丢进 `shell:startup`。

**怎么确认它活着**：浏览器开 `http://127.0.0.1:8787/api/health`，回 `{"ok":true}`。

---

## 三、中心化版：装到 Linux 服务器

以 AlmaLinux / Rocky 系为例；Debian 系把 `dnf` 换 `apt`。

### 3.1 环境

```bash
dnf install -y python3.12 python3.12-pip git
useradd -r -m aim                       # 专用低权限用户，别用 root 跑业务
mkdir -p /opt/ai-memory && chown -R aim:aim /opt/ai-memory
```

### 3.2 代码与依赖

```bash
sudo -u aim git clone <你的仓库> /opt/ai-memory/vault
sudo -u aim python3.12 -m venv /opt/ai-memory/venv
sudo -u aim /opt/ai-memory/venv/bin/pip install -r /opt/ai-memory/vault/graphite/backend/requirements.txt
```

为什么单独建 venv：系统 python 常缺 `pydantic`，缺了的表现不是「起不来」，而是「页面能开、一保存就 500」。

### 3.3 三个服务单元

`/etc/systemd/system/ai-memory-mcp.service`

```ini
[Unit]
Description=ai-memory MCP (HTTP)
After=network-online.target

[Service]
Type=simple
User=aim
Group=aim
WorkingDirectory=/opt/ai-memory/vault
Environment=AI_MEMORY_DIR=/opt/ai-memory/vault
Environment=AI_MEMORY_READONLY=1
ExecStart=/opt/ai-memory/venv/bin/python /opt/ai-memory/vault/脚本/memory_runtime.py --transport http --host 0.0.0.0 --port 8770 --require-token
Restart=on-failure
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/ai-memory-graphite.service`（工作台后端，只听回环）

```ini
[Unit]
Description=ai-memory Graphite backend
After=network-online.target ai-memory-mcp.service

[Service]
Type=simple
User=aim
Group=aim
WorkingDirectory=/opt/ai-memory/vault
Environment=GRAPHITE_VAULT_ROOT=/opt/ai-memory/vault
Environment=GRAPHITE_HOST=127.0.0.1
Environment=GRAPHITE_PORT=8788
Environment=GRAPHITE_MODE=central
Restart=on-failure
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/ai-memory-gate.service`（登录门禁 + 对公入口 8766）

```ini
[Unit]
Description=ai-memory login gate
After=network-online.target ai-memory-graphite.service

[Service]
Type=simple
User=aim
Group=aim
WorkingDirectory=/opt/ai-memory/vault
Environment=AI_MEMORY_DIR=/opt/ai-memory/vault
ExecStart=/opt/ai-memory/venv/bin/python /opt/ai-memory/vault/脚本/web_gate.py --listen 0.0.0.0:8766 --upstream 127.0.0.1:8788
Restart=on-failure
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now ai-memory-mcp ai-memory-graphite ai-memory-gate
systemctl is-active ai-memory-mcp ai-memory-graphite ai-memory-gate    # 三个 active
```

**降权的关键细节（踩过）**：服务以 `User=aim` 跑，库文件的**属组必须是 `aim`**。
否则服务读不到库，报 `sqlite3.OperationalError: unable to open database file`，起来三秒就死。
修法：`chown root:aim /opt/ai-memory/vault/数据库/*.sqlite3 && chmod 640 ...`

### 3.4 防火墙

```bash
firewall-cmd --permanent --add-port=8766/tcp    # 登录门禁（对公）
firewall-cmd --permanent --add-port=8770/tcp    # MCP（给 AI 客户端）
firewall-cmd --reload
# 8788 千万别开：它是内部回环，开了等于绕过登录直接看库
```

只开放「确实有进程在听」的端口。没进程在听的放行规则是纯攻击面。

### 3.5 建第一个管理员

```bash
cd /opt/ai-memory/vault
sudo -u aim /opt/ai-memory/venv/bin/python 脚本/init_admin.py
```

然后浏览器开 `http://<服务器IP>:8766/` → 登录 → 进 `/admin` 管账号与令牌。

---

## 四、MCP 接进 AI 客户端

用安装器最省事（自动扫描 + 一键写配置）。手动的话：

```json
{
  "mcpServers": {
    "ai-memory": {
      "type": "url",
      "url": "http://<服务器IP或本机>:8770/mcp",
      "headers": { "Authorization": "Bearer <你的 token>" }
    }
  }
}
```

本地版不需要 token：地址就是 `http://127.0.0.1:8770/mcp`。

验证：在 AI 里说「用 memory_search 搜一下 部署」。

---

## 五、备份与升级

```bash
cd /opt/ai-memory/vault && ./脚本/backup_db.py      # 生成 数据库/备份/index-<时间>.sqlite3
```

升级：换代码目录 → 重启三个单元 → `systemctl is-active` 三个都 active。
**升级前一定先备份**，出问题换回备份库 + 旧代码目录。

---

## 六、安全清单（必读）

- [ ] **先上登录，再开门**：没登录之前不要把 8788 暴露出去。
- [ ] **8788 只绑 127.0.0.1**，对外只开 8766。
- [ ] **库文件 `640` + 属组留给服务用户**（`chown root:aim`）。
- [ ] **令牌有有效期**，不用就吊销。
- [ ] **对外一律 HTTPS**，不要裸 HTTP 暴露公网。
- [ ] **SSH 保留你的登录方式**，但把 `MaxAuthTries` 降到 3，并装 fail2ban。
- [ ] **只开放有进程在听的端口**。
- [ ] 定期 `pip-audit` / `npm audit`。

---

## 七、排错速查

| 现象 | 大概率原因 | 怎么确认 |
|---|---|---|
| 网页打不开 | 服务没起 / 端口没开 | `systemctl status`、`ss -lnt \| grep 87` |
| 503 记忆库不可用 | 库路径错 / 库文件是 0 字节空壳 | `ls -l 数据库/index.sqlite3` 看大小与属主 |
| 服务起来三秒就死 | `User=` 与库文件属组不匹配 | `journalctl -u <单元> -n 30` 找 `unable to open database file` |
| AI 连 MCP 401 | 令牌与地址不配套 | 设置页复制现成地址与 token，别手抄 |
| 安装器双击没反应 | 缺 WebView2 运行时 | 装 "WebView2 Runtime"；或看 `installer.log` |
| 改完页面没变化 | 浏览器缓存 | 地址后加 `?t=123` 强刷 |
