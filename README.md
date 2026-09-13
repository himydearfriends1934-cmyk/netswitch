# netswitch · 网络控制面板

> 一个为 Debian 13 设计的轻量级网页控制面板，支持有线/无线网络切换、出口 IP 检测、DNS 切换和 APT 镜像源管理。

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Flask](https://img.shields.io/badge/Flask-3.1-green)
![Debian](https://img.shields.io/badge/Debian-13_Trixie-red)
![License](https://img.shields.io/badge/License-MIT-yellow)

## 功能特性

| 功能 | 说明 |
|---|---|
| 🚀 顶部实时出口监控 | 动态显示整机当前上网通道、公网 IP、国家、ISP 及国内/国际双向延迟 |
| 🔌 有线网卡自动发现 | 自动枚举所有物理以太网卡，以序号排列，展示物理协商速率与收发吞吐 |
| 📶 无线 Wi-Fi 列表 | 全面扫描周边 Wi-Fi，序号排列，双频（2.4G/5G）智能识别与高亮 |
| ⚡ 一键连接即出口 | 点击 Wi-Fi 连接成功后自动切换为整机默认出口路由，无需二次点击 |
| 🌟 呼吸灯动态指示 | 当前生效的上网出口网络带有柔和呼吸光晕动态效果，一目了然 |
| 🔍 智能多源出口识别 | 自动容灾探测真实出口国家（带国旗）、城市、公网 IP 与运营商 |
| 🔍 DNS 一键切换 | 国内（阿里云 / 114DNS）↔ 国际（Cloudflare / Google）秒级切换并防篡改加锁 |
| 📦 APT 镜像源一键换 | 清华 TUNA / 中科大 USTC / Debian 官方源一键切换并自动更新索引 |
| 📊 系统硬件与健康监控 | CPU 负载、内存、Swap、磁盘、内核版本、NVIDIA 显卡实时温度/显存/风扇 |

## 系统要求

- Debian 13 Trixie（或兼容 Linux 系统）
- Python 3.10+
- NetworkManager（`nmcli`）
- `curl`、`iproute2`、`iputils-ping`

## 快速安装

```bash
# 一键安装（需要 root 权限）
curl -fsSL https://raw.githubusercontent.com/himydearfriends1934-cmyk/netswitch/main/install.sh | bash
```

安装完成后在浏览器访问：`http://<服务器内网IP或TailscaleIP>:8080`

## 手动安装

```bash
# 1. 安装依赖
apt-get update && apt-get install -y python3 python3-flask git curl iproute2 iputils-ping network-manager

# 2. 克隆仓库
git clone https://github.com/himydearfriends1934-cmyk/netswitch.git /opt/controlpanel

# 3. 注册并启动开机自启服务
cp /opt/controlpanel/netswitch.service /etc/systemd/system/controlpanel.service
systemctl daemon-reload
systemctl enable --now controlpanel.service
```

## 网络接口自适应

`netswitch` 具有全自动网卡嗅探功能：
- 自动枚举系统内的所有有线物理网卡（如 `enp*` / `eth*`），按 1、2、3... 编排。
- 自动识别无线网卡（如 `wlp*` / `wlan*`）并驱动扫描附近热点。无需手工改写配置。
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
