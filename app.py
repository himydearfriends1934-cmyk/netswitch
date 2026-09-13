#!/usr/bin/env python3
"""
HOMEHOSTINGSER Control Panel - Stable Edition
Flask-based server management dashboard
"""
import subprocess, json, re, time, threading, uuid
from flask import Flask, jsonify, request, Response, render_template_string

app = Flask(__name__)

# ── Config ──────────────────────────────────────────
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
IFACES = {"wired": "enp0s31f6", "wireless": "wlp6s0"}
COUNTRY_FLAGS = {
    "CN":"🇨🇳","US":"🇺🇸","HK":"🇭🇰","JP":"🇯🇵","SG":"🇸🇬",
    "DE":"🇩🇪","GB":"🇬🇧","FR":"🇫🇷","AU":"🇦🇺","KR":"🇰🇷",
    "TW":"🇹🇼","NL":"🇳🇱","CA":"🇨🇦","RU":"🇷🇺","IN":"🇮🇳",
}

# Background task store
_tasks = {}  # task_id -> {status, results, done}

# ── Shell helper ─────────────────────────────────────
def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"

# ── Network info ─────────────────────────────────────
def iface_info(name):
    code, out, _ = sh(f"ip -j addr show {name}")
    if code != 0 or not out:
        return {"state":"missing","ip":None,"mac":None,"speed":None,"rx_bytes":0,"tx_bytes":0}
    try:
        data = json.loads(out)[0]
    except:
        return {"state":"error","ip":None,"mac":None,"rx_bytes":0,"tx_bytes":0}
    state = "up" if data.get("operstate") == "UP" else "down"
    ip = next((f"{a['local']}/{a['prefixlen']}" for a in data.get("addr_info",[]) if a.get("family")=="inet"), None)
    _, spd, _ = sh(f"cat /sys/class/net/{name}/speed 2>/dev/null")
    _, rx, _  = sh(f"cat /sys/class/net/{name}/statistics/rx_bytes 2>/dev/null")
    _, tx, _  = sh(f"cat /sys/class/net/{name}/statistics/tx_bytes 2>/dev/null")
    return {
        "state": state, "ip": ip, "mac": data.get("address"),
        "speed": f"{spd} Mbps" if spd.isdigit() else None,
        "rx_bytes": int(rx) if rx.isdigit() else 0,
        "tx_bytes": int(tx) if tx.isdigit() else 0,
    }

def wifi_active():
    _, out, _ = sh("nmcli -t -f ACTIVE,SSID,SIGNAL,CHAN,RATE dev wifi")
    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == "yes" and len(parts) >= 5:
            return {"ssid":parts[1],"signal":int(parts[2]) if parts[2].isdigit() else 0,
                    "channel":parts[3],"rate":parts[4]}
    return {}

def active_gateway():
    _, out, _ = sh("ip route show default")
    for iface in IFACES.values():
        if iface in out:
            return iface
    return None

def geoip_via(iface, timeout=7):
    """Query exit IP + geo info through a specific network interface."""
    code, out, _ = sh(
        f"curl --interface {iface} -s --connect-timeout {timeout} --max-time {timeout+2} "
        f"'http://ip-api.com/json?fields=status,query,country,countryCode,regionName,city,isp,org,lat,lon'",
        timeout=timeout+5
    )
    if code != 0 or not out:
        return None
    try:
        d = json.loads(out)
        return d if d.get("status") == "success" else None
    except:
        return None

def ping_ms(iface, target, count=3):
    """Return average ping RTT in ms, or None if unreachable."""
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
    l = (load.split()[:3] + ["?","?","?"])[:3]
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
        "hostname": host, "kernel": kernel, "uptime": uptime,
        "load": {"1m":l[0],"5m":l[1],"15m":l[2]},
        "mem":  {"total":int(mp[0]) if len(mp)>0 else 0, "used":int(mp[1]) if len(mp)>1 else 0},
        "swap": {"total":int(sp[0]) if len(sp)>0 else 0, "used":int(sp[1]) if len(sp)>1 else 0},
        "disk": {"total":int(dp[0]) if len(dp)>0 else 0, "used":int(dp[1]) if len(dp)>1 else 0, "pct":dp[2] if len(dp)>2 else "?"},
        "gpu": gpu or None,
    }

# ── API: Status ──────────────────────────────────────
@app.route("/api/status")
def api_status():
    wired    = iface_info(IFACES["wired"])
    wireless = iface_info(IFACES["wireless"])
    wifie    = wifi_active() if wireless["state"] == "up" else {}
    dns_mode, dns_servers = current_dns_mode()
    return jsonify({
        "ts": int(time.time()),
        "gateway": active_gateway(),
        "interfaces": {"wired": wired, "wireless": {**wireless, **wifie}},
        "dns": {"mode": dns_mode, "servers": dns_servers, "presets": DNS_PRESETS},
        "mirror": {"mode": current_mirror_mode(),
                   "presets": {k:{"label":v["label"],"url":v["url"]} for k,v in MIRROR_PRESETS.items()}},
        "sysinfo": system_info(),
    })

# ── API: Per-interface intelligence ─────────────────
@app.route("/api/intel/<iface_key>")
def api_intel(iface_key):
    if iface_key not in IFACES:
        return jsonify({"ok":False,"msg":"Unknown interface"}), 400
    iface = IFACES[iface_key]
    info = iface_info(iface)
    if info["state"] != "up":
        return jsonify({"ok":False,"msg":f"{iface} 未在线"})
    geo    = geoip_via(iface)
    lat_cn = ping_ms(iface, "223.5.5.5")
    lat_gl = ping_ms(iface, "1.1.1.1")
    return jsonify({
        "ok": True,
        "geo": geo,
        "flag": COUNTRY_FLAGS.get(geo.get("countryCode","") if geo else "", "🌍"),
        "latency": {"domestic_ms": lat_cn, "international_ms": lat_gl},
    })

# ── API: WiFi scan ───────────────────────────────────
@app.route("/api/wifi/scan")
def api_wifi_scan():
    sh("nmcli dev wifi rescan 2>/dev/null")
    time.sleep(1.5)
    _, out, _ = sh("nmcli -t -f SSID,SIGNAL,SECURITY,CHAN,IN-USE dev wifi list")
    nets, seen = [], set()
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 4:
            ssid = parts[0].strip()
            if ssid and ssid not in seen:
                seen.add(ssid)
                nets.append({
                    "ssid": ssid,
                    "signal": int(parts[1]) if parts[1].isdigit() else 0,
                    "security": parts[2] if parts[2] else "Open",
                    "channel": parts[3],
                    "in_use": len(parts) > 4 and parts[4].strip() == "*",
                })
    nets.sort(key=lambda x: -x["signal"])
    return jsonify({"ok": True, "networks": nets})

