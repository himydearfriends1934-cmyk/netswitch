# netswitch · 网络控制面板

> 一个为 Debian 13 设计的轻量级网页控制面板，支持有线/无线网络切换、出口 IP 检测、DNS 切换和 APT 镜像源管理。

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Flask](https://img.shields.io/badge/Flask-3.1-green)
![Debian](https://img.shields.io/badge/Debian-13_Trixie-red)
![License](https://img.shields.io/badge/License-MIT-yellow)

## 功能特性

| 功能 | 说明 |
|---|---|
| 🌐 网络接口管理 | 有线 / 无线独立开关，一键设为默认出口路由 |
| 🔍 出口 IP 检测 | 实时查询真实出口 IP、国家、城市、ISP，国内/国际延迟测试 |
| 📶 Wi-Fi 批量配置 | 扫描周边网络，批量输入密码，系统逐一检测出口属性，点击切换 |
| 🔍 DNS 切换 | 国内（阿里云/114DNS）↔ 国际（Cloudflare/Google/Quad9）一键切换 |
| 📦 APT 镜像源 | 清华 TUNA / 中科大 USTC / Debian 官方 三源切换，自动 apt update |
| 📊 系统监控 | 内存、磁盘、Swap、负载、内核、NVIDIA GPU 状态实时显示 |

## 系统要求

- Debian 13 Trixie（或兼容系统）
- Python 3.10+
- NetworkManager（`nmcli`）
- `curl`、`ping`、`ip` 命令

## 快速安装

```bash
# 一键安装（需要 root）
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/netswitch/main/install.sh | bash
```

安装完成后访问：`http://<你的服务器IP>:8080`

## 手动安装

```bash
# 安装依赖
apt-get install -y python3 python3-flask

# 克隆仓库
git clone https://github.com/himydearfriends1934-cmyk/netswitch.git /opt/controlpanel

# 安装 systemd 服务
cp /opt/controlpanel/netswitch.service /etc/systemd/system/controlpanel.service
systemctl daemon-reload
systemctl enable --now controlpanel.service
```

## 网络接口配置

默认配置针对以下硬件，如需修改请编辑 `app.py` 顶部的 `IFACES` 字典：

```python
IFACES = {
    "wired":    "enp0s31f6",   # Intel I219-V 千兆以太网
    "wireless": "wlp6s0",     # MediaTek MT7921 Wi-Fi 6
}
```

## Wi-Fi 批量检测原理

1. 点击「扫描周边网络」，显示附近所有 SSID
2. 在密码框输入各网络密码（留空 = 开放网络）
3. 点击「全部检测」，系统在后台依次连接每个网络：
   - 通过真实接口查询出口 IP（via `ip-api.com`）
   - Ping 测国内延迟（`223.5.5.5`）和国际延迟（`1.1.1.1`）
4. 检测完成后显示结果表格，**点击任意行即切换至该网络**
5. 全程有线网络保持在线，控制面板不会断连

## 安全说明

- 服务以 `root` 身份运行（需要操作网络接口）
- 建议仅在内网 / Tailscale 网络下使用，不要直接暴露到公网
- 可配合防火墙限制 8080 端口访问来源

## License

MIT
