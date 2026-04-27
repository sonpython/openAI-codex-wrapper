# Access Gate Options

codex-wrapper is **INTERNAL ONLY** (v1 locked 2026-04-27). The wrapper must be
unreachable from the public Internet. Choose exactly one of the three options below
and document the choice in `.env.production`.

---

## Option A — Caddy IP Allowlist (default, no SaaS dependency)

**Best for:** air-gapped or WireGuard-VPN networks, no third-party accounts required.

Traffic flow: `client (VPN/LAN) → Caddy @allowed matcher → gateway:8000`

The production Caddyfile (`infra/Caddyfile.production`) ships this option by default:

```caddyfile
@allowed {
    remote_ip 10.0.0.0/8 192.168.0.0/16 100.64.0.0/10
}
handle @allowed {
    reverse_proxy gateway:8000 { ... }
}
handle {
    respond 403
}
```

Private CIDR ranges covered:
- `10.0.0.0/8` — RFC-1918 class A (corporate LANs, cloud VPCs)
- `192.168.0.0/16` — RFC-1918 class C (home/office routers)
- `100.64.0.0/10` — RFC-6598 shared address space (Tailscale MagicDNS range)

**Add WireGuard** to restrict even further:

```bash
# Install WireGuard on host (Ubuntu)
apt-get install -y wireguard
wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key
# Configure wg0.conf with peer public keys
# Then restrict Caddy to the WireGuard tunnel subnet only, e.g. 10.8.0.0/24
```

---

## Option B — Tailscale (recommended for small dev teams)

**Best for:** distributed team, mobile/remote access, no public firewall needed.

1. Install Tailscale on host: `curl -fsSL https://tailscale.com/install.sh | sh`
2. `tailscale up --advertise-tags=tag:codex-api`
3. Set ACL policy to restrict `tag:codex-api` access to specific users/devices.
4. In Caddyfile, restrict to Tailscale subnet: `remote_ip 100.64.0.0/10 100.100.100.100/32`
5. Block all traffic on `:80` and `:443` from non-Tailscale IPs via UFW.

```bash
ufw allow in on tailscale0
ufw deny 80
ufw deny 443
```

---

## Option C — Cloudflare Access (recommended for orgs with Cloudflare)

**Best for:** teams already using Cloudflare, SSO/Google Auth required.

1. Create a Cloudflare Zero Trust application pointing to the internal domain.
2. Set Identity Provider to Google Workspace (or GitHub, Okta).
3. Add policy: allow `@yourcompany.com` emails only.
4. Proxy DNS through Cloudflare (orange cloud); do NOT enable public access.
5. Cloudflare Tunnel (`cloudflared`) connects host to Cloudflare edge—no inbound ports needed.

```bash
cloudflared tunnel login
cloudflared tunnel create codex-api
# edit ~/.cloudflared/config.yml
cloudflared tunnel run codex-api
```

---

## v1 Default: Option A (Caddy IP Allowlist)

`infra/Caddyfile.production` implements Option A. To switch, replace the
`@allowed` block and update `ACCESS_GATE_KIND` in `.env.production`.

| Env var | Values | Description |
|---|---|---|
| `ACCESS_GATE_KIND` | `caddy-ip` \| `tailscale` \| `cloudflare` | Documents chosen gate (informational) |

**Verification:** after deploy, run a port-scan from an external IP:

```bash
nmap -p 80,443,8000,5432,6379,9090 <VM_PUBLIC_IP>
# Expected: 80 open, 443 open, all others filtered/closed
# With WireGuard/Tailscale: 80+443 also closed externally
```