# ── API: Connect single WiFi ─────────────────────────
@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    body = request.get_json() or {}
    ssid = body.get("ssid","").strip()
    pwd  = body.get("password","").strip()
    if not ssid:
        return jsonify({"ok":False,"msg":"SSID 不能为空"}), 400
    # Delete stale profile then reconnect
    sh(f"nmcli con delete id '{ssid}' 2>/dev/null")
    if pwd:
        code, out, err = sh(f"nmcli dev wifi connect '{ssid}' password '{pwd}' ifname wlp6s0", timeout=30)
    else:
        code, out, err = sh(f"nmcli dev wifi connect '{ssid}' ifname wlp6s0", timeout=30)
    ok = code == 0 and "successfully activated" in out.lower()
    if ok:
        _, ip, _ = sh("ip -4 addr show wlp6s0 | grep -oP '(?<=inet )[0-9./]+'")
        return jsonify({"ok":True,"msg":f"已连接 {ssid}，IP: {ip}"})
    return jsonify({"ok":False,"msg": out or err or "连接失败，请检查密码"})

@app.route("/api/wifi/disconnect", methods=["POST"])
def api_wifi_disconnect():
    sh("nmcli dev disconnect wlp6s0")
    return jsonify({"ok":True,"msg":"无线已断开"})

# ── API: Bulk WiFi test (background + poll) ──────────
def _run_bulk_test(task_id, entries):
    """
    Run in background thread.
    SAFETY: wired interface (enp0s31f6) + Tailscale stays up throughout.
    We only toggle the wlp6s0 WiFi connection; the panel itself never goes down.
    """
    task = _tasks[task_id]
    # Save currently connected WiFi
    _, orig_ssid, _ = sh("nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d: -f2")

    for i, entry in enumerate(entries):
        ssid = entry.get("ssid","").strip()
        pwd  = entry.get("password","").strip()
        if not ssid:
            continue

        task["current"] = ssid
        task["progress"] = i

        result = {"ssid": ssid, "status": "testing"}
        task["results"].append(result)

        # Try to connect
        sh(f"nmcli con delete id '{ssid}' 2>/dev/null")
        if pwd:
            code, out, err = sh(f"nmcli dev wifi connect '{ssid}' password '{pwd}' ifname wlp6s0", timeout=25)
        else:
            code, out, err = sh(f"nmcli dev wifi connect '{ssid}' ifname wlp6s0", timeout=25)

        connected = code == 0 and "successfully activated" in out.lower()
        if not connected:
            result.update({"status":"failed","error":"密码错误或无信号"})
            continue

        time.sleep(1.2)  # wait for IP assignment
        _, local_ip, _ = sh("ip -4 addr show wlp6s0 | grep -oP '(?<=inet )[0-9./]+'")

        # GeoIP (ip-api.com free: 45 req/min, rate-safe with delay)
        geo    = geoip_via("wlp6s0", timeout=7)
        lat_cn = ping_ms("wlp6s0", "223.5.5.5", 3)
        lat_gl = ping_ms("wlp6s0", "1.1.1.1", 3)
        time.sleep(1.5)  # rate-limit guard for ip-api.com

        cc = geo.get("countryCode","") if geo else ""
        result.update({
            "status": "ok",
            "local_ip": local_ip,
            "geo": geo,
            "flag": COUNTRY_FLAGS.get(cc,"🌍"),
            "latency": {"domestic_ms": lat_cn, "international_ms": lat_gl},
        })

    # Restore original WiFi connection
    if orig_ssid:
        sh(f"nmcli con up id '{orig_ssid}' 2>/dev/null", timeout=20)
    else:
        sh("nmcli dev disconnect wlp6s0 2>/dev/null")

    task["done"] = True
    task["current"] = None

@app.route("/api/wifi/bulk-start", methods=["POST"])
def api_bulk_start():
    body = request.get_json() or {}
    entries = body.get("entries", [])
    if not entries:
        return jsonify({"ok":False,"msg":"没有条目"}), 400
    task_id = str(uuid.uuid4())[:8]
    _tasks[task_id] = {"done":False,"current":None,"progress":0,"results":[],"total":len(entries)}
    t = threading.Thread(target=_run_bulk_test, args=(task_id, entries), daemon=True)
    t.start()
    return jsonify({"ok":True,"task_id":task_id})

@app.route("/api/wifi/bulk-status/<task_id>")
def api_bulk_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"ok":False,"msg":"Task not found"}), 404
    return jsonify({
        "ok": True,
        "done": task["done"],
        "current": task["current"],
        "progress": task["progress"],
        "total": task["total"],
        "results": task["results"],
    })

# ── API: Network control ─────────────────────────────
@app.route("/api/network/<iface_key>/<action>", methods=["POST"])
def api_network(iface_key, action):
    if iface_key not in IFACES:
        return jsonify({"ok":False,"msg":"Unknown interface"}), 400
    iface = IFACES[iface_key]
    if action == "up":
        sh(f"ip link set {iface} up")
        if iface_key == "wireless": sh(f"nmcli dev connect {iface}")
    elif action == "down":
        if iface_key == "wireless": sh(f"nmcli dev disconnect {iface}")
        sh(f"ip link set {iface} down")
    return jsonify({"ok":True,"msg":f"{iface} {action}"})

@app.route("/api/network/route/<iface_key>", methods=["POST"])
def api_set_route(iface_key):
    if iface_key not in IFACES:
        return jsonify({"ok":False,"msg":"Unknown interface"}), 400
    iface = IFACES[iface_key]
    _, routes, _ = sh("ip route show default")
    gws = {}
    for line in routes.splitlines():
        m = re.search(r"default via (\S+) dev (\S+)", line)
        if m: gws[m.group(2)] = m.group(1)
    if iface not in gws:
        return jsonify({"ok":False,"msg":f"{iface} 当前无默认路由，接口需在线并已获取 IP"}), 400
    for i, (dev, gw) in enumerate(gws.items()):
        metric = 100 if dev == iface else 200 + i * 10
        sh(f"ip route del default via {gw} dev {dev} 2>/dev/null")
        sh(f"ip route add default via {gw} dev {dev} metric {metric}")
    return jsonify({"ok":True,"msg":f"默认出口已切换至 {iface_key} ({iface})"})

