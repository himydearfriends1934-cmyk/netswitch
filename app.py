#!/usr/bin/env python3
"""
HOMEHOSTINGSER Control Panel — netswitch
Auto-detects all wired interfaces; shows all WiFi networks as numbered list.
Accurately identifies active connected WiFi even across dual-band (2.4G/5G).
"""
import subprocess, json, re, time, threading, uuid
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

DNS_PRESETS = {
    "domestic": {"label":"国内 DNS","servers":["223.5.5.5","223.6.6.6","114.114.114.114"],"note":"阿里云 / 114DNS"},
    "overseas": {"label":"国际 DNS","servers":["1.1.1.1","8.8.8.8","9.9.9.9"],"note":"Cloudflare / Google / Quad9"},
}
MIRROR_PRESETS = {
    "domestic":{"label":"清华 TUNA","url":"mirrors.tuna.tsinghua.edu.cn","content":
        "deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie main contrib non-free non-free-firmware\n"
        "deb http://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main contrib non-free non-free-firmware\n"
        "deb http://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main contrib non-free non-free-firmware\n"},
    "ustc":{"label":"中科大 USTC","url":"mirrors.ustc.edu.cn","content":
        "deb http://mirrors.ustc.edu.cn/debian trixie main contrib non-free non-free-firmware\n"
        "deb http://mirrors.ustc.edu.cn/debian trixie-updates main contrib non-free non-free-firmware\n"
        "deb http://mirrors.ustc.edu.cn/debian-security trixie-security main contrib non-free non-free-firmware\n"},
    "overseas":{"label":"Debian 官方","url":"deb.debian.org","content":
        "deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware\n"
        "deb http://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware\n"
        "deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware\n"},
}
COUNTRY_FLAGS = {
    "CN":"🇨🇳","US":"🇺🇸","HK":"🇭🇰","JP":"🇯🇵","SG":"🇸🇬",
    "DE":"🇩🇪","GB":"🇬🇧","FR":"🇫🇷","AU":"🇦🇺","KR":"🇰🇷",
    "TW":"🇹🇼","NL":"🇳🇱","CA":"🇨🇦","RU":"🇷🇺","IN":"🇮🇳",
}
_tasks = {}

def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"

def get_all_wired_ifaces():
    code, out, _ = sh("ip -j link show")
    if code != 0 or not out:
        return ["enp0s31f6"]
    try:
        links = json.loads(out)
        return [l["ifname"] for l in links
                if re.match(r"^(en|eth|eno|ens|enp|enx)", l.get("ifname",""))
                and l.get("ifname") != "lo"]
    except:
        return ["enp0s31f6"]

def get_all_wireless_ifaces():
    code, out, _ = sh("ip -j link show")
    if code != 0 or not out:
        return ["wlp6s0"]
    try:
        links = json.loads(out)
        return [l["ifname"] for l in links
                if re.match(r"^(wl|wlan|wifi)", l.get("ifname",""))]
    except:
        return ["wlp6s0"]

# Global traffic rate tracker: iface -> (last_time, last_rx, last_tx, rx_rate, tx_rate)
_traffic_history = {}

def iface_info(name):
    code, out, _ = sh(f"ip -j addr show {name}")
    if code != 0 or not out:
        return {"name":name,"state":"missing","ip":None,"mac":None,"speed":None,"rx_bytes":0,"tx_bytes":0,"rx_rate":0,"tx_rate":0}
    try:
        data = json.loads(out)[0]
    except:
        return {"name":name,"state":"error","ip":None,"mac":None,"rx_bytes":0,"tx_bytes":0,"rx_rate":0,"tx_rate":0}
    state = "up" if data.get("operstate") == "UP" else "down"
    ip = next((f"{a['local']}/{a['prefixlen']}" for a in data.get("addr_info",[]) if a.get("family")=="inet"), None)
    _, spd, _ = sh(f"cat /sys/class/net/{name}/speed 2>/dev/null")
    _, rx, _  = sh(f"cat /sys/class/net/{name}/statistics/rx_bytes 2>/dev/null")
    _, tx, _  = sh(f"cat /sys/class/net/{name}/statistics/tx_bytes 2>/dev/null")

    rx_b = int(rx) if rx.isdigit() else 0
    tx_b = int(tx) if tx.isdigit() else 0

    # Calculate real-time throughput rates (bytes/sec)
    now = time.time()
    rx_rate = 0.0
    tx_rate = 0.0
    if name in _traffic_history:
        prev_time, prev_rx, prev_tx, _, _ = _traffic_history[name]
        dt = now - prev_time
        if dt > 0.4:
            rx_rate = max(0.0, (rx_b - prev_rx) / dt)
            tx_rate = max(0.0, (tx_b - prev_tx) / dt)
    _traffic_history[name] = (now, rx_b, tx_b, rx_rate, tx_rate)

    return {
        "name": name, "state": state, "ip": ip, "mac": data.get("address"),
        "speed": f"{spd} Mbps" if spd.isdigit() else None,
        "rx_bytes": rx_b,
        "tx_bytes": tx_b,
        "rx_rate": rx_rate,
        "tx_rate": tx_rate,
    }

def get_all_wifi_networks(wireless_iface="wlp6s0"):
    # Saved connections
    _, saved_out, _ = sh("nmcli -t -f NAME,TYPE con show | grep wireless")
    saved_ssids = set()
    for line in saved_out.splitlines():
        parts = line.split(":")
        if parts:
            saved_ssids.add(parts[0].strip())

    # Active connection SSID directly from nmcli dev status
    _, act_out, _ = sh(f"nmcli -t -f DEVICE,STATE,CONNECTION dev | grep '^{wireless_iface}:connected:'")
    active_con_name = act_out.split(":")[-1].strip() if act_out else ""

    _, out, _ = sh(f"nmcli -t -f SSID,SIGNAL,SECURITY,CHAN,RATE,IN-USE dev wifi list ifname {wireless_iface}")
    raw_nets = []
    for line in out.splitlines():
        parts = re.split(r'(?<!\\):', line)
        if len(parts) < 5:
            continue
        ssid = parts[0].replace("\\:", ":").strip()
        if not ssid:
            continue
        signal = int(parts[1]) if parts[1].isdigit() else 0
        rate = parts[4] if len(parts) > 4 else ""
        in_use = (len(parts) > 5 and parts[5].strip() == "*") or (ssid == active_con_name)
        raw_nets.append({
            "ssid": ssid,
            "signal": signal,
            "security": parts[2] if parts[2] else "Open",
            "channel": parts[3],
            "rate": rate,
            "in_use": in_use,
            "saved": ssid in saved_ssids,
        })

    # Deduplicate by SSID: if any BSSID is in_use, ensure the entry has in_use=True
    grouped = {}
    for n in raw_nets:
        s = n["ssid"]
        if s not in grouped:
            grouped[s] = n
        else:
            if n["in_use"]:
                grouped[s] = n
            elif not grouped[s]["in_use"] and n["signal"] > grouped[s]["signal"]:
                grouped[s] = n

    nets = list(grouped.values())
    nets.sort(key=lambda x: (-x["in_use"], -x["signal"]))
    return nets

def active_gateway():
    _, out, _ = sh("ip route show default")
    best_iface, best_metric = None, 9999
    for line in out.splitlines():
        m_dev = re.search(r"dev (\S+)", line)
        m_met = re.search(r"metric (\d+)", line)
        if m_dev:
            iface = m_dev.group(1)
            metric = int(m_met.group(1)) if m_met else 0
            if metric < best_metric:
                best_metric = metric
                best_iface = iface
    return best_iface

