"""nftables configuration generated directly from RabbitWall installation settings."""
import ipaddress
from settings import validate

LEASE = 20
PROBE_COMMENT = 'rabbitwall-health-probe'
DNS_COMMENT = 'rabbitwall-local-dns'
BLOCKED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
           '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16',
           '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')

SYSCTL = '''net.ipv4.ip_forward = 1
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
net.netfilter.nf_conntrack_acct = 1
'''


def dns_rule(config):
    c = validate(config); ai = ipaddress.IPv4Interface(c['ai_cidr'])
    return (f'insert rule inet rabbitwall_filter input iifname "{c["ai_if"]}" '
            f'ip saddr {ai.network} ip daddr {ai.ip} meta l4proto {{ tcp, udp }} th dport 53 '
            f'counter accept comment "{DNS_COMMENT}"\n')


def probe_rules(config):
    c = validate(config)
    return ''.join(f'insert rule inet rabbitwall_filter output oifname "{node["interface"]}" '
                   f'ip saddr {ipaddress.IPv4Interface(c["ai_cidr"] if node["interface"]==c["ai_if"] else c["uplink_cidr"]).ip} '
                   f'ip daddr {node["ip"]} icmp type echo-request counter accept comment "{PROBE_COMMENT}"\n'
                   for node in c['nodes'])


def gate(config):
    c = validate(config); ai = ipaddress.IPv4Interface(c['ai_cidr'])
    return f'''table inet rabbitwall_console {{
    set online_sources {{
        type ipv4_addr
        flags interval,timeout
        timeout {LEASE}s
    }}
    chain ai_egress {{
        ct original ip saddr @online_sources counter return
        limit rate 5/second burst 10 packets log prefix "rabbitwall-forward-drop gate "
        counter drop
    }}
    chain dns_request {{
        ip saddr @online_sources counter return
        counter drop
    }}
    chain dns_reply {{
        ip daddr @online_sources counter return
        counter drop
    }}
    chain input {{
        type filter hook input priority -10; policy accept;
        iifname "{c['ai_if']}" ip daddr {ai.ip} meta l4proto {{ tcp, udp }} th dport 53 jump dns_request
    }}
    chain output {{
        type filter hook output priority -10; policy accept;
        ip saddr {ai.ip} ip daddr {c['dns_upstream']} meta l4proto {{ tcp, udp }} th dport 53 jump dns_request
        oifname "{c['ai_if']}" ip saddr {ai.ip} meta l4proto {{ tcp, udp }} th sport 53 jump dns_reply
    }}
    chain forward {{
        type filter hook forward priority -10; policy accept;
        iifname "{c['ai_if']}" meta l4proto {{ tcp, udp }} th dport 53 counter drop
        iifname "{c['ai_if']}" oifname "{c['uplink_if']}" meta nfproto ipv6 counter drop
        iifname "{c['uplink_if']}" oifname "{c['ai_if']}" meta nfproto ipv6 counter drop
        iifname "{c['ai_if']}" oifname "{c['uplink_if']}" ip saddr {ai.network} ct direction original jump ai_egress
        iifname "{c['uplink_if']}" oifname "{c['ai_if']}" ct original ip saddr {ai.network} jump ai_egress
    }}
}}
'''


def baseline(config):
    c = validate(config); ai = ipaddress.IPv4Interface(c['ai_cidr']); up = ipaddress.IPv4Interface(c['uplink_cidr'])
    admin = ''.join(f'        iifname $uplink oifname $ai ip saddr $admin ip daddr {node["ip"]} tcp dport 22 ct state new accept\n'
                    f'        iifname $uplink oifname $ai ip saddr $admin ip daddr {node["ip"]} icmp type echo-request accept\n'
                    for node in c['nodes'] if node['interface']==c['ai_if'])
    result = f'''#!/usr/sbin/nft -f
# RabbitWall owns this machine's nftables ruleset.
flush ruleset

define uplink = "{c['uplink_if']}"
define ai = "{c['ai_if']}"
define admin = {c['admin_ip']}

table inet rabbitwall_filter {{
    set non_public_v4 {{
        type ipv4_addr
        flags interval
        elements = {{ {', '.join(BLOCKED)} }}
    }}
    chain input {{
        type filter hook input priority 0; policy drop;
        iifname "lo" accept
        meta nfproto ipv6 counter drop
        iifname $ai ip saddr != {ai.network} counter drop
        iifname $uplink ip saddr {ai.network} counter drop
        ct state invalid counter drop
        ct state established,related accept
        iifname $uplink ip saddr $admin ip daddr {up.ip} tcp dport 22 ct state new accept
        iifname $uplink ip saddr $admin ip daddr {up.ip} icmp type echo-request accept
        iifname $ai ip daddr {ai.ip} icmp type echo-request accept
        limit rate 5/second burst 10 packets log prefix "rabbitwall-input-drop "
        counter drop
    }}
    chain forward {{
        type filter hook forward priority 0; policy drop;
        meta nfproto ipv6 counter drop
        iifname $ai ip saddr != {ai.network} counter drop
        iifname $uplink ip saddr {ai.network} counter drop
        ct state invalid counter drop
        ct state established,related accept
{admin}        iifname $ai ip daddr @non_public_v4 counter drop
        iifname $ai oifname $uplink tcp dport {{ 80, 443 }} ct state new accept
        iifname $ai oifname $uplink udp dport 123 accept
        limit rate 5/second burst 10 packets log prefix "rabbitwall-forward-drop "
        counter drop
    }}
    chain output {{
        type filter hook output priority 0; policy drop;
        oifname "lo" accept
        meta nfproto ipv6 counter drop
        ct state invalid counter drop
        ct state established,related accept
        oifname $uplink ip daddr {c['dns_upstream']} meta l4proto {{ tcp, udp }} th dport 53 accept
        oifname $uplink ip daddr {c['gateway']} icmp type echo-request accept
        ip protocol icmp icmp type {{ destination-unreachable, time-exceeded, parameter-problem }} accept
        ip daddr @non_public_v4 counter drop
        oifname $uplink tcp dport {{ 80, 443 }} ct state new accept
        oifname $uplink udp dport 123 accept
        limit rate 5/second burst 10 packets log prefix "rabbitwall-output-drop "
        counter drop
    }}
}}

include "/etc/nftables.d/rabbitwall.nft"
'''
    if c['nat']:
        result += f'''
table ip rabbitwall_nat {{
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        oifname "{c['uplink_if']}" ip saddr {ai.network} masquerade
    }}
}}
'''
    return result