# ── API: DNS ─────────────────────────────────────────
@app.route("/api/dns/<mode>", methods=["POST"])
def api_dns(mode):
    if mode not in DNS_PRESETS:
        return jsonify({"ok":False,"msg":"Invalid mode"}), 400
    p = DNS_PRESETS[mode]
    content = f"# ControlPanel — {p['label']}\n" + "\n".join(f"nameserver {s}" for s in p["servers"]) + "\n"
    # Safely overwrite (unlock if immutable, write, lock again)
    sh("chattr -i /etc/resolv.conf 2>/dev/null")
    sh("rm -f /etc/resolv.conf")
    with open("/etc/resolv.conf","w") as f:
        f.write(content)
    sh("chattr +i /etc/resolv.conf")
    return jsonify({"ok":True,"msg":f"DNS → {p['label']}: {', '.join(p['servers'])}"})

# ── API: Mirror ──────────────────────────────────────
@app.route("/api/mirror/<mode>", methods=["POST"])
def api_mirror(mode):
    if mode not in MIRROR_PRESETS:
        return jsonify({"ok":False,"msg":"Invalid mode"}), 400
    p = MIRROR_PRESETS[mode]
    with open("/etc/apt/sources.list","w") as f:
        f.write(p["content"])
    _, out, _ = sh("apt-get update -q 2>&1 | tail -n 4", timeout=60)
    err_flag = "error" in out.lower() or "err:" in out.lower()
    return jsonify({"ok":True,"msg":f"镜像源 → {p['label']} {'⚠️ '+out[:100] if err_flag else '✅ apt update 成功'}"})

# ── Frontend HTML ────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HOMEHOSTINGSER · 控制面板</title>
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