def geoip_via(iface, timeout=6):
    # Try ipwho.is (JSON, HTTPS, reliable worldwide)
    code, out, _ = sh(
        f"curl --interface {iface} -s -k --connect-timeout {timeout} --max-time {timeout+2} 'https://ipwho.is/'",
        timeout=timeout+4
    )
    if code == 0 and out:
        try:
            d = json.loads(out)
            if d.get("success", False) or d.get("ip"):
                return {
                    "query": d.get("ip"),
                    "country": d.get("country"),
                    "countryCode": d.get("country_code"),
                    "regionName": d.get("region"),
                    "city": d.get("city"),
                    "isp": d.get("connection",{}).get("isp") or d.get("isp"),
                    "org": d.get("connection",{}).get("org") or d.get("org"),
                }
        except:
            pass

    # Fallback 1: ip-api.com
    code, out, _ = sh(
        f"curl --interface {iface} -s --connect-timeout {timeout} --max-time {timeout+2} "
        f"'http://ip-api.com/json/?fields=status,query,country,countryCode,regionName,city,isp,org'",
        timeout=timeout+4
    )
    if code == 0 and out:
        try:
            d = json.loads(out)
            if d.get("status") == "success":
                return d
        except:
            pass

    # Fallback 2: api.ip.sb
    code, out, _ = sh(
        f"curl --interface {iface} -s -k --connect-timeout {timeout} --max-time {timeout+2} 'https://api.ip.sb/geoip'",
        timeout=timeout+4
    )
    if code == 0 and out:
        try:
            d = json.loads(out)
            return {
                "query": d.get("ip"),
                "country": d.get("country"),
                "countryCode": d.get("country_code"),
                "regionName": d.get("region"),
                "city": d.get("city"),
                "isp": d.get("isp"),
                "org": d.get("organization"),
            }
        except:
            pass

    return None

def ping_ms(iface, target, count=3):
    code, out, _ = sh(f"ping -I {iface} -c {count} -W 2 {target} 2>/dev/null | tail -1", timeout=12)
    m = re.search(r"[\d.]+/([\d.]+)/[\d.]+", out)
    return round(float(m.group(1)), 1) if m else None

def current_dns_mode():
    _, out, _ = sh("cat /etc/resolv.conf")
    servers = re.findall(r"nameserver\s+(\S+)", out)
    for mode, p in DNS_PRESETS.items():
        if servers and servers[0] in p["servers"]:
            return mode, servers
    return "custom", servers

def current_mirror_mode():
    _, out, _ = sh("head -n 5 /etc/apt/sources.list")
    for mode, p in MIRROR_PRESETS.items():
        if p["url"] in out:
            return mode
    return "custom"

def system_info():
    _, load, _   = sh("cat /proc/loadavg")
    l = (load.split()[:3]+["?","?","?"])[:3]
    _, mem, _    = sh("free -b | awk '/^Mem:/{print $2,$3}'")
    mp = mem.split()
    _, swap, _   = sh("free -b | awk '/^Swap:/{print $2,$3}'")
    sp = swap.split()
    _, disk, _   = sh("df -B1 / | awk 'NR==2{print $2,$3,$5}'")
    dp = disk.split()
    _, uptime, _ = sh("uptime -p")
    _, kernel, _ = sh("uname -r")
    _, host, _   = sh("hostname")
    _, gpu, _    = sh("nvidia-smi --query-gpu=name,temperature.gpu,fan.speed,memory.used,memory.total --format=csv,noheader 2>/dev/null | head -n1")
    return {
        "hostname":host,"kernel":kernel,"uptime":uptime,
        "load":{"1m":l[0],"5m":l[1],"15m":l[2]},
        "mem":{"total":int(mp[0]) if len(mp)>0 else 0,"used":int(mp[1]) if len(mp)>1 else 0},
        "swap":{"total":int(sp[0]) if len(sp)>0 else 0,"used":int(sp[1]) if len(sp)>1 else 0},
        "disk":{"total":int(dp[0]) if len(dp)>0 else 0,"used":int(dp[1]) if len(dp)>1 else 0,"pct":dp[2] if len(dp)>2 else "?"},
        "gpu":gpu or None,
    }

# ── API ───────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    wired_ifaces   = get_all_wired_ifaces()
    wireless_ifaces = get_all_wireless_ifaces()
    primary_wifi   = wireless_ifaces[0] if wireless_ifaces else "wlp6s0"

    wired_list = [iface_info(n) for n in wired_ifaces]
    wireless_hw = [iface_info(n) for n in wireless_ifaces]
    wifi_networks = get_all_wifi_networks(primary_wifi)

    dns_mode, dns_servers = current_dns_mode()
    return jsonify({
        "ts": int(time.time()),
        "gateway": active_gateway(),
        "primary_wifi": primary_wifi,
        "wired": wired_list,
        "wireless_hw": wireless_hw,
        "wifi_networks": wifi_networks,
        "dns": {"mode":dns_mode,"servers":dns_servers,"presets":DNS_PRESETS},
        "mirror": {"mode":current_mirror_mode(),
                   "presets":{k:{"label":v["label"],"url":v["url"]} for k,v in MIRROR_PRESETS.items()}},
        "sysinfo": system_info(),
    })

@app.route("/api/wifi/scan")
def api_wifi_scan():
    wl = get_all_wireless_ifaces()
    iface = wl[0] if wl else "wlp6s0"
    sh(f"nmcli dev wifi rescan ifname {iface} 2>/dev/null")
    time.sleep(1.5)
    networks = get_all_wifi_networks(iface)
    return jsonify({"ok":True,"networks":networks})

@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    body = request.get_json() or {}
    ssid = body.get("ssid","").strip()
    pwd  = body.get("password","").strip()
    wl   = get_all_wireless_ifaces()
    iface = wl[0] if wl else "wlp6s0"
    if not ssid:
        return jsonify({"ok":False,"msg":"SSID 不能为空"}), 400
    if pwd:
        sh(f"nmcli con delete id '{ssid}' 2>/dev/null")
        code, out, err = sh(f"nmcli dev wifi connect '{ssid}' password '{pwd}' ifname {iface}", timeout=30)
    else:
        code, out, err = sh(f"nmcli con up id '{ssid}' 2>/dev/null", timeout=20)
        if code != 0:
            code, out, err = sh(f"nmcli dev wifi connect '{ssid}' ifname {iface}", timeout=30)
    ok = code == 0 and ("successfully activated" in out.lower() or "connection successfully" in out.lower())
    if ok:
        time.sleep(1) # wait for IP
        _, ip, _ = sh(f"ip -4 addr show {iface} | grep -oP '(?<=inet )[0-9./]+'")
        # Automatically make this newly connected wifi the active internet exit!
        _, routes, _ = sh("ip route show default")
        gws = {}
        for line in routes.splitlines():
            m = re.search(r"default via (\S+) dev (\S+)", line)
            if m:
                gws[m.group(2)] = m.group(1)
        if iface in gws:
            for i, (dev, gw) in enumerate(gws.items()):
                metric = 100 if dev == iface else 200 + i * 10
                sh(f"ip route del default via {gw} dev {dev} 2>/dev/null")
                sh(f"ip route add default via {gw} dev {dev} metric {metric}")
        return jsonify({"ok":True,"msg":f"已连接 {ssid} 并直接启用为主力出口！IP: {ip}"})
    return jsonify({"ok":False,"msg":out or err or "连接失败，请检查密码"})

@app.route("/api/wifi/disconnect", methods=["POST"])
def api_wifi_disconnect():
    wl = get_all_wireless_ifaces()
    iface = wl[0] if wl else "wlp6s0"
    sh(f"nmcli dev disconnect {iface}")
    return jsonify({"ok":True,"msg":"无线已断开"})

