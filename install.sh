#!/bin/bash
# netswitch — One-click installer for Debian 13
set -e

REPO="https://github.com/himydearfriends1934-cmyk/netswitch"
INSTALL_DIR="/opt/controlpanel"
SERVICE_NAME="controlpanel"
PORT="8080"

echo "╔══════════════════════════════════════╗"
echo "║   netswitch 控制面板 安装程序         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "❌ 请以 root 身份运行此脚本"
    exit 1
fi

echo "📦 安装依赖..."
apt-get update -q
apt-get install -y python3 python3-flask git curl iproute2 iputils-ping network-manager

echo "📁 下载控制面板..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "   已存在，执行 git pull 更新..."
    git -C "$INSTALL_DIR" pull
else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO" "$INSTALL_DIR"
fi

PY=$(which python3)
echo "🐍 Python: $PY"

echo "⚙️  配置 systemd 服务..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=HOMEHOSTINGSER Control Panel (netswitch)
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
ExecStart=${PY} ${INSTALL_DIR}/app.py
WorkingDirectory=${INSTALL_DIR}
Restart=on-failure
RestartSec=10
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ${SERVICE_NAME}.service

sleep 3

STATUS=$(systemctl is-active ${SERVICE_NAME}.service)
if [ "$STATUS" = "active" ]; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "✅ 安装成功！控制面板已启动"
    echo ""
    echo "   访问地址: http://${IP}:${PORT}"
    echo "   (或通过 Tailscale IP / 内网 IP 访问)"
    echo ""
    echo "   服务状态: systemctl status ${SERVICE_NAME}"
    echo "   查看日志: journalctl -u ${SERVICE_NAME} -f"
    echo "   停止服务: systemctl stop ${SERVICE_NAME}"
else
    echo "❌ 服务启动失败，查看日志："
    journalctl -u ${SERVICE_NAME} -n 20 --no-pager
    exit 1
fi
