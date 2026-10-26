#!/usr/bin/env python3
"""Render offline configuration files. Never connects to or configures a device."""
import ipaddress
import json
import pathlib
import re
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent
BLOCKED = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
           "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
           "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"]


def render(config, out):
    expected = {"admin_ipv4", "openwrt_ai_device", "rpi_uplink_if", "rpi_ai_if"}
    if set(config) != expected:
        raise ValueError(f"Exactly these keys are required: {sorted(expected)}")
    admin = str(ipaddress.IPv4Address(config["admin_ipv4"]))
    if ipaddress.IPv4Address(admin) not in ipaddress.IPv4Network("10.0.0.0/24") or admin in {
        "10.0.0.0", "10.0.0.254", "10.0.0.255"
    }:
        raise ValueError("admin_ipv4 must be a management PC in 10.0.0.1–253")
    for key in expected - {"admin_ipv4"}:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", config[key]):
            raise ValueError(f"Invalid Linux device/interface name: {key}")
    up, ai, dev = (config[k] for k in ("rpi_uplink_if", "rpi_ai_if", "openwrt_ai_device"))
    if up == ai:
        raise ValueError("RPi uplink and AI interfaces must be different")
    if dev in {"br-lan", "lo"} or {up, ai} & {"lo"}:
        raise ValueError("Do not use the existing LAN bridge or loopback")
    out.mkdir(parents=True, exist_ok=True)
    (out / "rpi").mkdir(exist_ok=True)
    (out / "openwrt").mkdir(exist_ok=True)

    # These are additive named UCI sections, NOT replacements for full configs.
    network = f"""config interface 'ai_transit'
        option device '{dev}'
        option proto 'static'
        option ipaddr '10.2.0.1'
        option netmask '255.255.255.252'
        option delegate '0'

config route 'ai_subnet'
        option interface 'ai_transit'
        option target '10.1.0.0/24'
        option gateway '10.2.0.2'
"""
    (out / "openwrt/network.fragment").write_text(network)
    rules = ["""config zone 'ai_zone'
        option name 'ai'
        list network 'ai_transit'
        option input 'REJECT'
        option output 'ACCEPT'
        option forward 'REJECT'
        option masq '0'
"""]

    def rule(name, values):
        lines = [f"config rule '{name}'"]
        for key, value in values.items():
            if isinstance(value, list):
                lines.extend(f"        list {key} '{v}'" for v in value)
            else:
                lines.append(f"        option {key} '{value}'")
        rules.append("\n".join(lines) + "\n")

    sources = ["10.1.0.0/24", "10.2.0.2/32"]
    rule("ai_dns", dict(name="AI DNS to designated resolver", src="ai", family="ipv4",
         src_ip=sources, dest_ip="10.0.0.254", proto="tcp udp", dest_port="53", target="ACCEPT"))
    rule("ai_transit_ping", dict(name="RPi ping transit gateway", src="ai", family="ipv4",
         src_ip="10.2.0.2", dest_ip="10.2.0.1", proto="icmp", icmp_type="echo-request", target="ACCEPT"))
    rule("ai_admin_ssh", dict(name="Management PC SSH to RPi and Spark", src="lan", dest="ai",
         family="ipv4", src_ip=admin, dest_ip=["10.2.0.2", "10.1.0.10"],
         proto="tcp", dest_port="22", target="ACCEPT"))
    rule("ai_admin_ping", dict(name="Management PC ping RPi and Spark", src="lan", dest="ai",
         family="ipv4", src_ip=admin, dest_ip=["10.2.0.2", "10.1.0.10"],
         proto="icmp", icmp_type="echo-request", target="ACCEPT"))
    # Must precede the web/NTP WAN permissions (e.g. private WAN or VPN routes).
    rule("ai_block_special", dict(name="AI reject non-public forwarded destinations", src="ai", dest="*",
         family="ipv4", dest_ip=BLOCKED, proto="all", target="REJECT"))
    rule("ai_web", dict(name="AI public HTTP HTTPS", src="ai", dest="wan", family="ipv4",
         src_ip=sources, proto="tcp", dest_port="80 443", target="ACCEPT"))
    rule("ai_ntp", dict(name="AI public NTP", src="ai", dest="wan", family="ipv4",
         src_ip=sources, proto="udp", dest_port="123", target="ACCEPT"))
    (out / "openwrt/firewall.fragment").write_text("\n".join(rules))
    (out / "openwrt/dhcp.fragment").write_text("""config dhcp 'ai_transit'
        option interface 'ai_transit'
        option ignore '1'
        option ra 'disabled'
        option dhcpv6 'disabled'
        option ndp 'disabled'
""")
    for name, iface, address, gateway in [
        ("taco-uplink", up, "10.2.0.2/30", "10.2.0.1"),
        ("taco-ai", ai, "10.1.0.1/24", None),
    ]:
        profile = f"""[connection]
id={name}
uuid={uuid.uuid5(uuid.NAMESPACE_DNS, 'dgx.local.' + name)}
type=ethernet
interface-name={iface}
autoconnect=true
autoconnect-priority=100

[ipv4]
method=manual
address1={address}{',' + gateway if gateway else ''}
never-default={'false' if gateway else 'true'}
ignore-auto-dns=true
"""
        if gateway:
            profile += "dns=10.0.0.254;\n"
        profile += "\n[ipv6]\nmethod=disabled\n"
        path = out / "rpi" / (name + ".nmconnection")
        path.write_text(profile)
        path.chmod(0o600)

    nft = f"""#!/usr/sbin/nft -f
# Dedicated router only: this replaces ALL existing nftables rules.
# Do not run Docker/UFW/firewalld on this machine alongside this ruleset.
flush ruleset

define uplink = "{up}"
define ai = "{ai}"
define admin = {admin}
define spark = 10.1.0.10

table inet taco_filter {{
    set non_public_v4 {{
        type ipv4_addr
        flags interval
        elements = {{ {', '.join(BLOCKED)} }}
    }}

    chain input {{
        type filter hook input priority 0; policy drop;
        iifname "lo" accept
        meta nfproto ipv6 counter drop
        iifname $ai ip saddr != 10.1.0.0/24 counter drop
        iifname $uplink ip saddr 10.1.0.0/24 counter drop
        ct state invalid counter drop
        ct state established,related accept
        iifname $uplink ip saddr $admin ip daddr 10.2.0.2 tcp dport 22 ct state new accept
        iifname $uplink ip saddr $admin ip daddr 10.2.0.2 icmp type echo-request accept
        iifname $ai ip daddr 10.1.0.1 icmp type echo-request accept
        # OPTIONAL NAS: uncomment only after configuring Samba authentication and shares.
        # iifname $ai ip saddr $spark ip daddr 10.1.0.1 tcp dport 445 accept
        limit rate 5/second burst 10 packets log prefix "taco-input-drop "
        counter drop
    }}

    chain forward {{
        type filter hook forward priority 0; policy drop;
        meta nfproto ipv6 counter drop
        iifname $ai ip saddr != 10.1.0.0/24 counter drop
        iifname $uplink ip saddr 10.1.0.0/24 counter drop
        ct state invalid counter drop
        ct state established,related accept
        iifname $uplink oifname $ai ip saddr $admin ip daddr $spark tcp dport 22 ct state new accept
        iifname $uplink oifname $ai ip saddr $admin ip daddr $spark icmp type echo-request accept
        # DNS is the only deliberate exception to the private-address block.
        iifname $ai oifname $uplink ip daddr 10.0.0.254 udp dport 53 accept
        iifname $ai oifname $uplink ip daddr 10.0.0.254 tcp dport 53 accept
        iifname $ai ip daddr @non_public_v4 counter drop
        iifname $ai oifname $uplink tcp dport {{ 80, 443 }} ct state new accept
        iifname $ai oifname $uplink udp dport 123 accept
        limit rate 5/second burst 10 packets log prefix "taco-forward-drop "
        counter drop
    }}

    chain output {{
        type filter hook output priority 0; policy drop;
        oifname "lo" accept
        meta nfproto ipv6 counter drop
        ct state invalid counter drop
        ct state established,related accept
        oifname $uplink ip daddr 10.0.0.254 udp dport 53 accept
        oifname $uplink ip daddr 10.0.0.254 tcp dport 53 accept
        oifname $uplink ip daddr 10.2.0.1 icmp type echo-request accept
        ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} accept
        ip daddr @non_public_v4 counter drop
        oifname $uplink tcp dport {{ 80, 443 }} ct state new accept
        oifname $uplink udp dport 123 accept
        limit rate 5/second burst 10 packets log prefix "taco-output-drop "
        counter drop
    }}
}}
"""
    (out / "rpi/nftables.conf").write_text(nft)
    (out / "rpi/90-taco-router.conf").write_text("""# /etc/sysctl.d/90-taco-router.conf
net.ipv4.ip_forward = 1
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.all.forwarding = 0
""")
    (out / "spark-settings.txt").write_text("""Static IPv4: 10.1.0.10/24
Gateway:     10.1.0.1
DNS:         10.0.0.254
IPv6:        disabled on this connection
Disable Wi-Fi / alternative upstream paths.
No DHCP server is included in this configuration.
""")
    (out / "resolved-config.json").write_text(json.dumps(config, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python3 render.py CONFIG.json OUTPUT_DIRECTORY")
    try:
        render(json.loads(pathlib.Path(sys.argv[1]).read_text()), pathlib.Path(sys.argv[2]))
    except (ValueError, OSError, TypeError) as error:
        sys.exit(str(error))
    print(f"Generated offline configuration in {sys.argv[2]}; nothing deployed.")
