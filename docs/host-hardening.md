# Host Hardening Checklist — codex-wrapper

**Target:** Ubuntu 24.04 LTS production VM.
Run each section once during initial provisioning. Re-verify after major OS updates.

---

## 1. UFW Firewall

```bash
# Install UFW if not present
apt-get install -y ufw

# Default: deny all inbound, allow all outbound
ufw default deny incoming
ufw default allow outgoing

# Allow SSH (restrict to known admin IPs in production)
ufw allow 22/tcp comment "SSH admin"

# Allow Caddy (HTTP for ACME + HTTPS for API)
ufw allow 80/tcp  comment "Caddy HTTP / ACME"
ufw allow 443/tcp comment "Caddy HTTPS"

# Enable (non-interactive)
ufw --force enable
ufw status verbose
```

**Verification:** `nmap -p 1-65535 <VM_PUBLIC_IP>` from an external host should show only 22, 80, 443 open. All Docker-internal ports (5432, 6379, 8000, 9090, 3000) must not appear.

> Note: Docker's `iptables` rules can bypass UFW. Set `DOCKER_OPTS="--iptables=false"` in `/etc/docker/daemon.json` and manage rules manually **only** if you need strict per-port external control. For this deployment, all Docker ports are already bound to `127.0.0.1` or left unbound in `docker-compose.production.yml` (Caddy is the only public listener).

---

## 2. Docker User Namespace Remapping

Remaps container UIDs to unprivileged host UIDs, preventing container-escape privilege escalation.

```bash
# Add dockremap user/group
groupadd --system --gid 165536 dockremap 2>/dev/null || true
useradd  --system --uid 165536 --gid dockremap \
         --no-create-home --shell /sbin/nologin dockremap 2>/dev/null || true

# Configure UID/GID mapping ranges
echo "dockremap:165536:65536" >> /etc/subuid
echo "dockremap:165536:65536" >> /etc/subgid

# Enable userns-remap in Docker daemon config
cat > /etc/docker/daemon.json <<'EOF'
{
  "userns-remap": "dockremap",
  "log-driver": "local",
  "log-opts": {
    "max-size": "100m",
    "max-file": "10"
  }
}
EOF

systemctl restart docker
docker info | grep -i "userns"   # expect: Security Options: userns
```

**Note:** After enabling userns-remap, existing volumes may need ownership adjustment (`chown -R 165536:165536 /var/lib/docker/volumes/<vol>/_data`).

---

## 3. SSH Hardening (Key-Only)

```bash
# Disable password authentication
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/'  /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/'     /etc/ssh/sshd_config

# Disable unused auth methods
echo "ChallengeResponseAuthentication no" >> /etc/ssh/sshd_config
echo "UsePAM yes"                          >> /etc/ssh/sshd_config
echo "X11Forwarding no"                    >> /etc/ssh/sshd_config
echo "AllowTcpForwarding yes"              >> /etc/ssh/sshd_config  # needed for SSH tunnels to Grafana

# Verify config before restart
sshd -t && systemctl reload sshd
```

**Verify:** `ssh -o PasswordAuthentication=yes root@<VM>` should be rejected.

Ensure at least one authorized key is in `~/.ssh/authorized_keys` before disabling passwords.

---

## 4. Automatic Security Updates

```bash
apt-get install -y unattended-upgrades update-notifier-common

# Enable automatic security updates
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# Configure which repos to auto-update (security only)
cat > /etc/apt/apt.conf.d/50unattended-upgrades <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";   // set true only if SLO allows
Unattended-Upgrade::Mail "ops@example.com";
EOF

systemctl enable --now unattended-upgrades
```

---

## 5. fail2ban for SSH

```bash
apt-get install -y fail2ban

cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled  = true
port     = ssh
filter   = sshd
logpath  = %(sshd_log)s
backend  = %(sshd_backend)s
EOF

systemctl enable --now fail2ban
fail2ban-client status sshd
```

---

## 6. AppArmor

Ubuntu 24.04 ships AppArmor enabled by default. Verify and enforce:

```bash
systemctl status apparmor
aa-status | grep -c enforce    # should show enforced profiles
```

Docker containers run under the `docker-default` AppArmor profile automatically.
For stricter profiles, add `security_opt: [apparmor:docker-default]` to compose services (already Docker default behavior).

**SELinux note:** Ubuntu uses AppArmor, not SELinux. If migrating to RHEL/Rocky, install `selinux-utils` and configure Docker with `--selinux-enabled`.

---

## 7. .env File Permissions

```bash
chmod 600 /opt/codex-wrapper/.env
chown root:root /opt/codex-wrapper/.env
```

Verify no secrets are visible in process listings:
```bash
# Confirm env vars are not in /proc/<pid>/cmdline
docker compose exec gateway cat /proc/1/cmdline | tr '\0' '\n'
# Should show uvicorn args only, no secret values
```

---

## 8. Verification Checklist

Run after initial hardening and after any major change:

```bash
# UFW status
ufw status verbose

# Docker userns-remap active
docker info | grep -i "userns"

# SSH password auth disabled
ssh -o BatchMode=yes -o PasswordAuthentication=yes root@localhost true 2>&1 | grep -i "denied\|refused"

# fail2ban active
fail2ban-client status

# AppArmor enforcing
aa-status | head -5

# .env permissions
stat -c "%a %U %G" /opt/codex-wrapper/.env   # expect: 600 root root

# No public-facing Docker ports (run from external host)
# nmap -p 5432,6379,8000,9090,3000,9093 <VM_PUBLIC_IP>
# All should show: filtered or closed
```
