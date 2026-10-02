#!/bin/bash
# Recovery script: reinstall services wiped by VM replacement.
# /etc/systemd/system and /usr/bin are ephemeral; ~ (home) persists.
# Canonical unit files live in ~/workspace/muse-bridge/systemd/.
set -e
HOME_DIR=/home/USER
SYSTEMD_DIR="$HOME_DIR/workspace/muse-bridge/systemd"

echo "[1/4] update-ca-certificates (Hatch egress CA for TLS-intercepting proxy)"
update-ca-certificates >/dev/null 2>&1 || true

echo "[2/4] 9router binary (verified runnable, retry on corrupt install)"
nine_ok() { [ -x /usr/bin/9router ] && /usr/bin/9router --version >/dev/null 2>&1; }
if ! nine_ok; then
  # Bersihkan sisa install korup (kasus 1 Okt 2026: TAR_ENTRY_ERROR, binary ada tapi rusak)
  rm -rf /usr/lib/node_modules/9router /usr/bin/9router
  # Cari npm yang benar-benar jalan (wrapper /usr/bin/npm sering rusak pasca-replace: MODULE_NOT_FOUND)
  find_npm() {
    for c in /opt/hatch-image/bin/npm-package/bin/npm-cli.js \
             /home/USER/.hermes/tools/node-26.7.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js \
             /home/USER/.hermes/tools/npm-12.0.2-linux-x64/lib/node_modules/npm/bin/npm-cli.js; do
      [ -f "$c" ] && node "$c" --version >/dev/null 2>&1 && { echo "node $c"; return 0; }
    done
    command -v npm >/dev/null && npm --version >/dev/null 2>&1 && { echo npm; return 0; }
    echo npm
  }
  NPM_BIN=$(find_npm)
  echo "  npm: $NPM_BIN"
  $NPM_BIN install -g 9router@0.5.91 || $NPM_BIN install -g 9router
fi
if ! nine_ok; then
  echo "  WARNING: 9router masih tidak bisa jalan setelah install ulang; coba manual"
else
  echo "  ok: $(/usr/bin/9router --version 2>/dev/null)"
fi