/* header */
header{background:linear-gradient(135deg,#0a0e28,#0c1222);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:11px}
.logo{width:32px;height:32px;background:linear-gradient(135deg,var(--accent),var(--cyan));border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.ht{font-size:.9rem;font-weight:700;letter-spacing:.2px}
.hs{font-size:.67rem;color:var(--t2);margin-top:2px}
.hr{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chip{background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:4px;padding:3px 8px;font-size:.67rem;color:var(--t2)}
.chip b{color:var(--text)}
.rbtn{background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.23);color:#a5b4fc;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:.69rem;font-weight:600;transition:.15s}
.rbtn:hover{background:rgba(99,102,241,.22)}
#cd{font-size:.65rem;color:var(--t3);min-width:24px;text-align:right}

/* layout */
.wrap{max-width:1060px;margin:0 auto;padding:15px 12px;display:grid;gap:11px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
@media(max-width:620px){.g2{grid-template-columns:1fr}}

/* card */
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.chd{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:7px}
.chd .ico{width:22px;height:22px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:12px}
.chd h2{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--t2)}
.cb{padding:12px 14px}

/* iface block */
.ifc{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:10px 12px;margin-bottom:8px;transition:.2s}
.ifc:last-of-type{margin-bottom:0}
.ifc.gw{border-color:rgba(99,102,241,.38);box-shadow:inset 0 0 0 1px rgba(99,102,241,.1),0 0 12px rgba(99,102,241,.05)}
.ifc-hd{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.ifc-label{font-size:.8rem;font-weight:700}
.ifc-sub{font-size:.65rem;color:var(--t3);margin-top:1px}
.ifc-acts{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap;align-items:center}

/* intelligence strip */
.intel-strip{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;padding:7px 11px;margin-bottom:8px;min-height:34px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:.73rem;transition:.3s}
.intel-strip.ok{border-color:rgba(34,211,238,.18);background:rgba(34,211,238,.03)}
.intel-strip.err{border-color:rgba(239,68,68,.2)}
.i-flag{font-size:1.2rem;line-height:1}
.i-place{font-size:.78rem;font-weight:700}
.i-isp{font-size:.67rem;color:var(--t2)}
.i-ip{font-family:monospace;font-size:.72rem;color:var(--cyan)}
.lat-wrap{display:flex;gap:8px}
.lat .k{font-size:.6rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.lat .v{font-size:.76rem;font-weight:700;margin-top:1px}
.v-g{color:var(--green)}.v-m{color:var(--yellow)}.v-b{color:var(--red)}.v-u{color:var(--t3)}
.intel-strip .tbtn{margin-left:auto;white-space:nowrap;background:rgba(34,211,238,.08);border:1px solid rgba(34,211,238,.18);color:var(--cyan);border-radius:4px;padding:3px 8px;cursor:pointer;font-size:.66rem;font-weight:600;transition:.15s}
.intel-strip .tbtn:hover{background:rgba(34,211,238,.16)}

/* stats */
.stats{display:flex;gap:10px;flex-wrap:wrap}
.st .k{font-size:.62rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.st .v{font-size:.73rem;font-weight:600;margin-top:1px}
.sbar{display:flex;gap:2px;align-items:flex-end}
.sbar s{width:3px;border-radius:1px;background:var(--border2);display:inline-block}
.sbar s.lit{background:var(--green)}

/* pills */
.pill{display:inline-flex;align-items:center;gap:3px;border-radius:3px;padding:1px 6px;font-size:.65rem;font-weight:700}
.pill::before{content:'';width:5px;height:5px;border-radius:50%}
.p-up{background:rgba(34,197,94,.09);color:var(--green)}.p-up::before{background:var(--green)}
.p-dn{background:rgba(239,68,68,.09);color:var(--red)}.p-dn::before{background:var(--red)}
.p-unk{background:rgba(245,158,11,.09);color:var(--yellow)}.p-unk::before{background:var(--yellow)}
.p-info{background:rgba(34,211,238,.09);color:var(--cyan)}.p-info::before{background:var(--cyan)}
.gw-b{background:rgba(99,102,241,.13);color:#a5b4fc;border:1px solid rgba(99,102,241,.25);border-radius:3px;font-size:.61rem;font-weight:700;padding:1px 5px}

/* buttons */
.btn{border:none;cursor:pointer;border-radius:5px;padding:4px 9px;font-size:.68rem;font-weight:700;transition:.13s;white-space:nowrap}
.bg{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2)}.bg:hover{background:rgba(34,197,94,.2)}
.br{background:rgba(239,68,68,.09);color:var(--red);border:1px solid rgba(239,68,68,.18)}.br:hover{background:rgba(239,68,68,.18)}
.ba{background:rgba(99,102,241,.11);color:#a5b4fc;border:1px solid rgba(99,102,241,.23)}.ba:hover{background:rgba(99,102,241,.23)}
.bc{background:rgba(34,211,238,.09);color:var(--cyan);border:1px solid rgba(34,211,238,.19)}.bc:hover{background:rgba(34,211,238,.19)}
.bd{background:rgba(255,255,255,.04);color:var(--t2);border:1px solid var(--border)}.bd:hover{background:rgba(255,255,255,.08)}
.byl{background:rgba(245,158,11,.1);color:var(--yellow);border:1px solid rgba(245,158,11,.22)}.byl:hover{background:rgba(245,158,11,.2)}
.btn:disabled{opacity:.28;cursor:not-allowed}

/* seg */
.seg{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:7px;overflow:hidden;width:100%}
.sb{flex:1;border:none;cursor:pointer;padding:7px 3px;font-size:.69rem;font-weight:600;background:transparent;color:var(--t2);transition:.13s;text-align:center;line-height:1.4}
.sb:hover:not(.active){background:rgba(255,255,255,.03)}
.sb.active{background:var(--accent);color:#fff;box-shadow:0 0 10px var(--ag)}
.sb small{display:block;font-size:.59rem;font-weight:400;opacity:.7;margin-top:1px}

.divider{height:1px;background:var(--border);margin:9px 0}
.ir{padding:7px 0;border-bottom:1px solid var(--border)}
.ir:last-of-type{border:none;padding-bottom:0}
.ir-l{font-size:.64rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.ir-v{font-size:.78rem;font-weight:600;margin-top:2px}
.ir-s{font-size:.66rem;color:var(--t3);margin-top:1px}

/* sysinfo */
.sg{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
@media(max-width:460px){.sg{grid-template-columns:1fr 1fr}}
.si{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px 10px}
.si .k{font-size:.6rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3)}
.si .v{font-size:.8rem;font-weight:700;margin-top:2px}
.si .v2{font-size:.66rem;color:var(--t2);margin-top:1px}
.bar{height:3px;background:var(--border2);border-radius:2px;margin-top:5px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .5s}
.bgg .bar-fill{background:var(--green)}.bga .bar-fill{background:var(--accent)}.bgy .bar-fill{background:var(--yellow)}
.gpu{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px 11px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:7px}
.gn{font-size:.75rem;font-weight:700;flex:1;min-width:110px}
.gv .k{font-size:.59rem;color:var(--t3);text-transform:uppercase}
.gv .v{font-size:.77rem;font-weight:700;color:var(--cyan)}

/* ══ WiFi Modal ══ */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.78);display:none;align-items:flex-start;justify-content:center;z-index:200;padding:16px;overflow-y:auto;backdrop-filter:blur(4px)}
.ov.open{display:flex}
.modal{background:#0d1022;border:1px solid var(--border2);border-radius:12px;width:680px;max-width:96vw;box-shadow:0 20px 60px rgba(0,0,0,.6);margin:auto}
.mhd{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px}
.mhd h3{font-size:.87rem;font-weight:700}
.mcl{margin-left:auto;background:none;border:none;color:var(--t3);cursor:pointer;font-size:1.05rem;padding:2px 5px;border-radius:3px}
.mcl:hover{color:var(--text);background:rgba(255,255,255,.06)}
.mbody{padding:16px 18px}
.mft{padding:12px 18px;border-top:1px solid var(--border);display:flex;gap:7px;justify-content:flex-end;align-items:center}

/* scan table */
.stbl{width:100%;border-collapse:collapse}
.stbl thead tr{background:rgba(255,255,255,.03)}
.stbl th{padding:6px 10px;font-size:.62rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3);font-weight:600;border-bottom:1px solid var(--border);text-align:left}
.stbl td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
.stbl tr:last-child td{border:none}
.pw-inp{width:100%;background:var(--bg);border:1px solid var(--border2);border-radius:5px;padding:5px 8px;color:var(--text);font-size:.75rem;outline:none;transition:.13s}
.pw-inp:focus{border-color:var(--accent)}
.pw-wrap2{position:relative}
.pw-wrap2 input{padding-right:28px}
.pw-eye2{position:absolute;right:7px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--t3);cursor:pointer;font-size:12px;padding:1px}
.pw-eye2:hover{color:var(--text)}

/* progress */
.prog-wrap{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:10px 12px;margin-bottom:11px;display:none}
.prog-bar-bg{height:5px;background:var(--border2);border-radius:3px;overflow:hidden;margin-top:6px}
.prog-bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--cyan));border-radius:3px;transition:width .4s}

/* results table */
.rtbl{width:100%;border-collapse:collapse;font-size:.74rem}
.rtbl th{background:rgba(255,255,255,.03);padding:5px 9px;text-align:left;font-size:.61rem;text-transform:uppercase;letter-spacing:.3px;color:var(--t3);font-weight:600;border-bottom:1px solid var(--border)}
.rtbl td{padding:7px 9px;border-bottom:1px solid var(--border);vertical-align:middle}
.rtbl tr:last-child td{border:none}
.rrow{cursor:pointer;transition:.12s}
.rrow:hover{background:rgba(255,255,255,.03)}
.rrow.sel{background:rgba(99,102,241,.08)}
.rrow.sel td:first-child{border-left:2px solid var(--accent)}
.lbg{display:inline-block;border-radius:3px;padding:1px 5px;font-weight:700;font-size:.68rem}
.lg{background:rgba(34,197,94,.11);color:var(--green)}.lm{background:rgba(245,158,11,.11);color:var(--yellow)}.lb{background:rgba(239,68,68,.09);color:var(--red)}.lu{background:rgba(255,255,255,.05);color:var(--t3)}

/* toast */
.toast{position:fixed;bottom:18px;right:18px;background:#121522;border:1px solid var(--border2);border-radius:8px;padding:10px 14px;font-size:.75rem;max-width:340px;z-index:9999;box-shadow:0 8px 36px rgba(0,0,0,.5);display:none;line-height:1.5}
.toast.show{display:block;animation:tIn .2s ease}
@keyframes tIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.spin{animation:spin 1s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
code{background:rgba(255,255,255,.06);border-radius:3px;padding:1px 4px;font-size:.85em}
footer{text-align:center;padding:13px;color:var(--t3);font-size:.64rem;border-top:1px solid var(--border)}
</style>
</head>
<body>

<header>
  <div class="logo">🖥</div>
  <div>
    <div class="ht">HOMEHOSTINGSER 控制面板</div>
    <div class="hs" id="hdr-sub">Debian 13 Trixie · 加载中…</div>
  </div>
  <div class="hr">
    <div class="chip">Tailscale <b>100.92.52.85</b></div>
    <div class="chip">IP <b id="hdr-ip">—</b></div>
    <button class="rbtn" onclick="doRefresh()">⟳ 刷新</button>
    <span id="cd"></span>
  </div>
</header>

<div class="wrap">

<!-- ── NETWORK ── -->
<div class="card">
  <div class="chd">
    <div class="ico" style="background:rgba(34,211,238,.07)">🌐</div>
    <h2>网络接口 & 路由管理</h2>
    <button class="btn bc" style="margin-left:auto;font-size:.68rem" onclick="openWifi()">📶 Wi-Fi 配置 / 出口检测</button>
  </div>
  <div class="cb">

    <!-- WIRED -->
    <div class="ifc" id="ifc-wired">
      <div class="ifc-hd">
        <span style="font-size:16px">🔌</span>
        <div>
          <div class="ifc-label">有线网络 <code style="font-size:.66rem;color:var(--t3)">enp0s31f6</code></div>
          <div class="ifc-sub">Intel I219-V 千兆以太网</div>
        </div>
        <div class="ifc-acts">
          <span id="wired-gw" class="gw-b" style="display:none">默认出口</span>
          <span id="wired-pill" class="pill">…</span>
          <button class="btn bg" onclick="netAct('wired','up')">开启</button>
          <button class="btn br" onclick="netAct('wired','down')">关闭</button>
          <button class="btn ba" onclick="setRoute('wired')">设为出口</button>
        </div>
      </div>
      <div class="intel-strip" id="wired-intel">
        <span style="color:var(--t3);font-size:.7rem">点击检测查看真实出口 IP、国家、延迟</span>
        <button class="tbtn" onclick="testIntel('wired')">🔍 检测出口</button>
      </div>
      <div class="stats">
        <div class="st"><div class="k">本地 IP</div><div class="v" id="wired-ip">—</div></div>
        <div class="st"><div class="k">MAC</div><div class="v" id="wired-mac">—</div></div>
        <div class="st"><div class="k">速率</div><div class="v" id="wired-speed">—</div></div>
        <div class="st"><div class="k">↓ 已收</div><div class="v" id="wired-rx">—</div></div>
        <div class="st"><div class="k">↑ 已发</div><div class="v" id="wired-tx">—</div></div>
      </div>
    </div>

    <!-- WIRELESS -->
    <div class="ifc" id="ifc-wifi">
      <div class="ifc-hd">
        <span style="font-size:16px">📶</span>
        <div>
          <div class="ifc-label">无线网络 <code style="font-size:.66rem;color:var(--t3)">wlp6s0</code></div>
          <div class="ifc-sub">MediaTek MT7921 802.11ax Wi-Fi 6</div>
        </div>
        <div class="ifc-acts">
          <span id="wifi-gw" class="gw-b" style="display:none">默认出口</span>
          <span id="wifi-pill" class="pill">…</span>
          <button class="btn bg" onclick="netAct('wireless','up')">开启</button>
          <button class="btn br" onclick="wifiDisc()">断开</button>
          <button class="btn ba" onclick="setRoute('wireless')">设为出口</button>
        </div>
      </div>
      <div class="intel-strip" id="wifi-intel">
        <span style="color:var(--t3);font-size:.7rem">点击检测查看真实出口 IP、国家、延迟</span>
        <button class="tbtn" onclick="testIntel('wireless')">🔍 检测出口</button>
      </div>
      <div class="stats">
        <div class="st"><div class="k">本地 IP</div><div class="v" id="wifi-ip">—</div></div>
        <div class="st"><div class="k">SSID</div><div class="v" id="wifi-ssid">—</div></div>
        <div class="st"><div class="k">信道</div><div class="v" id="wifi-chan">—</div></div>
        <div class="st"><div class="k">速率</div><div class="v" id="wifi-rate">—</div></div>
        <div class="st">
          <div class="k">信号</div>
          <div class="v" style="display:flex;align-items:center;gap:5px">
            <div class="sbar" id="sbar"><s style="height:4px"></s><s style="height:7px"></s><s style="height:10px"></s><s style="height:13px"></s><s style="height:16px"></s></div>
            <span id="wifi-sig">—</span>
          </div>
        </div>
        <div class="st"><div class="k">↓ 已收</div><div class="v" id="wifi-rx">—</div></div>
        <div class="st"><div class="k">↑ 已发</div><div class="v" id="wifi-tx">—</div></div>
      </div>
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
      <div class="ir"><div class="ir-l">当前 DNS 服务器</div><div class="ir-v" id="dns-sv">—</div><div class="ir-s" id="dns-note">—</div></div>
      <div class="divider"></div>
      <div class="seg">
        <button class="sb" id="dns-d" onclick="setDNS('domestic')">🇨🇳 国内<small>阿里云 / 114</small></button>
        <button class="sb" id="dns-o" onclick="setDNS('overseas')">🌐 国际<small>Cloudflare / Google</small></button>
      </div>
      <div style="margin-top:8px;font-size:.66rem;color:var(--t3);line-height:1.8">
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
      <div style="margin-top:8px;font-size:.66rem;color:var(--t3)">切换后自动运行 <code>apt update</code>，约需 10~20 秒</div>
    </div>
  </div>
</div>

<!-- ── SYSINFO ── -->
<div class="card">
  <div class="chd">
    <div class="ico" style="background:rgba(245,158,11,.06)">📊</div>
    <h2>系统状态</h2>
    <span style="margin-left:auto;font-size:.65rem;color:var(--t3)" id="si-ts"></span>
  </div>
  <div class="cb">
    <div class="sg">
      <div class="si bgg"><div class="k">内存</div><div class="v" id="si-mem">—</div><div class="v2" id="si-mem2">—</div><div class="bar"><div class="bar-fill" id="bm" style="width:0%"></div></div></div>
      <div class="si bga"><div class="k">磁盘 /</div><div class="v" id="si-disk">—</div><div class="v2" id="si-disk2">—</div><div class="bar"><div class="bar-fill" id="bd" style="width:0%"></div></div></div>
      <div class="si bgy"><div class="k">Swap</div><div class="v" id="si-swap">—</div><div class="v2" id="si-swap2">—</div><div class="bar"><div class="bar-fill" id="bs" style="width:0%"></div></div></div>
      <div class="si"><div class="k">负载 1m / 5m / 15m</div><div class="v" id="si-load">—</div></div>
      <div class="si"><div class="k">运行时长</div><div class="v" id="si-up">—</div></div>
      <div class="si"><div class="k">内核版本</div><div class="v" id="si-ker" style="font-size:.67rem">—</div></div>
    </div>
    <div id="gpu-row"></div>
  </div>
</div>
</div><!-- /wrap -->

<!-- ══ Wi-Fi 配置面板 Modal ═══════════════════════════════════════════ -->
<div class="ov" id="wifi-ov" onclick="if(event.target===this)closeWifi()">
  <div class="modal">
    <div class="mhd">
      <span style="font-size:16px">📶</span>
      <h3>Wi-Fi 配置 & 出口网络检测</h3>
      <button class="mcl" onclick="closeWifi()">✕</button>
    </div>
    <div class="mbody">

      <div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);border-radius:6px;padding:8px 12px;font-size:.7rem;color:var(--yellow);margin-bottom:12px;line-height:1.6">
        💡 <b>工作原理：</b>检测时系统会依次尝试连接每个 Wi-Fi，通过真实网络查询出口 IP 与延迟，完成后自动恢复原有连接。<br>
        ✅ 全程有线网络（<code>enp0s31f6</code>）和 Tailscale 保持在线，控制面板不会断连。
      </div>

      <div style="display:flex;gap:7px;align-items:center;margin-bottom:10px">
        <span style="font-size:.73rem;color:var(--t2);font-weight:600">第一步：扫描并输入密码</span>
        <button class="btn bc" style="margin-left:auto" onclick="doScan()">🔍 扫描周边网络</button>
      </div>

      <!-- Scan table -->
      <div style="border:1px solid var(--border);border-radius:7px;overflow:hidden;margin-bottom:11px;max-height:250px;overflow-y:auto" id="scan-wrap">
        <div style="padding:18px;text-align:center;color:var(--t3);font-size:.75rem">点击「扫描周边网络」开始</div>
      </div>

      <!-- Progress -->
      <div class="prog-wrap" id="prog-wrap">
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:.72rem">
          <span>正在检测：<b id="prog-cur" style="color:var(--cyan)">—</b></span>
          <span id="prog-txt" style="color:var(--t3)">0 / 0</span>
        </div>
        <div class="prog-bar-bg"><div class="prog-bar-fill" id="prog-bar" style="width:0%"></div></div>
      </div>

      <!-- Results -->
      <div id="results-section" style="display:none">
        <div style="font-size:.71rem;color:var(--t2);margin-bottom:7px;font-weight:600">
          第二步：检测结果 · <span id="res-count" style="color:var(--t3)"></span>
          <span style="float:right;font-weight:400;font-size:.67rem;color:var(--t3)">点击行即可切换至该网络</span>
        </div>
        <div style="overflow-x:auto;border:1px solid var(--border);border-radius:7px;overflow:hidden">
          <table class="rtbl">
            <thead><tr><th>SSID</th><th>出口 IP</th><th>国家/地区</th><th>运营商</th><th>城市</th><th>🇨🇳 国内延迟</th><th>🌐 国际延迟</th><th>状态</th></tr></thead>
            <tbody id="res-body"></tbody>
          </table>
        </div>
      </div>

    </div>
    <div class="mft">
      <span id="bulk-st" style="font-size:.7rem;color:var(--t3);flex:1"></span>
      <button class="btn bd" onclick="closeWifi()">关闭</button>
      <button class="btn byl" id="test-btn" onclick="doTestAll()" disabled>⚡ 全部检测</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<footer>HOMEHOSTINGSER · Debian 13 Trixie · i7-7700 · Quadro P600 · MT7921 Wi-Fi 6 · Tailscale 100.92.52.85</footer>

<script>
/* ── Utils ── */
function fmtB(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++;}return b.toFixed(1)+' '+u[i];}
function pct(u,t){return t>0?Math.round(u/t*100):0}
function pillC(s){return s==='up'?'pill p-up':s==='down'?'pill p-dn':'pill p-unk'}
function pillL(s){return s==='up'?'在线':s==='down'?'离线':'未知'}
function sigs(id,p){const b=document.querySelectorAll('#'+id+' s');const l=Math.round(p/20);b.forEach((e,i)=>e.classList.toggle('lit',i<l));}
function latC(ms){if(ms==null)return 'lu';if(ms<60)return 'lg';if(ms<150)return 'lm';return 'lb';}
function latB(ms){return ms==null?'<span class="lbg lu">超时</span>':`<span class="lbg ${latC(ms)}">${ms} ms</span>`;}

let _tt=null;
function toast(msg,ok=true,dur=4000){
  const t=document.getElementById('toast');t.innerHTML=msg;
  t.style.borderColor=ok?'rgba(34,197,94,.28)':'rgba(239,68,68,.3)';
  t.className='toast show';clearTimeout(_tt);_tt=setTimeout(()=>t.className='toast',dur);
}

/* ── Status ── */
async function doRefresh(){
  cdSec=15;document.getElementById('cd').textContent='';
  const rb=document.querySelector('.rbtn');rb.innerHTML='<span class="spin">⟳</span>';
  try{const d=await fetch('/api/status').then(r=>r.json());applyStatus(d);}
  catch(e){toast('刷新失败: '+e,false);}
  rb.innerHTML='⟳ 刷新';
}

function applyStatus(d){
  const {interfaces:ifc,dns,mirror,sysinfo:s,gateway}=d;
  const W=ifc.wired,WF=ifc.wireless;

  document.getElementById('hdr-sub').textContent=`${s.hostname} · ${s.kernel} · 负载 ${s.load['1m']}`;
  document.getElementById('hdr-ip').textContent=W.ip?.split('/')[0]||WF.ip?.split('/')[0]||'—';
  document.getElementById('si-ts').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');

  // Wired
  let wp=document.getElementById('wired-pill');wp.textContent=pillL(W.state);wp.className=pillC(W.state);
  document.getElementById('wired-ip').textContent=W.ip||'—';
  document.getElementById('wired-mac').textContent=W.mac||'—';
  document.getElementById('wired-speed').textContent=W.speed||'—';
  document.getElementById('wired-rx').textContent=fmtB(W.rx_bytes||0);
  document.getElementById('wired-tx').textContent=fmtB(W.tx_bytes||0);
  const wGW=gateway==='enp0s31f6';
  document.getElementById('wired-gw').style.display=wGW?'':'none';
  document.getElementById('ifc-wired').classList.toggle('gw',wGW);

  // Wireless
  let wfp=document.getElementById('wifi-pill');wfp.textContent=pillL(WF.state);wfp.className=pillC(WF.state);
  document.getElementById('wifi-ip').textContent=WF.ip||'—';
  document.getElementById('wifi-ssid').textContent=WF.ssid||'—';
  document.getElementById('wifi-chan').textContent=WF.channel||'—';
  document.getElementById('wifi-rate').textContent=WF.rate||'—';
  document.getElementById('wifi-sig').textContent=(WF.signal||0)+'%';
  sigs('sbar',WF.signal||0);
  document.getElementById('wifi-rx').textContent=fmtB(WF.rx_bytes||0);
  document.getElementById('wifi-tx').textContent=fmtB(WF.tx_bytes||0);
  const wfGW=gateway==='wlp6s0';
  document.getElementById('wifi-gw').style.display=wfGW?'':'none';
  document.getElementById('ifc-wifi').classList.toggle('gw',wfGW);

  // DNS
  const dm=dns.mode;
  const dl={domestic:'🇨🇳 国内 DNS',overseas:'🌐 国际 DNS',custom:'⚙️ 自定义'};
  const dp=document.getElementById('dns-pill');dp.textContent=dl[dm]||dm;
  dp.className='pill '+(dm==='domestic'?'p-up':dm==='overseas'?'p-info':'p-unk');
  document.getElementById('dns-sv').textContent=(dns.servers||[]).join(' · ')||'—';
  document.getElementById('dns-note').textContent=dns.presets?.[dm]?.note||'';
  ['domestic','overseas'].forEach(m=>document.getElementById('dns-'+m[0]).className='sb'+(dm===m?' active':''));

  // Mirror
  const mm=mirror.mode;
  const ml={domestic:'🇨🇳 清华 TUNA',ustc:'🎓 中科大 USTC',overseas:'🌐 官方源',custom:'⚙️ 自定义'};
  const mp=document.getElementById('mir-pill');mp.textContent=ml[mm]||mm;
  mp.className='pill '+(mm==='overseas'?'p-info':'p-up');
  const mpm=mirror.presets?.[mm];
  document.getElementById('mir-name').textContent=mpm?.label||mm;
  document.getElementById('mir-url').textContent=mpm?.url||'—';
  ['domestic','ustc','overseas'].forEach(m=>{
    const id=m==='domestic'?'mir-d':m==='ustc'?'mir-u':'mir-o';
    document.getElementById(id).className='sb'+(mm===m?' active':'');
  });

  // Sysinfo
  const mem=s.mem,disk=s.disk,swap=s.swap;
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
  if(s.gpu){
    const g=s.gpu.split(',').map(x=>x.trim());
    document.getElementById('gpu-row').innerHTML=`<div class="gpu"><div class="gn">🎮 ${g[0]}</div><div class="gv"><div class="k">温度</div><div class="v">${g[1]}°C</div></div><div class="gv"><div class="k">风扇</div><div class="v">${g[2]}</div></div><div class="gv"><div class="k">显存</div><div class="v">${g[3]} / ${g[4]}</div></div></div>`;
  }
}

/* ── Per-interface intelligence ── */
async function testIntel(key){
  const el=document.getElementById(key==='wired'?'wired-intel':'wifi-intel');
  el.className='intel-strip';
  el.innerHTML=`<span class="spin">⟳</span><span style="font-size:.7rem;color:var(--yellow);margin-left:6px">正在查询出口 IP 与延迟，约 15 秒…</span>`;
  try{
    const d=await fetch('/api/intel/'+key).then(r=>r.json());
    if(!d.ok){
      el.className='intel-strip err';
      el.innerHTML=`<span style="color:var(--red);font-size:.7rem">❌ ${d.msg}</span><button class="tbtn" onclick="testIntel('${key}')">重试</button>`;
      return;
    }
    const g=d.geo||{};
    const lDom=d.latency?.domestic_ms,lGl=d.latency?.international_ms;
    el.className='intel-strip ok';
    el.innerHTML=`
      <span class="i-flag">${d.flag||'🌍'}</span>
      <div><div class="i-place">${g.country||'未知'} · ${g.city||''} ${g.regionName||''}</div><div class="i-isp">${g.org||g.isp||'—'}</div></div>
      <div><div class="k" style="font-size:.6rem;color:var(--t3)">出口 IP</div><div class="i-ip">${g.query||'—'}</div></div>
      <div class="lat-wrap">
        <div class="lat"><div class="k">🇨🇳 国内</div><div class="v ${lDom!=null?(lDom<60?'v-g':lDom<150?'v-m':'v-b'):'v-u'}">${lDom!=null?lDom+' ms':'超时'}</div></div>
        <div class="lat"><div class="k">🌐 国际</div><div class="v ${lGl!=null?(lGl<100?'v-g':lGl<300?'v-m':'v-b'):'v-u'}">${lGl!=null?lGl+' ms':'超时'}</div></div>
      </div>
      <button class="tbtn" onclick="testIntel('${key}')">🔄 重测</button>
    `;
  }catch(e){
    el.className='intel-strip err';
    el.innerHTML=`<span style="color:var(--red);font-size:.7rem">❌ 检测失败: ${e}</span>`;
  }
}

/* ── Network actions ── */
async function netAct(i,a){toast(`${a==='up'?'开启':'关闭'} ${i==='wired'?'有线':'无线'}…`);const r=await fetch('/api/network/'+i+'/'+a,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1500);}
async function wifiDisc(){toast('断开无线…');const r=await fetch('/api/wifi/disconnect',{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1200);}
async function setRoute(i){toast('切换默认出口…');const r=await fetch('/api/network/route/'+i,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok);setTimeout(doRefresh,1200);}
async function setDNS(m){toast('切换 DNS…');const r=await fetch('/api/dns/'+m,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,5000);setTimeout(doRefresh,600);}
async function setMirror(m){toast('切换镜像源并 apt update，约 10~20s…',true,20000);const r=await fetch('/api/mirror/'+m,{method:'POST'}).then(r=>r.json());toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,7000);setTimeout(doRefresh,600);}

/* ── WiFi Modal ── */
let _nets=[];
function openWifi(){document.getElementById('wifi-ov').classList.add('open');}
function closeWifi(){document.getElementById('wifi-ov').classList.remove('open');doRefresh();}

async function doScan(){
  document.getElementById('scan-wrap').innerHTML='<div style="padding:18px;text-align:center;color:var(--t2)"><span class="spin">⟳</span> 扫描中…</div>';
  document.getElementById('test-btn').disabled=true;
  try{
    const d=await fetch('/api/wifi/scan').then(r=>r.json());
    _nets=d.networks||[];
    if(!_nets.length){document.getElementById('scan-wrap').innerHTML='<div style="padding:18px;text-align:center;color:var(--t3)">未发现 Wi-Fi 网络</div>';return;}
    renderScan(_nets);
    document.getElementById('test-btn').disabled=false;
  }catch(e){document.getElementById('scan-wrap').innerHTML=`<div style="padding:18px;text-align:center;color:var(--red)">扫描失败: ${e}</div>`;}
}

function renderScan(nets){
  document.getElementById('scan-wrap').innerHTML=`
    <table class="stbl">
      <thead><tr><th>网络名称 (SSID)</th><th>信号</th><th>加密</th><th>输入密码（可选）</th></tr></thead>
      <tbody>${nets.map((n,i)=>`
        <tr>
          <td><b>${n.ssid}</b>${n.in_use?' <span class="gw-b">当前连接</span>':''}<div style="font-size:.63rem;color:var(--t3)">信道 ${n.channel}</div></td>
          <td>
            <div style="display:flex;align-items:center;gap:5px">
              <div class="sbar" id="sb-${i}">${[4,7,10,13,16].map((h,j)=>`<s style="height:${h}px"${j<Math.round(n.signal/20)?' class="lit"':''}></s>`).join('')}</div>
              <span style="font-size:.68rem;color:var(--t2)">${n.signal}%</span>
            </div>
          </td>
          <td style="font-size:.68rem;color:var(--t2)">${n.security}</td>
          <td>
            <div class="pw-wrap2">
              <input class="pw-inp" type="password" id="pw-${i}" placeholder="${n.security==='Open'?'开放网络':'密码'}">
              <button class="pw-eye2" onclick="this.previousElementSibling.type=this.previousElementSibling.type==='password'?'text':'password'">👁</button>
            </div>
          </td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

/* ── Bulk test (background + polling) ── */
let _taskId=null,_pollTimer=null;

async function doTestAll(){
  const entries=_nets.map((n,i)=>({ssid:n.ssid,password:(document.getElementById('pw-'+i)?.value||'').trim()}));
  const btn=document.getElementById('test-btn');
  btn.disabled=true;btn.textContent='检测中…';
  document.getElementById('prog-wrap').style.display='block';
  document.getElementById('results-section').style.display='none';
  document.getElementById('bulk-st').textContent='正在后台逐一测试，请稍候…';

  try{
    const d=await fetch('/api/wifi/bulk-start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})}).then(r=>r.json());
    if(!d.ok){toast('❌ 启动失败: '+d.msg,false);btn.disabled=false;btn.textContent='⚡ 重新检测';return;}
    _taskId=d.task_id;
    _pollTimer=setInterval(pollBulk,2500);
  }catch(e){toast('❌ '+e,false);btn.disabled=false;btn.textContent='⚡ 全部检测';}
}

async function pollBulk(){
  if(!_taskId)return;
  try{
    const d=await fetch('/api/wifi/bulk-status/'+_taskId).then(r=>r.json());
    // Update progress
    const pct=d.total>0?Math.round(d.progress/d.total*100):0;
    document.getElementById('prog-cur').textContent=d.current||'—';
    document.getElementById('prog-txt').textContent=`${d.progress} / ${d.total}`;
    document.getElementById('prog-bar').style.width=pct+'%';
    // Show partial results
    if(d.results?.length) renderResults(d.results);
    if(d.done){
      clearInterval(_pollTimer);_pollTimer=null;
      document.getElementById('prog-wrap').style.display='none';
      document.getElementById('bulk-st').textContent=`检测完成 · ${d.results.filter(r=>r.status==='ok').length}/${d.results.length} 个网络可用`;
      document.getElementById('test-btn').disabled=false;
      document.getElementById('test-btn').textContent='⚡ 重新检测';
    }
  }catch(e){console.warn('Poll error',e);}
}

function renderResults(results){
  document.getElementById('results-section').style.display='block';
  document.getElementById('res-count').textContent=`${results.length} 个`;
  document.getElementById('res-body').innerHTML=results.map(r=>{
    if(r.status==='testing') return `<tr><td><b>${r.ssid}</b></td><td colspan="7" style="color:var(--t3)"><span class="spin">⟳</span> 检测中…</td></tr>`;
    if(r.status==='failed') return `<tr><td><b>${r.ssid}</b></td><td colspan="6" style="color:var(--t3)">— ${r.error||'失败'}</td><td><span class="pill p-dn">失败</span></td></tr>`;
    const g=r.geo||{};
    return `<tr class="rrow" onclick="switchNet('${r.ssid.replace(/'/g,"\\'")}')">
      <td><b>${r.ssid}</b></td>
      <td style="font-family:monospace;font-size:.7rem;color:var(--cyan)">${g.query||'—'}</td>
      <td>${r.flag||'🌍'} ${g.country||'—'}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.7rem;color:var(--t2)">${g.org||g.isp||'—'}</td>
      <td style="font-size:.7rem;color:var(--t2)">${g.city||'—'}</td>
      <td>${latB(r.latency?.domestic_ms)}</td>
      <td>${latB(r.latency?.international_ms)}</td>
      <td><span class="pill p-up">✓ 正常</span></td>
    </tr>`;
  }).join('');
}

async function switchNet(ssid){
  const idx=_nets.findIndex(n=>n.ssid===ssid);
  const pwd=idx>=0?(document.getElementById('pw-'+idx)?.value||''):'';
  toast(`切换至 ${ssid}…`,true,15000);
  document.querySelectorAll('.rrow').forEach(r=>r.classList.toggle('sel',r.querySelector('b')?.textContent===ssid));
  const r=await fetch('/api/wifi/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password:pwd})}).then(r=>r.json());
  toast(r.ok?'✅ '+r.msg:'❌ '+r.msg,r.ok,6000);
  if(r.ok)setTimeout(doRefresh,1500);
}

/* ── Auto-refresh ── */
let cdSec=15;
function tick(){if(--cdSec<=0){cdSec=15;doRefresh();}document.getElementById('cd').textContent=cdSec+'s';}
doRefresh();setInterval(tick,1000);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.getElementById('wifi-ov').classList.contains('open'))closeWifi();});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
