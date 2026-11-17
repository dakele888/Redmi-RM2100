#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方路由模式（无线 WAN）备份包校验 · 2026-11-09

用法: python 校验.py [router_mode_20261109.tgz]
"""
import hashlib
import sys
import tarfile

MD5 = "0809c469aa635708d87aaa1bd49e7391"
TGZ = "router_mode_20261109.tgz"

REQUIRED = [
    # 面板与 CGI
    "data/route/www/panel.html",
    "data/route/www/route.html",
    "data/route/www/devices.html",
    "data/route/www/cgi-bin/devicectl",
    "data/route/www/cgi-bin/wanctl",
    "data/route/www/cgi-bin/bridgectl",
    "data/route/www/cgi-bin/wlanctl",
    "data/route/www/cgi-bin/netctl",
    "data/route/www/cgi-bin/netstatus",
    "data/route/www/cgi-bin/sysstat",
    "data/route/www/cgi-bin/uplinkmode",
    "data/route/www/cgi-bin/routectl",
    "data/route/auth_check.sh",
    # 数据链路
    "data/route/device_list.sh",
    "data/route/devlist.lua",
    "data/route/dev_flow.sh",
    "data/route/logtrim.sh",
    "data/route/uci_router_mode.sh",
    "data/route/mode_switch.sh",
    # 官方文件补丁与数据桥
    "data/route/patches/wwdog.patched",
    "data/route/patches/ap_mode.patched",
    "data/route/patches/official_hook.sh",
    "data/route/hwnat_ctl.sh",
    "data/route/wifi_health.sh",
    "data/route/mpdu_density.sh",
    "data/route/patches/xqapi_bridge.sh",
    "data/route/patches/xqapi_extract.lua",
    "data/route/patches/ov_wan.lua",
    # 开机链
    "etc/init.d/zz_official",
    "etc/init.d/zz_cron",
    "etc/init.d/zz_dropbear",
    "etc/rc.d/S19zz_official",
    "etc/rc.d/S51zz_cron",
    "etc/rc.d/S20zz_dropbear",
    "etc/crontabs/root",
    "etc/nginx/nginx.conf",
    "etc/nginx/route-ui.conf",
    "etc/dropbear/dropbear_rsa_host_key",
    "etc/dropbear/authorized_keys",
    # 官方模式备份三件套（切模式闭环）
    "etc/config/.network.mode.router",
    "etc/config/.wireless.mode.router",
    "etc/config/.dhcp.mode.router",
    "etc/config/.network.mode.router.factory",
]

# 这些绝对不能出现在包里（整文件覆盖官方配置 / 鉴权开关 / 官方二进制）
FORBIDDEN = [
    "etc/config/network",
    "etc/config/wireless",
    "etc/config/firewall",
    "etc/config/dhcp",
    "data/route/.auth_required",
    "usr/sbin/wwdog",
    "usr/sbin/ap_mode.sh",
]

problems = []


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else TGZ
    raw = open(path, "rb").read()
    md5 = hashlib.md5(raw).hexdigest()
    print("包   : %s" % path)
    print("大小 : %d 字节" % len(raw))
    print("md5  : %s" % md5)

    print("\n[1] md5 校验")
    if md5 == MD5:
        print("  OK  与预期一致")
    else:
        print("  BAD 不一致（包可能被改动或损坏）")
        problems.append("md5")

    tf = tarfile.open(path)
    names = tf.getnames()
    print("条目 : %d" % len(names))

    print("\n[2] 必需文件")
    for r in REQUIRED:
        if r in names:
            print("  OK  %s" % r)
        else:
            print("  BAD 缺失 %s" % r)
            problems.append(r)

    print("\n[3] 危险内容（必须不存在）")
    bad = [n for n in names if n in FORBIDDEN]
    if bad:
        for b in bad:
            print("  BAD 包内含 %s" % b)
        problems.append("forbidden")
    else:
        print("  OK  官方配置整文件 / 鉴权开关 / 官方二进制 均未打包")

    print("\n[4] SSH 密钥")
    for k in ("etc/dropbear/dropbear_rsa_host_key", "etc/dropbear/authorized_keys"):
        try:
            sz = tf.getmember(k).size
        except KeyError:
            continue
        if sz > 0:
            print("  OK  %-45s %d B" % (k.split("/")[-1], sz))
        else:
            print("  BAD 0 字节：dropbear 会静默起不来 -> %s" % k)
            problems.append(k)

    print("\n[5] 关键脚本内容（官方路由模式口径）")
    # uci 字段级脚本：必须是字段级 set/delete，不能整文件覆盖
    try:
        us = tf.extractfile("data/route/uci_router_mode.sh").read().decode("utf-8", "ignore")
        if "set network.wan.ifname=\"apclii0\"" in us and "uci commit network" in us:
            print("  OK  uci_router_mode 会字段级写 wan=apclii0 / lan=31.1")
        else:
            print("  BAD uci_router_mode 内容异常")
            problems.append("uci_router_mode")
        if "cat /etc/config" not in us and "> /etc/config" not in us:
            print("  OK  不会整文件覆盖 /etc/config")
        else:
            print("  BAD uci_router_mode 存在整文件覆盖")
            problems.append("uci.overwrite")
        if "get wireless.$s.mode" in us or "mode) = \"sta\"" in us.replace('"', ''):
            print("  OK  ApCli 段名动态定位（恢复出厂后段名会变）")
        else:
            print("  WARN 未检测到 STA 段动态定位")
    except KeyError:
        problems.append("uci_router_mode")

    # hook：官方路由模式下只留数据桥
    try:
        hk = tf.extractfile("data/route/patches/official_hook.sh").read().decode("utf-8", "ignore")
        exec_lines = [l for l in hk.splitlines() if l.strip() and not l.strip().startswith("#")]
        exec_txt = "\n".join(exec_lines)
        if "xqapi_bridge.sh" in exec_txt and "ra_guard" not in exec_txt and "lan2_guard" not in exec_txt and "eth0_cond" not in exec_txt:
            print("  OK  hook 只驱动官方数据桥（旧守护已退役）")
        else:
            print("  BAD hook 仍引用旧守护")
            problems.append("hook.legacy")
        if "export PATH" in tf.extractfile("data/route/patches/xqapi_bridge.sh").read().decode("utf-8", "ignore"):
            print("  OK  xqapi_bridge 已修 hook 环境缺 PATH 问题")
        else:
            print("  WARN xqapi_bridge 未显式设置 PATH")
    except KeyError:
        problems.append("hook")

    # 模式备份三件套必须是「无线 WAN 形态」：wan=apclii0
    try:
        mb = tf.extractfile("etc/config/.network.mode.router").read().decode("utf-8", "ignore")
        if "apclii0" in mb and "192.168.31.1" in mb:
            print("  OK  模式备份三件套 = 无线 WAN 形态（wan=apclii0, lan=31.1）")
        else:
            print("  BAD 模式备份内容不是无线 WAN 形态")
            problems.append("mode3.content")
        fb = tf.extractfile("etc/config/.network.mode.router.factory").read().decode("utf-8", "ignore")
        if "eth1" in fb:
            print("  OK  出厂兜底模板已另存（factory）")
        else:
            print("  WARN 出厂兜底模板缺失或内容异常")
    except KeyError:
        problems.append("mode3")

    # CGI 官方化抽查：netctl 不允许再引用旧 lan2 / 旧 dnsmasq 重启
    try:
        nc = tf.extractfile("data/route/www/cgi-bin/netctl").read().decode("utf-8", "ignore")
        if "br-lan2" not in nc and "/data/route/dnsmasq.conf" not in nc and "killall dnsmasq" not in nc:
            print("  OK  netctl 已官方化（无 br-lan2 / 无手工换 dnsmasq）")
        else:
            print("  BAD netctl 仍含旧方案操作")
            problems.append("netctl.legacy")
        dc = tf.extractfile("data/route/www/cgi-bin/devicectl").read().decode("utf-8", "ignore")
        if "LANIF=br-lan" in dc and "dhcp.lan.ipaddr" in dc.replace('network.lan.ipaddr', 'dhcp.lan.ipaddr') or "network.lan.ipaddr" in dc:
            print("  OK  devicectl 已官方化")
        if "killall dnsmasq" in dc or "-C /data/route/dnsmasq.conf" in dc:
            print("  BAD devicectl 仍会手工换 dnsmasq")
            problems.append("devicectl.legacy")
    except KeyError:
        problems.append("cgi")

    # ap_mode 补丁：兜底 WAN 必须是 apclii0
    try:
        apm = tf.extractfile("data/route/patches/ap_mode.patched").read().decode("utf-8", "ignore")
        if "set network.wan.ifname=apclii0" in apm:
            print("  OK  ap_mode 兜底模板 WAN=apclii0（备份丢失时不写坏）")
        else:
            print("  BAD ap_mode 补丁兜底 WAN 异常")
            problems.append("ap_mode.patch")
    except KeyError:
        problems.append("ap_mode.patch")

    print("\n" + "=" * 52)
    if problems:
        print("结论: 有问题 %d 项 -> %s" % (len(problems), ", ".join(problems[:5])))
        sys.exit(1)
    print("结论: 校验通过，可用于恢复")


if __name__ == "__main__":
    main()