@app.route("/api/intel/<iface_name>")
def api_intel(iface_name):
    info = iface_info(iface_name)
    if info["state"] != "up":
        return jsonify({"ok":False,"msg":f"{iface_name} 未在线"})
    geo    = geoip_via(iface_name)
    lat_cn = ping_ms(iface_name, "223.5.5.5")
    lat_gl = ping_ms(iface_name, "1.1.1.1")
    return jsonify({
        "ok":True,"geo":geo,
        "flag":COUNTRY_FLAGS.get(geo.get("countryCode","") if geo else "","🌍"),
        "latency":{"domestic_ms":lat_cn,"international_ms":lat_gl},
    })

@app.route("/api/network/<iface_name>/up", methods=["POST"])
def api_iface_up(iface_name):
    sh(f"ip link set {iface_name} up")
    wl = get_all_wireless_ifaces()
    if iface_name in wl:
        sh(f"nmcli dev connect {iface_name}")
    return jsonify({"ok":True,"msg":f"{iface_name} 已开启"})

@app.route("/api/network/<iface_name>/down", methods=["POST"])
def api_iface_down(iface_name):
    wl = get_all_wireless_ifaces()
    if iface_name in wl:
        sh(f"nmcli dev disconnect {iface_name}")
    sh(f"ip link set {iface_name} down")
    return jsonify({"ok":True,"msg":f"{iface_name} 已关闭"})

@app.route("/api/network/<iface_name>/route", methods=["POST"])
def api_set_route(iface_name):
    _, routes, _ = sh("ip route show default")
    gws = {}
    for line in routes.splitlines():
        m = re.search(r"default via (\S+) dev (\S+)", line)
        if m:
            gws[m.group(2)] = m.group(1)
    if iface_name not in gws:
        return jsonify({"ok":False,"msg":f"{iface_name} 无默认路由，请确认已获取 IP"}), 400
    for i,(dev,gw) in enumerate(gws.items()):
        metric = 100 if dev==iface_name else 200+i*10
        sh(f"ip route del default via {gw} dev {dev} 2>/dev/null")
        sh(f"ip route add default via {gw} dev {dev} metric {metric}")
    return jsonify({"ok":True,"msg":f"默认上网出口已切换至 {iface_name}"})

@app.route("/api/dns/<mode>", methods=["POST"])
def api_dns(mode):
    if mode not in DNS_PRESETS:
        return jsonify({"ok":False,"msg":"Invalid"}), 400
    p = DNS_PRESETS[mode]
    content = f"# ControlPanel — {p['label']}\n"+"\n".join(f"nameserver {s}" for s in p["servers"])+"\n"
    sh("chattr -i /etc/resolv.conf 2>/dev/null; rm -f /etc/resolv.conf")
    with open("/etc/resolv.conf","w") as f: f.write(content)
    sh("chattr +i /etc/resolv.conf")
    return jsonify({"ok":True,"msg":f"DNS → {p['label']}: {', '.join(p['servers'])}"})

@app.route("/api/mirror/<mode>", methods=["POST"])
def api_mirror(mode):
    if mode not in MIRROR_PRESETS:
        return jsonify({"ok":False,"msg":"Invalid"}), 400
    p = MIRROR_PRESETS[mode]
    with open("/etc/apt/sources.list","w") as f: f.write(p["content"])
    _, out, _ = sh("apt-get update -q 2>&1 | tail -n 4", timeout=60)
    ok = "error" not in out.lower() and "err:" not in out.lower()
    return jsonify({"ok":True,"msg":f"镜像源 → {p['label']} {'✅ apt update 成功' if ok else '⚠️ '+out[:100]}"})