echo "[2b/4] sshd (openssh-server di-wipe tiap VM replacement)"
if ! command -v sshd >/dev/null 2>&1; then
  DEB=$(ls /var/cache/apt/archives/openssh-server_*.deb 2>/dev/null | head -1)
  SFTPDEB=$(ls /var/cache/apt/archives/openssh-sftp-server_*.deb 2>/dev/null | head -1)
  if [ -n "$DEB" ]; then
    export DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFNEW=1
    dpkg -i ${SFTPDEB:+$SFTPDEB }"$DEB" >/dev/null 2>&1 || true
    dpkg --configure -a >/dev/null 2>&1 || true
    echo "  openssh-server installed from local apt cache"
  else
    P=$(grep -m1 '^TELEGRAM_PROXY=' $HOME_DIR/.hermes/.env | cut -d= -f2-); P=${P%\"}; P=${P#\"}
    export http_proxy="$P" https_proxy="$P" HTTP_PROXY="$P" HTTPS_PROXY="$P"
    echo 'Acquire::Languages "none";' > /etc/apt/apt.conf.d/99-no-languages
    # Tunggu apt lock milik platform (os-intent replay apt-get update bisa macet >10 mnt), max ~5 mnt
    for i in $(seq 1 10); do
      if ! fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1; then break; fi
      echo "  waiting for apt lock ($i/10)..."
      sleep 30
    done
    if apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server openssh-sftp-server 2>/dev/null; then
      echo "  openssh-server installed via apt"
    else
      # Fallback: download .deb langsung dari mirror (bypass apt yang macet)
      echo "  apt failed, trying direct .deb download..."
      DL=/tmp/sshd-dl; mkdir -p "$DL"; cd "$DL"
      VER=$(curl -s --max-time 60 "http://azure.archive.ubuntu.com/ubuntu/dists/noble-updates/main/binary-amd64/Packages.gz" | zcat 2>/dev/null | awk '/^Package: openssh-server$/{f=1} f&&/^Filename:/{print $2; exit}')
      if [ -n "$VER" ]; then
        curl -s --max-time 120 -O "http://azure.archive.ubuntu.com/ubuntu/$VER" && \
        curl -s --max-time 120 -O "http://azure.archive.ubuntu.com/ubuntu/${VER/openssh-server/openssh-sftp-server}" && \
        DEBIAN_FRONTEND=noninteractive dpkg -i openssh-sftp-server_*.deb openssh-server_*.deb >/dev/null 2>&1 || true
        dpkg --configure -a >/dev/null 2>&1 || true
        # Simpan ke apt cache agar recovery berikutnya pakai jalur cepat
        cp -n "$DL"/openssh-server_*.deb "$DL"/openssh-sftp-server_*.deb /var/cache/apt/archives/ 2>/dev/null || true
        echo "  openssh-server installed from direct .deb download"
      else
        echo "  WARNING: openssh-server install failed, sshd will be missing"
      fi
      cd - >/dev/null
    fi
  fi
fi
if ls $HOME_DIR/workspace/ssh-vps/host-keys/ssh_host_* >/dev/null 2>&1; then
  cp $HOME_DIR/workspace/ssh-vps/host-keys/ssh_host_* /etc/ssh/
  chmod 600 /etc/ssh/ssh_host_*_key 2>/dev/null || true
fi
mkdir -p /etc/ssh/sshd_config.d /run/sshd
# VM replacement kadang me-wipe /etc/ssh/sshd_config (conffile) + user sshd + /run/sshd
if [ ! -f /etc/ssh/sshd_config ]; then
  cat > /etc/ssh/sshd_config <<'SSHD_EOF'
# Base sshd_config (restored after VM replacement; overrides in sshd_config.d/)
Include /etc/ssh/sshd_config.d/*.conf
KbdInteractiveAuthentication no
UsePAM yes
ChallengeResponseAuthentication no
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD_EOF
fi
id sshd >/dev/null 2>&1 || adduser --system --group --no-create-home --home /run/sshd --shell /usr/sbin/nologin sshd >/dev/null 2>&1
cp $HOME_DIR/workspace/ssh-vps/sshd_config.d/99-dhodi.conf /etc/ssh/sshd_config.d/99-dhodi.conf
mkdir -p /root/.ssh && chmod 700 /root/.ssh
cp $HOME_DIR/workspace/ssh-vps/keys/dhodi-vm.pub /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
sshd -t && systemctl reset-failed ssh 2>/dev/null; systemctl enable --now ssh || true; echo "  sshd active: $(systemctl is-active ssh 2>/dev/null)" || echo "  WARNING: ssh enable/start gagal, lanjut ke step berikutnya"

echo "[3/4] install systemd units"
for svc in 9router.service muse-bridge.service hermes-gateway.service lt-9router-tunnel.service; do
  [ -f "$SYSTEMD_DIR/$svc" ] && cp "$SYSTEMD_DIR/$svc" /etc/systemd/system/$svc
done
for svc in vps-9router-tunnel.service vps-dhodi-tunnel.service vps-sshd-tunnel.service vps-envelopred-tunnel.service; do
  if [ -f "$HOME_DIR/workspace/ssh-vps/systemd/$svc" ]; then
    cp "$HOME_DIR/workspace/ssh-vps/systemd/$svc" /etc/systemd/system/$svc
  fi
done
for svc in agentx-daemon.service rapat-orchestrator.service; do
  if [ -f "$HOME_DIR/workspace/agentx/systemd/$svc" ]; then
    cp "$HOME_DIR/workspace/agentx/systemd/$svc" /etc/systemd/system/$svc
  fi
done
systemctl daemon-reload
systemctl enable 9router.service muse-bridge.service hermes-gateway.service lt-9router-tunnel.service 2>/dev/null || true
for svc in vps-9router-tunnel.service vps-dhodi-tunnel.service vps-sshd-tunnel.service vps-envelopred-tunnel.service agentx-daemon.service rapat-orchestrator.service; do
  [ -f /etc/systemd/system/$svc ] && systemctl enable $svc
done

echo "[4/4] start services"
for svc in 9router.service muse-bridge.service hermes-gateway.service lt-9router-tunnel.service; do
  [ -f /etc/systemd/system/$svc ] && systemctl restart $svc
done
for svc in vps-9router-tunnel.service vps-dhodi-tunnel.service vps-sshd-tunnel.service vps-envelopred-tunnel.service agentx-daemon.service rapat-orchestrator.service; do
  [ -f /etc/systemd/system/$svc ] && systemctl restart $svc
done
sleep 5
systemctl is-active 9router.service muse-bridge.service hermes-gateway.service
echo "[4b/4] WordPress EnvelopRed (di-wipe tiap VM replacement -> deploy ulang)"
if [ ! -d /var/www/envelopred ] && [ -x "$HOME_DIR/workspace/your_files/wordpress-envelopred/deploy-local.sh" ]; then
  "$HOME_DIR/workspace/your_files/wordpress-envelopred/deploy-local.sh" >/var/log/envelopred-redeploy.log 2>&1 || echo "  WARNING: deploy-local.sh gagal, lihat /var/log/envelopred-redeploy.log"
else
  echo "  /var/www/envelopred sudah ada / script tidak ditemukan, lewati"
fi
echo "done."
