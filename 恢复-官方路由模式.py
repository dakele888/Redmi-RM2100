#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redmi RM2100 一键恢复 —— 官方路由模式（无线 WAN）版 · 2026-11-09

形态：官方原生路由模式，wan=apclii0（无线上行）/ eth1（有线可选），
      内网 192.168.31.x（官方 DHCP/NAT/防火墙/IPv6 原生管理）。

恢复内容：
  /data/route/**            面板、CGI、脚本（不含已退役组件与运行时文件）
  /etc/init.d/zz_*          开机钩子（S19 双 bind mount / S20 SSH / S51 cron）
  /etc/rc.d/S19/S20/S51     启动顺序
  /etc/dropbear/**          SSH 主机密钥与授权公钥
  /etc/crontabs/root        定时任务（logtrim）
  /etc/nginx/route-ui.conf  面板路由
  /etc/nginx/nginx.conf     含 include 行
  /etc/config/.*.mode.router  官方模式备份三件套 + 出厂兜底（切模式闭环用）
  uci 字段级配置            uci_router_mode.sh（wan=apclii0 / lan=31.1 / DHCP /
                            删 NETMODE / ipv6.settings native），不整文件覆盖

绝不整文件覆盖 /etc/config/network、wireless、firewall。

用法:
    python 恢复-官方路由模式.py --ip 192.168.31.1        # 恢复后自动重启
    python 恢复-官方路由模式.py --ip 192.168.31.1 --no-reboot
恢复出厂后路由器地址一般是 192.168.31.1（官方默认），或上游 WiFi 给的地址。
"""
import argparse
import functools
import hashlib
import http.cookiejar
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
DEVICE_ID = "00:e0:1d:f2:60:df"
DEFAULT_PWD = "YOUR_ROUTER_PASSWORD"   # ← 用前改成你自己的路由器管理密码
PORT_NC = 9999
HTTP_PORTS = (8899, 8898, 8897, 8896)
TGZ = "router_mode_20261109.tgz"
MD5 = "a80882be2a30fdd02f00948332357485"

# 恢复清单：只允许这些前缀，其它一律不落地
ALLOW = (
    "data/route/",
    "etc/init.d/zz_",
    "etc/rc.d/S19zz_official",
    "etc/rc.d/S20zz_dropbear",
    "etc/rc.d/S51zz_cron",
    "etc/dropbear/",
    "etc/crontabs/root",
    "etc/nginx/route-ui.conf",
    "etc/nginx/nginx.conf",
    "etc/config/.network.mode.router",
    "etc/config/.wireless.mode.router",
    "etc/config/.dhcp.mode.router",
)
# 整文件覆盖仍然禁止（官方配置主体由官方负责，uci 只做字段级）
FORBID = (
    "etc/config/network",
    "etc/config/wireless",
    "etc/config/firewall",
    "etc/config/dhcp",
)

captured = []
_nonce_seq = 0


def sha1(s):
    return hashlib.sha1(s.encode()).hexdigest()


def local_ip_for(dest):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 80))
        return s.getsockname()[0]
    finally:
        s.close()


class _Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve_dir(d):
    for p in HTTP_PORTS:
        try:
            h = functools.partial(_Q, directory=d)
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", p), h)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return p, srv
        except OSError:
            continue
    raise RuntimeError("HTTP 端口全被占用")


class Listener(threading.Thread):
    def __init__(self, timeout):
        threading.Thread.__init__(self, daemon=True)
        self.timeout = timeout

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", PORT_NC))
            srv.listen(1)
        except Exception as e:
            captured.append(("[bind 失败] %s" % e).encode())
            srv.close()
            return
        srv.settimeout(self.timeout)
        try:
            c, _ = srv.accept()
            c.settimeout(10)
            while True:
                d = c.recv(65536)
                if not d:
                    break
                captured.append(d)
            c.close()
        except Exception:
            pass
        finally:
            srv.close()


def login(ip, pwd):
    global _nonce_seq
    last = None
    for _ in range(4):
        _nonce_seq += 1
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(cj))
        op.addheaders = [("User-Agent", "Mozilla/5.0"), ("Referer", "http://%s/" % ip)]
        rnd = int(time.time() * 1000) * 100 + (_nonce_seq % 100)
        nonce = "0_%s_%d_%d" % (DEVICE_ID, int(time.time()), rnd)
        body = urllib.parse.urlencode({
            "username": "admin",
            "password": sha1(nonce + sha1(pwd + KEY)),
            "logtype": "2",
            "nonce": nonce,
        }).encode()
        req = urllib.request.Request(
            "http://%s/cgi-bin/luci/api/xqsystem/login" % ip, data=body)
        with op.open(req, timeout=12) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        if j.get("code") == 0:
            return op, j["token"]
        last = j
        if j.get("code") != 1582:
            break
        time.sleep(1.2)
    raise RuntimeError("登录失败：%s（密码不对用 --pwd）" % last)


def inject(ip, pwd, pc_ip, script, timeout=120):
    del captured[:]
    op, stok = login(ip, pwd)
    lis = Listener(timeout + 10)
    lis.start()
    time.sleep(0.6)
    inner = "(%s) > /tmp/ro 2>&1; busybox nc %s %d < /tmp/ro" % (script, pc_ip, PORT_NC)
    body = urllib.parse.urlencode({
        "bssid": "00:11:22:33:44:55", "user_id": "0",
        "ssid": ";%s;" % inner, "_locale": "zh_cn",
    }).encode()
    req = urllib.request.Request(
        "http://%s/cgi-bin/luci/;stok=%s/api/misystem/set_config_iotdev" % (ip, stok), data=body)
    try:
        with op.open(req, timeout=15) as r:
            r.read()
    except Exception:
        pass
    lis.join(timeout + 12)
    return b"".join(captured).decode("utf-8", "ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.31.1")
    ap.add_argument("--pwd", default=DEFAULT_PWD)
    ap.add_argument("--no-reboot", action="store_true")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    tgz = os.path.join(here, TGZ)
    if not os.path.isfile(tgz):
        sys.exit("找不到 %s" % TGZ)
    md5 = hashlib.md5(open(tgz, "rb").read()).hexdigest()
    if md5 != MD5:
        sys.exit("包 md5 与预期不符：%s" % md5)

    pc_ip = local_ip_for(a.ip)
    print("=" * 56)
    print(" 路由器 : http://%s" % a.ip)
    print(" 本机   : %s" % pc_ip)
    print(" 包     : %s (%d 字节)" % (TGZ, os.path.getsize(tgz)))
    print("=" * 56)

    port, srv = serve_dir(here)
    try:
        login(a.ip, a.pwd)
    except Exception as e:
        srv.shutdown()
        sys.exit("[X] 连不上或密码不对：%s" % e)
    print("[0/5] API 登录成功，HTTP :%d" % port)

    print("[1/5] 路由器下载备份包 ...")
    out = inject(a.ip, a.pwd, pc_ip,
                 "rm -f /tmp/bs.tgz; wget -q -T 30 -O /tmp/bs.tgz 'http://%s:%d/%s'; "
                 "echo RC=$?; md5sum /tmp/bs.tgz" % (pc_ip, port, TGZ), 90)
    print(out.strip())
    if MD5 not in out:
        srv.shutdown()
        sys.exit("!! 路由器上 md5 不符，停止（未改动任何文件）")
    print("    md5 一致")

    print("[2/5] 校验包内路径（禁止整文件覆盖官方配置）...")
    chk = inject(a.ip, a.pwd, pc_ip,
                 "echo BAD=$(tar tzf /tmp/bs.tgz | grep -cE '(^| )etc/config/(network|wireless|firewall|dhcp)( |$)'); "
                 "echo MODE3=$(tar tzf /tmp/bs.tgz | grep -c 'mode.router')", 60)
    print(chk.strip())
    if "BAD=0" not in chk:
        srv.shutdown()
        sys.exit("!! 包里混入了整份官方配置文件，拒绝恢复")
    if "MODE3=" not in chk or "MODE3=0" in chk:
        print("    (提示) 模式备份三件套缺失，切模式闭环将不完整")

    print("[3/5] 解包（脚本 / 面板 / 开机项 / uci 字段级配置）...")
    script = (
        "tar czf /tmp/pre_safe.tgz /etc/nginx/nginx.conf /etc/crontabs/root "
        "/etc/config/network /etc/config/dhcp 2>/dev/null; "
        "cd / && tar xzf /tmp/bs.tgz; echo UNPACK=$?; "
        "rm -f /data/route/.auth_required; "
        "chown -R root:root /data/route /etc/dropbear; "
        "chmod 755 /data/route/*.sh /data/route/patches/* "
        "/data/route/www/cgi-bin/* /etc/init.d/zz_* 2>/dev/null; "
        "chmod 700 /etc/dropbear; chmod 600 /etc/dropbear/* 2>/dev/null; "
        "[ -s /etc/dropbear/dropbear_rsa_host_key ] || "
        "{ rm -f /etc/dropbear/dropbear_rsa_host_key; "
        "dropbearkey -t rsa -f /etc/dropbear/dropbear_rsa_host_key >/dev/null 2>&1; }; "
        "ln -sf ../init.d/zz_cron /etc/rc.d/S51zz_cron; "
        "ln -sf ../init.d/zz_dropbear /etc/rc.d/S20zz_dropbear; "
        "ln -sf ../init.d/zz_official /etc/rc.d/S19zz_official; "
        "rm -f /etc/rc.d/S96zz_lan2; "
        "grep -q 'route-ui.conf' /etc/nginx/nginx.conf || "
        "sed -i \"/include 'miwifi-webinitrd.conf';/a\\        include 'route-ui.conf';\" "
        "/etc/nginx/nginx.conf; "
        "sh /data/route/uci_router_mode.sh; echo UCI_DONE=$?; "
        "nginx -t >/dev/null 2>&1 && /etc/init.d/nginx restart >/dev/null 2>&1; "
        "sync; echo ROUTE=$(ls /data/route/*.sh | wc -l) "
        "CGI=$(ls /data/route/www/cgi-bin/ | wc -l) "
        "RC=$(ls /etc/rc.d | grep -c zz_) "
        "WWDOG=$(grep -c 'official_hook' /usr/sbin/wwdog 2>/dev/null || echo 0) "
        "NETWAN=$(uci -q get network.wan.ifname)"
    )
    out = inject(a.ip, a.pwd, pc_ip, script, 240)
    print(out.strip())
    if "UNPACK=0" not in out or "UCI_DONE=0" not in out:
        srv.shutdown()
        sys.exit("!! 解包或 uci 配置失败；/tmp/pre_safe.tgz 可回滚 network/dhcp/nginx/crontab")
    srv.shutdown()

    if a.no_reboot:
        print("[4/5] 跳过重启。手动： reboot")
        print("       （重启后 uci 形态与开机钩子才全部生效）")
        return

    print("[4/5] 重启 ...")
    try:
        inject(a.ip, a.pwd, pc_ip, "sync; sleep 1; reboot", 10)
    except Exception:
        pass
    print("")
    print("== 等 1~2 分钟 ==")
    print(" 面板 : http://192.168.31.1/admin   （内网固定 31.1）")
    print(" 上行 : 无线 WAN（apclii0），上游 SSID 用面板「WiFi 切换」配")
    print(" IPv6 : 官方机制已开（native），上游下发前缀后内网自动获取")
    print(" SSH  : dropbear 仅密钥登录 root@192.168.31.1")
    print(" 说明 : /etc/config 官方配置主体未被整文件改动，开机由官方负责")


if __name__ == "__main__":
    main()
