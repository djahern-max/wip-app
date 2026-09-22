#!/usr/bin/env bash
# deploy/setup.sh — first-time bootstrap of the jobcost.dev droplet (F05.0, D-27).
#
# Run as root on the droplet (Ubuntu 24.04):  bash setup.sh
# Idempotent, step by step: each step tests before it acts and prints
# "done: <step>" or "skipped: <step>". A second run on the same server changes
# nothing. It installs packages, the wip user and its deploy key, swap, the checkout
# at /opt/wip, the two environment files (from the templates, only if absent), the
# venv, the systemd units (enabled, not started) and nginx with a Let's Encrypt
# certificate. It does not run migrations and does not start the services: the env
# files are empty until the owner fills them; the last lines say what to do next.
# No secret and no hostname other than jobcost.dev (and github.com) appears here.
set -euo pipefail

DOMAIN=jobcost.dev
CONTACT=admin@jobcost.dev
APP_USER=wip
APP_DIR=/opt/wip
ETC_DIR=/etc/wip
WEBROOT=/var/www/certbot
REPO_URL="${REPO_URL:-git@github.com:djahern-max/wip-app.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

done_() { echo "done:    $1"; }
skip()  { echo "skipped: $1"; }
fail()  { echo "setup.sh: $1" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root"

# --- 1. packages -----------------------------------------------------------------------
PACKAGES=(nginx certbot python3-certbot-nginx python3-venv git postgresql-client-16 ca-certificates curl gnupg)
missing=()
for p in "${PACKAGES[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done
if [ ${#missing[@]} -gt 0 ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q "${missing[@]}"
    done_ "packages (${missing[*]})"
else
    skip "packages"
fi

# --- 2. the wip user (no shell login) and its deploy key --------------------------------
if id "$APP_USER" >/dev/null 2>&1; then
    skip "user $APP_USER"
else
    adduser --system --group --home "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
    done_ "user $APP_USER"
fi
[ -d "/home/$APP_USER" ] || install -d -m 750 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER"
SSH_DIR="/home/$APP_USER/.ssh"
KEY="$SSH_DIR/id_ed25519"
if [ -f "$KEY" ]; then
    skip "deploy key"
else
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$SSH_DIR"
    sudo -u "$APP_USER" ssh-keygen -q -t ed25519 -N "" -C "$APP_USER@$DOMAIN deploy key" -f "$KEY"
    done_ "deploy key"
fi
if [ -f "$SSH_DIR/known_hosts" ] && grep -q "^github.com" "$SSH_DIR/known_hosts"; then
    skip "github.com host key"
else
    sudo -u "$APP_USER" sh -c "ssh-keyscan -t ed25519,rsa,ecdsa github.com >> '$SSH_DIR/known_hosts' 2>/dev/null"
    chmod 600 "$SSH_DIR/known_hosts"
    done_ "github.com host key (compare fingerprints with GitHub's published ones)"
    ssh-keygen -lf "$SSH_DIR/known_hosts"
fi

# --- 3. swap (2 GB; the frontend build peaks at a few hundred MB on 2 GB of RAM) ----------
if [ -n "$(swapon --show --noheadings)" ]; then
    skip "swap"
else
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap -q /swapfile
    swapon /swapfile
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
    done_ "swap (2 GB at /swapfile)"
fi

# --- 4. the checkout at /opt/wip -------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    skip "checkout $APP_DIR"
else
    install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
    if ! sudo -u "$APP_USER" -H git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"; then
        rmdir "$APP_DIR" 2>/dev/null || true
        echo
        echo "The clone of $REPO_URL failed. If the repository is private, add this public key"
        echo "as a read-only deploy key (GitHub → repository Settings → Deploy keys), then run"
        echo "setup.sh again; every step above is skipped on the second run."
        echo
        cat "$KEY.pub"
        exit 1
    fi
    done_ "checkout $APP_DIR ($REPO_BRANCH)"
fi
[ "$(stat -c %a "$APP_DIR")" = 755 ] || chmod 755 "$APP_DIR"  # nginx (www-data) reads frontend/dist

# --- 5. Node from NodeSource, the major pinned by frontend/.nvmrc --------------------------
NODE_MAJOR="$(tr -d '[:space:]' < "$APP_DIR/frontend/.nvmrc")"
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || fail "frontend/.nvmrc must hold a major version, got '$NODE_MAJOR'"
KEYRING=/etc/apt/keyrings/nodesource.gpg
SOURCES=/etc/apt/sources.list.d/nodesource.list
WANT_SOURCE="deb [signed-by=$KEYRING] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main"
if [ -f "$KEYRING" ] && [ -f "$SOURCES" ] && [ "$(cat "$SOURCES")" = "$WANT_SOURCE" ]; then
    skip "NodeSource repository (node_${NODE_MAJOR}.x)"
else
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor --yes -o "$KEYRING"
    echo "$WANT_SOURCE" > "$SOURCES"
    apt-get update -q
    done_ "NodeSource repository (node_${NODE_MAJOR}.x)"
fi
if command -v node >/dev/null 2>&1 && [[ "$(node --version)" == "v${NODE_MAJOR}."* ]]; then
    skip "nodejs $(node --version)"
else
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q nodejs
    [[ "$(node --version)" == "v${NODE_MAJOR}."* ]] || fail "installed node is $(node --version), wanted v${NODE_MAJOR}"
    done_ "nodejs $(node --version)"
fi

# --- 6. /etc/wip and the two environment files (never overwritten) -----------------------
if [ -d "$ETC_DIR" ] && [ "$(stat -c '%U:%G %a' "$ETC_DIR")" = "root:$APP_USER 750" ]; then
    skip "$ETC_DIR"
else
    install -d -m 750 -o root -g "$APP_USER" "$ETC_DIR"
    done_ "$ETC_DIR (root:$APP_USER 750)"
fi
if [ -f "$ETC_DIR/app.env" ]; then
    skip "$ETC_DIR/app.env (exists; not touched)"
else
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/deploy/env.template" "$ETC_DIR/app.env"
    done_ "$ETC_DIR/app.env from deploy/env.template ($APP_USER:$APP_USER 600)"
fi
if [ -f "$ETC_DIR/migrate.env" ]; then
    skip "$ETC_DIR/migrate.env (exists; not touched)"
else
    install -m 600 -o root -g root "$APP_DIR/deploy/migrate.env.template" "$ETC_DIR/migrate.env"
    done_ "$ETC_DIR/migrate.env from deploy/migrate.env.template (root:root 600)"
fi

# --- 7. the venv (deploy.sh installs the package into it) ----------------------------------
VENV="$APP_DIR/backend/.venv"
if [ -x "$VENV/bin/python" ]; then
    skip "venv $VENV"
else
    sudo -u "$APP_USER" -H python3 -m venv "$VENV"
    sudo -u "$APP_USER" -H "$VENV/bin/pip" install --quiet --upgrade pip
    done_ "venv $VENV"
fi

# --- 8. systemd units (installed and enabled; started by deploy.sh) ---------------------------
units_changed=0
for unit in wip-api.service wip-worker.service; do
    src="$APP_DIR/deploy/systemd/$unit"
    dst="/etc/systemd/system/$unit"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        skip "unit $unit"
    else
        install -m 644 -o root -g root "$src" "$dst"
        units_changed=1
        done_ "unit $unit"
    fi
done
[ "$units_changed" = 1 ] && systemctl daemon-reload
for unit in wip-api wip-worker; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        skip "enable $unit"
    else
        systemctl enable --quiet "$unit"
        done_ "enable $unit"
    fi
done

# --- 9. nginx: snippets, webroot, the site, the certificate ----------------------------------
install_file() {  # install_file <src> <dst> <label>; returns 0 when the file changed
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        skip "$3"
        return 1
    fi
    install -m 644 -o root -g root "$1" "$2"
    done_ "$3"
    return 0
}
nginx_changed=0
install_file "$APP_DIR/deploy/nginx/security-headers.conf" /etc/nginx/snippets/wip-security-headers.conf "nginx snippet wip-security-headers.conf" && nginx_changed=1
install_file "$APP_DIR/deploy/nginx/tls.conf" /etc/nginx/snippets/wip-tls.conf "nginx snippet wip-tls.conf" && nginx_changed=1
if [ -e /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
    nginx_changed=1
    done_ "remove the default nginx site"
else
    skip "remove the default nginx site"
fi
if [ -d "$WEBROOT" ]; then
    skip "webroot $WEBROOT"
else
    install -d -m 755 -o root -g root "$WEBROOT"
    done_ "webroot $WEBROOT"
fi
SITE_AVAILABLE="/etc/nginx/sites-available/$DOMAIN"
SITE_ENABLED="/etc/nginx/sites-enabled/$DOMAIN"
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    # No certificate yet: serve port 80 only, answer the challenge, get the certificate.
    install_file "$APP_DIR/deploy/nginx/bootstrap-http.conf" "$SITE_AVAILABLE" "nginx site $DOMAIN (bootstrap, port 80)" || true
    ln -sf "$SITE_AVAILABLE" "$SITE_ENABLED"
    nginx -t
    systemctl enable --quiet nginx
    systemctl restart nginx
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" -d "www.$DOMAIN" \
        --non-interactive --agree-tos -m "$CONTACT" --no-eff-email \
        --deploy-hook "systemctl reload nginx"
    done_ "certificate for $DOMAIN and www.$DOMAIN (renewal: certbot.timer, then nginx reload)"
    nginx_changed=1
else
    skip "certificate for $DOMAIN (exists; certbot.timer renews it)"
fi
install_file "$APP_DIR/deploy/nginx/$DOMAIN.conf" "$SITE_AVAILABLE" "nginx site $DOMAIN" && nginx_changed=1
if [ "$(readlink -f "$SITE_ENABLED" 2>/dev/null)" = "$SITE_AVAILABLE" ]; then
    skip "enable nginx site $DOMAIN"
else
    ln -sf "$SITE_AVAILABLE" "$SITE_ENABLED"
    nginx_changed=1
    done_ "enable nginx site $DOMAIN"
fi
if [ "$nginx_changed" = 1 ]; then
    nginx -t
    systemctl enable --quiet nginx
    systemctl reload nginx
    done_ "nginx reload"
else
    skip "nginx reload"
fi

# --- next ------------------------------------------------------------------------------------
cat <<EOF

setup.sh finished. Next (docs/OPERATIONS.md, "Production (jobcost.dev)"):
  1. Fill $ETC_DIR/app.env (as root; owner $APP_USER, mode 600) and $ETC_DIR/migrate.env
     (root only). Generate CRYPTO_KEYS on this server; never paste it anywhere else.
  2. Create the roles and the database on the Managed Postgres cluster from this server:
     db/init/01_roles.sh with APP_DATABASES=wip (see the runbook for the variables).
  3. Deploy the first release:  bash $APP_DIR/deploy/deploy.sh
     (migrates, builds, starts wip-api and wip-worker, checks https://$DOMAIN/api/health).
  4. Then:  sudo -u $APP_USER ENV_FILE=$ETC_DIR/app.env $APP_DIR/backend/.venv/bin/python \\
              $APP_DIR/backend/scripts/prod_check.py
EOF