# ── Frontend ──────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HOMEHOSTINGSER · netswitch</title>
<style>
:root{
  --bg:#070912;--surface:#0c0f1e;--card:#10131f;
  --border:#181b2a;--border2:#21253a;
  --accent:#6366f1;--ag:rgba(99,102,241,.28);
  --cyan:#22d3ee;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;
  --text:#dde4f0;--t2:#8898b8;--t3:#404d68;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}

header{background:linear-gradient(135deg,#0a0e28,#0c1222);border-bottom:1px solid var(--border);padding:11px 18px;display:flex;align-items:center;gap:10px}
.logo{width:30px;height:30px;background:linear-gradient(135deg,var(--accent),var(--cyan));border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.ht{font-size:.88rem;font-weight:700}
.hs{font-size:.66rem;color:var(--t2);margin-top:2px}
.hr{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chip{background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:4px;padding:2px 7px;font-size:.66rem;color:var(--t2)}
.chip b{color:var(--text)}
.rbtn{background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.22);color:#a5b4fc;border-radius:5px;padding:4px 9px;cursor:pointer;font-size:.68rem;font-weight:600;transition:.14s}
.rbtn:hover{background:rgba(99,102,241,.22)}
#cd{font-size:.64rem;color:var(--t3);min-width:22px;text-align:right}

.wrap{max-width:1060px;margin:0 auto;padding:14px 11px;display:grid;gap:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:600px){.g2{grid-template-columns:1fr}}

.card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.chd{padding:9px 13px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:7px}
.chd .ico{width:21px;height:21px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px}
.chd h2{font-size:.69rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--t2)}
.cb{padding:11px 13px}

/* ── Interface item ── */
.wiface{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:9px 11px;margin-bottom:7px;transition:.2s}
.wiface:last-of-type{margin-bottom:0}
.wiface.gw{border-color:rgba(99,102,241,.38);box-shadow:inset 0 0 0 1px rgba(99,102,241,.1)}
.wiface-hd{display:flex;align-items:center;gap:7px;margin-bottom:7px;flex-wrap:wrap}
.wiface-num{width:20px;height:20px;border-radius:5px;background:rgba(99,102,241,.15);color:#a5b4fc;font-size:.68rem;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.wiface-label{font-size:.78rem;font-weight:700}
.wiface-sub{font-size:.63rem;color:var(--t3);margin-top:1px}
.wiface-acts{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap;align-items:center}

/* ── Intel strip ── */
.intel{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:5px;padding:6px 10px;margin-top:6px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;min-height:30px;font-size:.71rem;transition:.25s}
.intel.ok{border-color:rgba(34,211,238,.18);background:rgba(34,211,238,.025)}
.intel.err{border-color:rgba(239,68,68,.18)}
.i-flag{font-size:1.15rem}
.i-place{font-size:.76rem;font-weight:700}
.i-isp{font-size:.65rem;color:var(--t2)}
.i-ip{font-family:monospace;font-size:.7rem;color:var(--cyan)}
.lw{display:flex;gap:7px}
.lt .k{font-size:.59rem;text-transform:uppercase;color:var(--t3)}
.lt .v{font-size:.74rem;font-weight:700;margin-top:1px}
.vg{color:var(--green)}.vm{color:var(--yellow)}.vb{color:var(--red)}.vu{color:var(--t3)}
.intel .tbtn{margin-left:auto;white-space:nowrap;background:rgba(34,211,238,.07);border:1px solid rgba(34,211,238,.17);color:var(--cyan);border-radius:4px;padding:2px 7px;cursor:pointer;font-size:.64rem;font-weight:600;transition:.14s}
.intel .tbtn:hover{background:rgba(34,211,238,.16)}

/* ── Stats ── */
.stats{display:flex;gap:9px;flex-wrap:wrap}
.st .k{font-size:.61rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.st .v{font-size:.72rem;font-weight:600;margin-top:1px}

/* ── WiFi network list ── */
.wnet-list{display:flex;flex-direction:column;gap:6px}
.wnet{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:8px 11px;transition:.18s}
.wnet.connected{
  border: 1.5px solid #22c55e !important;
  background: linear-gradient(90deg, rgba(34,197,94,0.12) 0%, rgba(12,15,30,0.8) 100%) !important;
  box-shadow: 0 0 16px rgba(34,197,94,0.15);
}
.wnet-hd{display:flex;align-items:center;gap:7px}
.wnet-num{width:18px;height:18px;border-radius:4px;background:rgba(255,255,255,.06);color:var(--t2);font-size:.64rem;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.wnet.connected .wnet-num{background: #22c55e !important; color: #000 !important; font-weight: 900;}
.wnet-ssid{font-size:.78rem;font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wnet.connected .wnet-ssid{color: #4ade80 !important;}
.wnet-meta{font-size:.63rem;color:var(--t3);display:flex;gap:5px;align-items:center;margin-top:3px;flex-wrap:wrap}
.wnet-acts{margin-left:auto;display:flex;gap:4px;align-items:center}
.sbar{display:flex;gap:2px;align-items:flex-end}
.sbar s{width:3px;border-radius:1px;background:var(--border2);display:inline-block}
.sbar s.lit{background:var(--green)}
.sbar.weak s.lit{background:var(--yellow)}
.sbar.poor s.lit{background:var(--red)}

.pw-expand{display:none;margin-top:7px;padding-top:7px;border-top:1px solid var(--border)}
.pw-expand.show{display:flex;gap:6px;align-items:center}
.pw-inp{flex:1;background:var(--bg);border:1px solid var(--border2);border-radius:5px;padding:5px 8px;color:var(--text);font-size:.74rem;outline:none;transition:.13s}
.pw-inp:focus{border-color:var(--accent)}
.pw-eye{background:none;border:none;color:var(--t3);cursor:pointer;font-size:12px;padding:2px 4px}
.pw-eye:hover{color:var(--text)}

/* Pills */
.pill{display:inline-flex;align-items:center;gap:3px;border-radius:3px;padding:1px 5px;font-size:.63rem;font-weight:700}
.pill::before{content:'';width:4px;height:4px;border-radius:50%}
.p-up{background:rgba(34,197,94,.09);color:var(--green)}.p-up::before{background:var(--green)}
.p-dn{background:rgba(239,68,68,.09);color:var(--red)}.p-dn::before{background:var(--red)}
.p-unk{background:rgba(245,158,11,.09);color:var(--yellow)}.p-unk::before{background:var(--yellow)}
.p-info{background:rgba(34,211,238,.09);color:var(--cyan)}.p-info::before{background:var(--cyan)}

/* 呼吸灯效果：正在用此网络上网 */
@keyframes breathingPulse {
  0%   { box-shadow: 0 0 0 0 rgba(99,102,241, 0.7); transform: scale(1); }
  50%  { box-shadow: 0 0 16px 4px rgba(99,102,241, 0.85); transform: scale(1.02); }
  100% { box-shadow: 0 0 0 0 rgba(99,102,241, 0); transform: scale(1); }
}
.gw-b{
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  color: #fff !important;
  border: 1.5px solid #a5b4fc !important;
  border-radius: 5px;
  font-size: .68rem;
  font-weight: 900;
  padding: 4px 10px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  letter-spacing: .3px;
  animation: breathingPulse 2s infinite ease-in-out;
}

/* 超醒目的【用此网路上网】切换按钮 */
.btn-switch{
  background: linear-gradient(135deg, #0284c7, #0369a1) !important;
  color: #fff !important;
  border: 1.5px solid #38bdf8 !important;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: .72rem;
  font-weight: 800;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(14,165,233, 0.4);
  transition: all .15s ease;
  white-space: nowrap;
}
.btn-switch:hover{
  background: linear-gradient(135deg, #0369a1, #0284c7) !important;
  box-shadow: 0 0 16px rgba(56,189,248, 0.7);
  transform: translateY(-1px);
}

.connected-b{background:#22c55e;color:#000;border-radius:3px;font-size:.62rem;font-weight:900;padding:2px 6px;letter-spacing:.3px}
.saved-b{background:rgba(34,197,94,.08);color:var(--green);border:1px solid rgba(34,197,94,.18);border-radius:3px;font-size:.6rem;padding:1px 4px}
.spd-b{background:rgba(34,211,238,.08);color:var(--cyan);border:1px solid rgba(34,211,238,.18);border-radius:3px;font-size:.6rem;padding:1px 4px;font-weight:600}

/* Buttons */
.btn{border:none;cursor:pointer;border-radius:5px;padding:3px 8px;font-size:.66rem;font-weight:700;transition:.13s;white-space:nowrap}
.bg{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2)}.bg:hover{background:rgba(34,197,94,.2)}
.br{background:rgba(239,68,68,.09);color:var(--red);border:1px solid rgba(239,68,68,.17)}.br:hover{background:rgba(239,68,68,.17)}
.ba{background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.22)}.ba:hover{background:rgba(99,102,241,.22)}
.bc{background:rgba(34,211,238,.08);color:var(--cyan);border:1px solid rgba(34,211,238,.18)}.bc:hover{background:rgba(34,211,238,.18)}
.bd{background:rgba(255,255,255,.04);color:var(--t2);border:1px solid var(--border)}.bd:hover{background:rgba(255,255,255,.08)}
.byl{background:rgba(245,158,11,.1);color:var(--yellow);border:1px solid rgba(245,158,11,.21)}.byl:hover{background:rgba(245,158,11,.2)}
.btn:disabled{opacity:.28;cursor:not-allowed}

/* Seg */
.seg{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:7px;overflow:hidden;width:100%}
.sb{flex:1;border:none;cursor:pointer;padding:7px 3px;font-size:.68rem;font-weight:600;background:transparent;color:var(--t2);transition:.13s;text-align:center;line-height:1.4}
.sb:hover:not(.active){background:rgba(255,255,255,.03)}
.sb.active{background:var(--accent);color:#fff;box-shadow:0 0 10px var(--ag)}
.sb small{display:block;font-size:.58rem;font-weight:400;opacity:.7;margin-top:1px}

.divider{height:1px;background:var(--border);margin:8px 0}
.ir{padding:6px 0;border-bottom:1px solid var(--border)}
.ir:last-of-type{border:none;padding-bottom:0}
.ir-l{font-size:.62rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.ir-v{font-size:.77rem;font-weight:600;margin-top:2px}
.ir-s{font-size:.64rem;color:var(--t3);margin-top:1px}

/* Sysinfo */
.sg{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
@media(max-width:440px){.sg{grid-template-columns:1fr 1fr}}
.si{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:7px 9px}
.si .k{font-size:.59rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.si .v{font-size:.78rem;font-weight:700;margin-top:2px}
.si .v2{font-size:.64rem;color:var(--t2);margin-top:1px}
.bar{height:3px;background:var(--border2);border-radius:2px;margin-top:4px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .5s}
.bgg .bar-fill{background:var(--green)}.bga .bar-fill{background:var(--accent)}.bgy .bar-fill{background:var(--yellow)}
.gpu{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:7px 10px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:6px}
.gn{font-size:.73rem;font-weight:700;flex:1;min-width:100px}
.gv .k{font-size:.58rem;color:var(--t3);text-transform:uppercase}
.gv .v{font-size:.75rem;font-weight:700;color:var(--cyan)}

.scan-row{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.scan-row span{font-size:.7rem;color:var(--t2)}
.net-count{font-size:.64rem;color:var(--t3);margin-left:4px}

.toast{position:fixed;bottom:16px;right:16px;background:#111422;border:1px solid var(--border2);border-radius:8px;padding:9px 13px;font-size:.74rem;max-width:320px;z-index:9999;box-shadow:0 6px 30px rgba(0,0,0,.5);display:none;line-height:1.5}
.toast.show{display:block;animation:tIn .18s ease}
@keyframes tIn{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
.spin{animation:spin 1s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
code{background:rgba(255,255,255,.06);border-radius:3px;padding:1px 3px;font-size:.84em}
footer{text-align:center;padding:12px;color:var(--t3);font-size:.63rem;border-top:1px solid var(--border)}
</style>
</head>
<body>

<header>
  <div class="logo">🖥</div>
  <div>
    <div class="ht">HOMEHOSTINGSER · netswitch</div>
    <div class="hs" id="hdr-sub">Debian 13 · 加载中…</div>
  </div>
  <div class="hr">
    <div class="chip">Tailscale <b>100.92.52.85</b></div>
    <div class="chip">当前主出口 <b id="hdr-gw">—</b></div>
    <button class="rbtn" onclick="doRefresh()">⟳ 刷新</button>
    <span id="cd"></span>
  </div>
</header>

<div class="wrap">

<!-- ── TOP ACTIVE SUMMARY BANNER ── -->
<div class="card" style="border: 1.5px solid rgba(99,102,241,.4); background: linear-gradient(135deg, rgba(15,23,42,0.95), rgba(30,27,75,0.7)); box-shadow: 0 4px 20px rgba(0,0,0,0.4);">
  <div class="cb" style="display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; padding:14px 18px">
    <div style="display:flex; align-items:center; gap:12px">
      <div style="width:40px; height:40px; border-radius:10px; background:linear-gradient(135deg,#6366f1,#22c55e); display:flex; align-items:center; justify-content:center; font-size:20px; box-shadow:0 0 12px rgba(34,197,94,0.3)">
        🚀
      </div>
      <div>
        <div style="font-size:.65rem; text-transform:uppercase; letter-spacing:.5px; color:var(--t2); font-weight:700">
          ● 当前整机上网主出口 (Active Exit)
        </div>
        <div style="display:flex; align-items:center; gap:8px; margin-top:2px">
          <span id="top-active-name" style="font-size:1.05rem; font-weight:900; color:#fff">检测中…</span>
          <span id="top-active-type" class="gw-b" style="font-size:.65rem">有线/无线</span>
        </div>
      </div>
    </div>

    <div style="display:flex; flex-wrap:wrap; align-items:center; gap:16px; border-left:1px solid var(--border2); padding-left:16px">
      <div>
        <div style="font-size:.6rem; color:var(--t3); text-transform:uppercase; font-weight:700">出口国家与地区</div>
        <div id="top-active-geo" style="font-size:.85rem; font-weight:800; color:var(--cyan); margin-top:2px">查询中…</div>
      </div>
      <div>
        <div style="font-size:.6rem; color:var(--t3); text-transform:uppercase; font-weight:700">公网出口 IP</div>
        <div id="top-active-ip" style="font-size:.85rem; font-weight:800; font-family:monospace; color:#a5b4fc; margin-top:2px">—</div>
      </div>
      <div>
        <div style="font-size:.6rem; color:var(--t3); text-transform:uppercase; font-weight:700">实时上下行网速</div>
        <div id="top-active-rate" style="font-size:.86rem; font-weight:900; margin-top:2px">
          <span style="color:#22c55e">↓ 0 KB/s</span>
          <span style="color:var(--t3);margin:0 4px">|</span>
          <span style="color:#38bdf8">↑ 0 KB/s</span>
        </div>
      </div>
      <div>
        <div style="font-size:.6rem; color:var(--t3); text-transform:uppercase; font-weight:700">国内 / 国际延迟</div>
        <div id="top-active-lat" style="font-size:.82rem; font-weight:700; margin-top:2px">—</div>
      </div>
    </div>
  </div>
</div>

<!-- ── NETWORK ── -->
<div class="card">
  <div class="chd">
    <div class="ico" style="background:rgba(34,211,238,.07)">🌐</div>
    <h2>网络接口 & 路由管理</h2>
    <button class="btn bc" style="margin-left:auto;font-size:.66rem" onclick="doWifiScan()">🔍 扫描刷新 Wi-Fi</button>
  </div>
  <div class="cb">

    <!-- WIRED section -->
    <div style="font-size:.66rem;color:var(--t2);font-weight:700;text-transform:uppercase;letter-spacing:.4px;margin-bottom:7px">
      🔌 有线网卡
    </div>
    <div id="wired-list">
      <div style="color:var(--t3);font-size:.74rem;padding:10px 0">加载中…</div>
    </div>

    <div class="divider" style="margin:10px 0 10px"></div>

    <!-- WIRELESS section -->
    <div class="scan-row">
      <div style="font-size:.66rem;color:var(--t2);font-weight:700;text-transform:uppercase;letter-spacing:.4px">
        📶 无线网络
      </div>
      <span class="net-count" id="wifi-count"></span>
      <span id="wifi-hw" style="font-size:.63rem;color:var(--t3)"></span>
    </div>
    <div class="wnet-list" id="wifi-list">
      <div style="color:var(--t3);font-size:.74rem;padding:10px 0">加载中…</div>
    </div>

  </div>
</div>

<!-- ── DNS + MIRROR ── -->
<div class="g2">
  <div class="card">
    <div class="chd">
      <div class="ico" style="background:rgba(167,139,250,.07)">🔍</div>
      <h2>DNS 切换</h2>
      <span id="dns-pill" class="pill" style="margin-left:auto">…</span>
    </div>
    <div class="cb">
      <div class="ir"><div class="ir-l">当前 DNS</div><div class="ir-v" id="dns-sv">—</div><div class="ir-s" id="dns-note">—</div></div>
      <div class="divider"></div>
      <div class="seg">
        <button class="sb" id="dns-d" onclick="setDNS('domestic')">🇨🇳 国内<small>阿里 / 114</small></button>
        <button class="sb" id="dns-o" onclick="setDNS('overseas')">🌐 国际<small>Cloudflare / Google</small></button>
      </div>
      <div style="margin-top:7px;font-size:.65rem;color:var(--t3);line-height:1.8">
        <b style="color:var(--t2)">国内：</b>223.5.5.5 · 223.6.6.6 · 114.114.114.114<br>
        <b style="color:var(--t2)">国际：</b>1.1.1.1 · 8.8.8.8 · 9.9.9.9
      </div>
    </div>
  </div>

  <div class="card">
    <div class="chd">
      <div class="ico" style="background:rgba(34,197,94,.06)">📦</div>
      <h2>APT 镜像源</h2>
      <span id="mir-pill" class="pill" style="margin-left:auto">…</span>
    </div>
    <div class="cb">
      <div class="ir"><div class="ir-l">当前镜像</div><div class="ir-v" id="mir-name">—</div><div class="ir-s" id="mir-url">—</div></div>
      <div class="divider"></div>
      <div class="seg">
        <button class="sb" id="mir-d" onclick="setMirror('domestic')">🇨🇳 清华<small>TUNA</small></button>
        <button class="sb" id="mir-u" onclick="setMirror('ustc')">🎓 中科大<small>USTC</small></button>
        <button class="sb" id="mir-o" onclick="setMirror('overseas')">🌐 官方<small>Debian.org</small></button>
      </div>
      <div style="margin-top:7px;font-size:.65rem;color:var(--t3)">切换后自动 <code>apt update</code>，约 10~20s</div>
    </div>
  </div>
</div>

<!-- ── SYSINFO ── -->
<div class="card">
  <div class="chd">
    <div class="ico" style="background:rgba(245,158,11,.06)">📊</div>
    <h2>系统状态</h2>
    <span style="margin-left:auto;font-size:.63rem;color:var(--t3)" id="si-ts"></span>
  </div>
  <div class="cb">
    <div class="sg">
      <div class="si bgg"><div class="k">内存</div><div class="v" id="si-mem">—</div><div class="v2" id="si-mem2">—</div><div class="bar"><div class="bar-fill" id="bm" style="width:0%"></div></div></div>
      <div class="si bga"><div class="k">磁盘 /</div><div class="v" id="si-disk">—</div><div class="v2" id="si-disk2">—</div><div class="bar"><div class="bar-fill" id="bd" style="width:0%"></div></div></div>
      <div class="si bgy"><div class="k">Swap</div><div class="v" id="si-swap">—</div><div class="v2" id="si-swap2">—</div><div class="bar"><div class="bar-fill" id="bs" style="width:0%"></div></div></div>
      <div class="si"><div class="k">负载 1m/5m/15m</div><div class="v" id="si-load">—</div></div>
      <div class="si"><div class="k">运行时长</div><div class="v" id="si-up">—</div></div>
      <div class="si"><div class="k">内核</div><div class="v" id="si-ker" style="font-size:.66rem">—</div></div>
    </div>
    <div id="gpu-row"></div>
  </div>
</div>

</div><!-- /wrap -->

<div class="toast" id="toast"></div>
<footer>HOMEHOSTINGSER · Debian 13 Trixie · i7-7700 · Quadro P600 · MT7921 Wi-Fi 6 · Tailscale 100.92.52.85</footer>

<script>
function fmtB(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++;}return b.toFixed(1)+' '+u[i];}
function fmtRate(b){
  if(!b || b <= 0) return '0 B/s';
  const u=['B/s','KB/s','MB/s','GB/s'];
  let i=0;
  while(b>=1024&&i<3){b/=1024;i++;}
  return b.toFixed(1)+' '+u[i];
}
function pct(u,t){return t>0?Math.round(u/t*100):0}
function pillC(s){return s==='up'?'pill p-up':s==='down'?'pill p-dn':'pill p-unk'}
function pillL(s){return s==='up'?'在线':s==='down'?'离线':'未知'}
function sigBars(sig){
  const bars=[4,7,10,13,16];
  const lit=Math.round(sig/20);
  let q=sig<30?'poor':sig<60?'weak':'';
  return `<div class="sbar ${q}">${bars.map((h,i)=>`<s style="height:${h}px"${i<lit?' class="lit"':''}></s>`).join('')}</div>`;
}
function latC(ms){if(ms==null)return 'vu';if(ms<60)return 'vg';if(ms<150)return 'vm';return 'vb';}

let _tt=null;
function toast(msg,ok=true,dur=4000){
  const t=document.getElementById('toast');t.innerHTML=msg;
  t.style.borderColor=ok?'rgba(34,197,94,.27)':'rgba(239,68,68,.28)';
  t.className='toast show';clearTimeout(_tt);_tt=setTimeout(()=>t.className='toast',dur);
}

const intelCache = {};

async function doRefresh(){
  cdSec=10;document.getElementById('cd').textContent='';
  const rb=document.querySelector('.rbtn');rb.innerHTML='<span class="spin">⟳</span>';
  try{const d=await fetch('/api/status').then(r=>r.json());applyStatus(d);}
  catch(e){toast('刷新失败: '+e,false);}
  rb.innerHTML='⟳ 刷新';
}

function applyStatus(d){
  const gw=d.gateway;
  const primaryWifi=d.primary_wifi||'wlp6s0';
  document.getElementById('hdr-sub').textContent=`${d.sysinfo.hostname} · ${d.sysinfo.kernel} · 负载 ${d.sysinfo.load['1m']}`;
  document.getElementById('hdr-gw').textContent=gw||'—';
  document.getElementById('si-ts').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');

  // ── Update Top Active Summary Banner ──
  const activeWifi = (d.wifi_networks||[]).find(n=>n.in_use);
  let activeName = gw || '未知';
  let activeType = '有线以太网';
  let activeIfcInfo = null;
  if(gw && (gw.startsWith('wl') || gw === primaryWifi)){
    activeName = activeWifi ? activeWifi.ssid : '无线网络 ('+gw+')';
    activeType = '无线 Wi-Fi';
    activeIfcInfo = (d.wireless_hw||[]).find(w=>w.name===gw) || (d.wireless_hw||[])[0];
  } else if(gw && (gw.startsWith('en') || gw.startsWith('eth'))){
    activeName = '有线网卡 ('+gw+')';
    activeType = '千兆有线网络';
    activeIfcInfo = (d.wired||[]).find(w=>w.name===gw);
  }
  document.getElementById('top-active-name').textContent = activeName;
  document.getElementById('top-active-type').textContent = activeType;

  // Real-time Upload / Download Rate on Top Banner
  if(activeIfcInfo){
    const downRate = fmtRate(activeIfcInfo.rx_rate || 0);
    const upRate = fmtRate(activeIfcInfo.tx_rate || 0);
    document.getElementById('top-active-rate').innerHTML = `
      <span style="color:#22c55e">↓ ${downRate}</span>
      <span style="color:var(--t3);margin:0 4px">|</span>
      <span style="color:#38bdf8">↑ ${upRate}</span>
    `;
  } else {
    document.getElementById('top-active-rate').innerHTML = `<span style="color:var(--t3)">↓ 0 B/s | ↑ 0 B/s</span>`;
  }

  // Bind active gateway geoip to top banner
  if(gw && intelCache[gw]){
    const ig = intelCache[gw];
    const geo = ig.geo || {};
    document.getElementById('top-active-geo').textContent = `${ig.flag||'🌍'} ${geo.country||'未知'} · ${geo.city||''}`;
    document.getElementById('top-active-ip').textContent = geo.query || '—';
    const ld = ig.latency?.domestic_ms, lg = ig.latency?.international_ms;
    document.getElementById('top-active-lat').innerHTML = `
      <span class="${latC(ld)}">🇨🇳 ${ld!=null?ld+'ms':'超时'}</span>
      <span style="color:var(--t3);margin:0 4px">|</span>
      <span class="${latC(lg)}">🌐 ${lg!=null?lg+'ms':'超时'}</span>
    `;
  } else if(gw) {
    testIntel(gw);
  }

  // ── Wired interfaces ──
  const wl=document.getElementById('wired-list');
  if(!d.wired||d.wired.length===0){
    wl.innerHTML='<div style="color:var(--t3);font-size:.74rem;padding:8px 0">未检测到有线网卡</div>';
  } else {
    wl.innerHTML=d.wired.map((ifc,idx)=>{
      const isGW=gw===ifc.name;
      const cached=intelCache[ifc.name];
      return `<div class="wiface${isGW?' gw':''}">
        <div class="wiface-hd">
          <div class="wiface-num">${idx+1}</div>
          <div>
            <div class="wiface-label">${ifc.name} ${ifc.speed?`<span class="spd-b">⚡ ${ifc.speed}</span>`:''}</div>
            <div class="wiface-sub">MAC: ${ifc.mac||'—'}</div>
          </div>
          <div class="wiface-acts">
            ${isGW?'<span class="gw-b">★ 正在用此网络上网</span>':`<button class="btn-switch" onclick="ifaceRoute('${ifc.name}')">⚡ 用此网路上网</button>`}
            <span class="${pillC(ifc.state)}">${pillL(ifc.state)}</span>
            <button class="btn bg" onclick="ifaceUp('${ifc.name}')">开启</button>
            <button class="btn br" onclick="ifaceDown('${ifc.name}')">关闭</button>
          </div>
        </div>
        <div class="stats">
          <div class="st"><div class="k">本地 IP</div><div class="v">${ifc.ip||'—'}</div></div>
          <div class="st"><div class="k">协商速率</div><div class="v">${ifc.speed||'—'}</div></div>
          <div class="st"><div class="k">实时下行</div><div class="v" style="color:#22c55e;font-weight:800">↓ ${fmtRate(ifc.rx_rate||0)}</div></div>
          <div class="st"><div class="k">实时上行</div><div class="v" style="color:#38bdf8;font-weight:800">↑ ${fmtRate(ifc.tx_rate||0)}</div></div>
          <div class="st"><div class="k">累计已收</div><div class="v">${fmtB(ifc.rx_bytes||0)}</div></div>
          <div class="st"><div class="k">累计已发</div><div class="v">${fmtB(ifc.tx_bytes||0)}</div></div>
        </div>
        <div class="intel ${cached?'ok':''}" id="intel-${ifc.name}">
          ${cached ? renderIntelContent(cached, ifc.name) : `
            <span style="color:var(--t3);font-size:.68rem">出口属性检测中…</span>
            <button class="tbtn" onclick="testIntel('${ifc.name}')">🔍 重新检测</button>
          `}
        </div>
      </div>`;
    }).join('');

    d.wired.forEach(ifc=>{
      if(ifc.state==='up' && !intelCache[ifc.name]){
        testIntel(ifc.name);
      }
    });
  }

  const wHW=d.wireless_hw||[];
  document.getElementById('wifi-hw').textContent=wHW.map(w=>`${w.name} (${pillL(w.state)})`).join(' / ');

  const primaryWifiInfo = wHW.find(w=>w.name===primaryWifi) || wHW[0] || {};
  applyWifiList(d.wifi_networks||[], gw, primaryWifi, primaryWifiInfo);

  const dm=d.dns.mode;
  const dl={domestic:'🇨🇳 国内 DNS',overseas:'🌐 国际 DNS',custom:'⚙️ 自定义'};
  const dp=document.getElementById('dns-pill');dp.textContent=dl[dm]||dm;
  dp.className='pill '+(dm==='domestic'?'p-up':dm==='overseas'?'p-info':'p-unk');
  document.getElementById('dns-sv').textContent=(d.dns.servers||[]).join(' · ')||'—';
  document.getElementById('dns-note').textContent=d.dns.presets?.[dm]?.note||'';
  ['domestic','overseas'].forEach(m=>document.getElementById('dns-'+m[0]).className='sb'+(dm===m?' active':''));

  const mm=d.mirror.mode;
  const ml={domestic:'🇨🇳 清华 TUNA',ustc:'🎓 中科大 USTC',overseas:'🌐 官方源',custom:'⚙️ 自定义'};
  const mp2=document.getElementById('mir-pill');mp2.textContent=ml[mm]||mm;
  mp2.className='pill '+(mm==='overseas'?'p-info':'p-up');
  const mpm=d.mirror.presets?.[mm];
  document.getElementById('mir-name').textContent=mpm?.label||mm;
  document.getElementById('mir-url').textContent=mpm?.url||'—';
  ['domestic','ustc','overseas'].forEach(m=>{
    const id=m==='domestic'?'mir-d':m==='ustc'?'mir-u':'mir-o';
    document.getElementById(id).className='sb'+(mm===m?' active':'');
  });

  const s=d.sysinfo,mem=s.mem,disk=s.disk,swap=s.swap;
  const mp3=pct(mem.used,mem.total),dp3=parseInt(disk.pct)||0,sp=pct(swap.used,swap.total);
  document.getElementById('si-mem').textContent=fmtB(mem.used)+' / '+fmtB(mem.total);
  document.getElementById('si-mem2').textContent=mp3+'% 已用';
  document.getElementById('bm').style.width=mp3+'%';
  document.getElementById('si-disk').textContent=fmtB(disk.used)+' / '+fmtB(disk.total);
  document.getElementById('si-disk2').textContent=disk.pct+' 已用';
  document.getElementById('bd').style.width=dp3+'%';
  document.getElementById('si-swap').textContent=fmtB(swap.used)+' / '+fmtB(swap.total);
  document.getElementById('si-swap2').textContent=sp+'% 已用';
  document.getElementById('bs').style.width=sp+'%';
  document.getElementById('si-load').textContent=`${s.load['1m']} / ${s.load['5m']} / ${s.load['15m']}`;
  document.getElementById('si-up').textContent=s.uptime;
  document.getElementById('si-ker').textContent=s.kernel;
  if(s.gpu){const g=s.gpu.split(',').map(x=>x.trim());
    document.getElementById('gpu-row').innerHTML=`<div class="gpu"><div class="gn">🎮 ${g[0]}</div><div class="gv"><div class="k">温度</div><div class="v">${g[1]}°C</div></div><div class="gv"><div class="k">风扇</div><div class="v">${g[2]}</div></div><div class="gv"><div class="k">显存</div><div class="v">${g[3]} / ${g[4]}</div></div></div>`;
  }
}

function renderIntelContent(d, name){
  const g=d.geo||{};
  const ld=d.latency?.domestic_ms,lg=d.latency?.international_ms;
  return `
    <span class="i-flag">${d.flag||'🌍'}</span>
    <div>
      <div class="i-place">${g.country||'未知国家'} · ${g.city||''} ${g.regionName||''}</div>
      <div class="i-isp">${g.org||g.isp||'未知运营商'}</div>
    </div>
    <div>
      <div style="font-size:.59rem;color:var(--t3)">出口 IP</div>
      <div class="i-ip">${g.query||'—'}</div>
    </div>
    <div class="lw">
      <div class="lt"><div class="k">🇨🇳 国内</div><div class="v ${latC(ld)}">${ld!=null?ld+' ms':'超时'}</div></div>
      <div class="lt"><div class="k">🌐 国际</div><div class="v ${latC(lg)}">${lg!=null?lg+' ms':'超时'}</div></div>
    </div>
    <button class="tbtn" onclick="testIntel('${name}')">🔄 刷新属性</button>
  `;
}

function applyWifiList(nets, gw, primaryWifi, primaryWifiInfo){
  primaryWifiInfo = primaryWifiInfo || {};
  const wl=document.getElementById('wifi-list');
  document.getElementById('wifi-count').textContent=nets.length ? `· ${nets.length} 个网络` : '';
  if(!nets.length){
    wl.innerHTML='<div style="color:var(--t3);font-size:.74rem;padding:8px 0">未扫描到网络，点右上角「扫描刷新」</div>';
    return;
  }
  const isWifiGW = gw && (gw === primaryWifi || gw.startsWith('wl'));

  wl.innerHTML=nets.map((n,i)=>{
    const connected=n.in_use;
    const cached=connected ? intelCache[primaryWifi] : null;
    return `<div class="wnet${connected?' connected':''}" id="wn-${i}">
      <div class="wnet-hd">
        <div class="wnet-num">${i+1}</div>
        <div style="flex:1;min-width:0">
          <div class="wnet-ssid" title="${n.ssid}">${n.ssid}
            ${connected?'<span class="connected-b" style="margin-left:5px">● 已连接 / 占用中</span>':''}
            ${n.saved&&!connected?'<span class="saved-b" style="margin-left:4px">已保存</span>':''}
            ${n.rate?`<span class="spd-b" style="margin-left:4px">⚡ 协商 ${n.rate}</span>`:''}
          </div>
          <div class="wnet-meta">
            ${sigBars(n.signal)}
            <span>${n.signal}%</span>
            <span style="color:var(--border2)">·</span>
            <span>信道 ${n.channel}</span>
            <span style="color:var(--border2)">·</span>
            <span>${n.security}</span>
          </div>
        </div>
        <div class="wnet-acts">
          ${connected
            ? `${isWifiGW
                ? '<span class="gw-b">★ 正在用此网络上网</span>'
                : `<button class="btn-switch" onclick="wifiSetRoute()">⚡ 用此网路上网</button>`
               }
               <button class="btn br" onclick="wifiDisc()">断开</button>`
            : `<button class="btn bg" onclick="wifiConnect(${i},'${n.ssid.replace(/'/g,"\\'")}',${n.saved})">连接</button>`
          }
          <button class="btn bd" onclick="togglePw(${i})" title="输入/修改密码">🔑</button>
        </div>
      </div>
      ${connected ? `
        <div class="stats" style="margin-top:6px;padding:6px 9px;background:rgba(0,0,0,0.25);border-radius:5px">
          <div class="st"><div class="k">本地 IP</div><div class="v">${primaryWifiInfo.ip||'—'}</div></div>
          <div class="st"><div class="k">实时下行</div><div class="v" style="color:#22c55e;font-weight:800">↓ ${fmtRate(primaryWifiInfo.rx_rate||0)}</div></div>
          <div class="st"><div class="k">实时上行</div><div class="v" style="color:#38bdf8;font-weight:800">↑ ${fmtRate(primaryWifiInfo.tx_rate||0)}</div></div>
          <div class="st"><div class="k">累计流量</div><div class="v">${fmtB((primaryWifiInfo.rx_bytes||0)+(primaryWifiInfo.tx_bytes||0))}</div></div>
        </div>
        <div class="intel ${cached?'ok':''}" id="intel-${primaryWifi}">
          ${cached ? renderIntelContent(cached, primaryWifi) : `
            <span style="color:var(--t3);font-size:.68rem">正在自动查询当前 WiFi 网络属性与出口国家…</span>
            <button class="tbtn" onclick="testIntel('${primaryWifi}')">🔍 检测属性</button>
          `}
        </div>
      ` : ''}
      <div class="pw-expand" id="pw-${i}">
        <input class="pw-inp" type="password" id="pwd-${i}" placeholder="${n.security==='Open'?'开放网络，可留空':'Wi-Fi 密码（留空用已保存密码）'}">
        <button class="pw-eye" onclick="toggleEye(${i})">👁</button>
        <button class="btn bc" onclick="wifiConnect(${i},'${n.ssid.replace(/'/g,"\\'")}',false)">连接</button>
      </div>
    </div>`;
  }).join('');

  const connectedNet = nets.find(n=>n.in_use);
  if(connectedNet && !intelCache[primaryWifi]){
    testIntel(primaryWifi);
  }
}

async function testIntel(name){
  const el=document.getElementById('intel-'+name);
  if(el){
    el.className='intel';
    el.innerHTML=`<span class="spin">⟳</span><span style="font-size:.68rem;color:var(--yellow);margin-left:5px">正在检测出口 IP、国家及网络延迟…</span>`;
  }
  try{
    const d=await fetch('/api/intel/'+name).then(r=>r.json());
    if(!d.ok){
      if(el){
        el.className='intel err';
        el.innerHTML=`<span style="color:var(--red);font-size:.68rem">❌ ${d.msg}</span><button class="tbtn" onclick="testIntel('${name}')">重试</button>`;
      }
      return;
    }
    intelCache[name] = d;
    const refreshedEl = document.getElementById('intel-'+name);
    if(refreshedEl){
      refreshedEl.className='intel ok';
      refreshedEl.innerHTML=renderIntelContent(d, name);
    }
    // Also sync to top banner if this is active gateway
    const currentGw = document.getElementById('hdr-gw')?.textContent?.trim();
    if(name === currentGw || (currentGw && currentGw.startsWith('wl') && name.startsWith('wl'))){
      const g = d.geo || {};
      document.getElementById('top-active-geo').textContent = `${d.flag||'🌍'} ${g.country||'未知'} · ${g.city||''}`;
      document.getElementById('top-active-ip').textContent = g.query || '—';
      document.getElementById('top-active-isp').textContent = g.org || g.isp || '—';
      const ld = d.latency?.domestic_ms, lg = d.latency?.international_ms;
      document.getElementById('top-active-lat').innerHTML = `
        <span class="${latC(ld)}">🇨🇳 ${ld!=null?ld+'ms':'超时'}</span>
        <span style="color:var(--t3);margin:0 4px">|</span>
        <span class="${latC(lg)}">🌐 ${lg!=null?lg+'ms':'超时'}</span>
      `;
    }
  }catch(e){
    const refreshedEl = document.getElementById('intel-'+name);
    if(refreshedEl){
      refreshedEl.className='intel err';
      refreshedEl.innerHTML=`<span style="color:var(--red);font-size:.68rem">❌ ${e}</span>`;
    }
  }
}

async function ifaceUp(n){toast(`开启 ${n}…`);const r=await fetch('/api/network/'+n+'/up',{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1400);}
async function ifaceDown(n){delete intelCache[n];toast(`关闭 ${n}…`);const r=await fetch('/api/network/'+n+'/down',{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1400);}
async function ifaceRoute(n){toast(`切换上网主出口至 ${n}…`);const r=await fetch('/api/network/'+n+'/route',{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1200);}

function togglePw(i){const el=document.getElementById('pw-'+i);el.classList.toggle('show');}
function toggleEye(i){const inp=document.getElementById('pwd-'+i);inp.type=inp.type==='password'?'text':'password';}

async function wifiConnect(i, ssid, saved){
  const pwd=(document.getElementById('pwd-'+i)?.value||'').trim();
  if(!saved && !pwd){
    document.getElementById('pw-'+i).classList.add('show');
    document.getElementById('pwd-'+i).focus();
    toast('请输入 Wi-Fi 密码后点「连接」',true,3000);
    return;
  }
  toast(`正在连接 ${ssid}…`,true,20000);
  const r=await fetch('/api/wifi/connect',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ssid,password:pwd})}).then(r=>r.json());
  toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,6000);
  for(let k in intelCache){
    if(k.startsWith('wl')) delete intelCache[k];
  }
  if(r.ok)setTimeout(doRefresh,1500);
}
async function wifiDisc(){
  for(let k in intelCache){
    if(k.startsWith('wl')) delete intelCache[k];
  }
  toast('断开无线…');
  const r=await fetch('/api/wifi/disconnect',{method:'POST'}).then(r=>r.json());
  toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);
  setTimeout(doRefresh,1200);
}
async function wifiSetRoute(){
  const r=await fetch('/api/status').then(r=>r.json());
  const wh=r.wireless_hw?.[0];
  if(!wh)return;
  toast('切换上网主出口至无线…');
  const rr=await fetch('/api/network/'+wh.name+'/route',{method:'POST'}).then(r=>r.json());
  toast(rr.ok?'✅ '+rr.msg:'❌ '+rr.msg,rr.ok);setTimeout(doRefresh,1200);
}

async function doWifiScan(){
  toast('扫描周边 Wi-Fi…',true,8000);
  document.getElementById('wifi-list').innerHTML='<div style="color:var(--t2);font-size:.74rem;padding:8px 0"><span class="spin">⟳</span> 扫描中…</div>';
  try{
    const d=await fetch('/api/wifi/scan').then(r=>r.json());
    applyWifiList(d.networks||[], null, 'wlp6s0');
    document.getElementById('wifi-count').textContent=`· ${(d.networks||[]).length} 个网络`;
    toast(`扫描完成，发现 ${(d.networks||[]).length} 个网络`);
  }catch(e){toast('扫描失败: '+e,false);}
}

async function setDNS(m){toast('切换 DNS…');const r=await fetch('/api/dns/'+m,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,5000);setTimeout(doRefresh,600);}
async function setMirror(m){toast('切换镜像源并 apt update…',true,20000);const r=await fetch('/api/mirror/'+m,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,7000);setTimeout(doRefresh,600);}

let cdSec=15;
function tick(){if(--cdSec<=0){cdSec=15;doRefresh();}document.getElementById('cd').textContent=cdSec+'s';}
doRefresh();setInterval(tick,1000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
