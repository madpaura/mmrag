#!/usr/bin/env bash
# mmrag per-project deployer: LightRAG + Postgres/pgvector + Neo4j + management portal, behind the host nginx.
#
#   ./setup.sh check                    prerequisites only
#   ./setup.sh deploy  <proj>           create (first time) or update + start a project
#   ./setup.sh down    <proj>           stop a project (data kept)
#   ./setup.sh status  [proj]           containers + health
#   ./setup.sh list                     projects and their URLs
#   ./setup.sh backup  <proj>           full cold backup -> backups/<proj>-<ts>.tar.gz
#   ./setup.sh restore <proj> <archive> sanity-check archive, then restore everything
#   ./setup.sh destroy <proj>           stop and DELETE all data of a project
#   ./setup.sh key        <proj>        show the project's access key (API key = portal password)
#   ./setup.sh rotate-key <proj>        replace the key everywhere (LightRAG, portal, nginx)
#   ./setup.sh trust-ca <url|cert-file>... [-y]   trust a company CA (fixes "certificate verify failed")
#   ./setup.sh trust-ca --list                    show trusted company CAs
#
# Global settings (env or config/global.env):
#   BASE_URL (http://mydomain.com)  URL_ROOT (/mmrag)  NGINX_CONF_DIR (/etc/nginx/mmrag.d)
#   NGINX_RELOAD (1)  LITELLM_URL (http://localhost:4000)  PORT_BASE (19600)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$ROOT/config/global.env" ]] && set -a && . "$ROOT/config/global.env" && set +a
BASE_URL="${BASE_URL:-http://mydomain.com}"; BASE_URL="${BASE_URL%/}"
URL_ROOT="${URL_ROOT:-/mmrag}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx/mmrag.d}"
NGINX_RELOAD="${NGINX_RELOAD:-1}"
LITELLM_URL="${LITELLM_URL:-http://localhost:4000}"
PORT_BASE="${PORT_BASE:-19600}"
PROJECTS="$ROOT/projects"; BACKUPS="$ROOT/backups"; CA_DIR="$ROOT/config/ca"
VOLUMES=(postgres_data neo4j_data rag_inputs rag_storage portal_data)

c_red=$'\e[31m'; c_grn=$'\e[32m'; c_yel=$'\e[33m'; c_off=$'\e[0m'
ok()   { echo "${c_grn}✓${c_off} $*"; }
warn() { echo "${c_yel}!${c_off} $*"; }
die()  { echo "${c_red}✗${c_off} $*" >&2; exit 1; }
SUDO=""; sudo_if_needed() { [[ -w "$1" ]] && SUDO="" || SUDO="sudo"; }

valid_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "project name must match [a-z0-9-] (got '$1')"; }
pdir()  { echo "$PROJECTS/$1"; }
dc()    { local p="$1"; shift; docker compose --project-directory "$(pdir "$p")" -f "$(pdir "$p")/docker-compose.yml" "$@"; }
envget(){ grep -E "^$2=" "$1" | tail -1 | cut -d= -f2-; }
envset(){ if grep -qE "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi; }
rand()  { head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24; }
randkey(){ printf 'mmr_%s' "$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"; }
port_free() { ! (ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null) | awk '{print $4}' | grep -qE "[:.]$1\$"; }
used_ports(){ cat "$PROJECTS"/*/.env 2>/dev/null | grep -E '^(LIGHTRAG|PORTAL)_PORT=' | cut -d= -f2; }
next_port() { local p=$PORT_BASE; while used_ports | grep -qx "$p" || ! port_free "$p"; do p=$((p+1)); done; echo "$p"; }

# ---------------------------------------------------------------- prerequisites
check() {
  local bad=0
  command -v docker >/dev/null && ok "docker $(docker --version | awk '{print $3}' | tr -d ,)" || { warn "docker missing"; bad=1; }
  docker info >/dev/null 2>&1 && ok "docker daemon reachable" || { warn "cannot talk to docker daemon (permissions?)"; bad=1; }
  docker compose version >/dev/null 2>&1 && ok "docker compose v2" || { warn "docker compose v2 missing"; bad=1; }
  for t in curl tar sha256sum openssl awk; do command -v $t >/dev/null && ok "$t" || { warn "$t missing"; bad=1; }; done
  [[ -f "$ROOT/patches/pipeline.py" ]] && ok "LightRAG patch present" || { warn "patches/pipeline.py missing"; bad=1; }
  if [[ ! -f "$ROOT/stack/lightrag.env.tmpl" && -f "$ROOT/stack/lightrag.env.tmpl.example" ]]; then
    cp "$ROOT/stack/lightrag.env.tmpl.example" "$ROOT/stack/lightrag.env.tmpl"
    warn "created stack/lightrag.env.tmpl from the example — set the LiteLLM API key / models in it (it is git-ignored)"
  fi
  [[ -f "$ROOT/stack/lightrag.env.tmpl" ]] && ok "LightRAG env template" || { warn "stack/lightrag.env.tmpl missing"; bad=1; }
  grep -q "__LITELLM_API_KEY__" "$ROOT/stack/lightrag.env.tmpl" 2>/dev/null && warn "stack/lightrag.env.tmpl still has the __LITELLM_API_KEY__ placeholder"
  local free; free=$(df -Pk "$ROOT" | awk 'NR==2{print int($4/1048576)}')
  (( free >= 10 )) && ok "disk: ${free}G free" || warn "disk: only ${free}G free (>=10G recommended)"
  curl -fsS -m 5 "$LITELLM_URL/health/liveliness" >/dev/null 2>&1 || curl -fsS -m 5 -o /dev/null "$LITELLM_URL/" 2>/dev/null \
    && ok "LiteLLM reachable at $LITELLM_URL" || warn "LiteLLM not reachable at $LITELLM_URL (LLM calls will fail)"
  if command -v nginx >/dev/null; then
    ok "nginx $(nginx -v 2>&1 | cut -d/ -f2)"
    grep -rqs "mmrag.d" /etc/nginx/ 2>/dev/null && ok "nginx includes $NGINX_CONF_DIR" \
      || warn "no nginx server block includes $NGINX_CONF_DIR/*.conf — see stack/nginx-server.example.conf"
  else warn "nginx not installed — routes will be generated but not activated"; fi
  return $bad
}

# ---------------------------------------------------------------- deploy
# One secret per project: LIGHTRAG_API_KEY. LightRAG enforces it (X-API-Key); nginx accepts the same key as the
# basic-auth password (user = project name) for browsers and then forwards the key to LightRAG.
ensure_key() {
  local d; d="$(pdir "$1")"
  if [[ -z "$(envget "$d/.env" LIGHTRAG_API_KEY 2>/dev/null)" ]]; then
    envset "$d/.env" LIGHTRAG_API_KEY "$(randkey)"
    ok "generated access key for $1 (show it with: ./setup.sh key $1)"
  fi
  [[ -n "$(envget "$d/.env" MMRAG_SESSION 2>/dev/null)" ]] || envset "$d/.env" MMRAG_SESSION "$(randkey)"   # browser cookie
  # only the health check stays open. LightRAG matches the *prefixed* path when LIGHTRAG_API_PREFIX is set, so list
  # both forms. (Its default "/health,/api/*" would also leave the Ollama-compatible /api/* chat API open.)
  envset "$d/.env" WHITELIST_PATHS "/health,$(envget "$d/.env" API_PREFIX)/health"
  chmod 600 "$d/.env"
}

write_nginx() {
  local p="$1" d key; d="$(pdir "$p")"; key="$(envget "$d/.env" LIGHTRAG_API_KEY)"
  [[ -n "$key" ]] || die "no LIGHTRAG_API_KEY in $d/.env"
  sed -e "s|__PROJECT__|$p|g" -e "s|__BASE_URL__|$BASE_URL|g" \
      -e "s|__API_PREFIX__|$(envget "$d/.env" API_PREFIX)|g" -e "s|__MGMT_PREFIX__|$(envget "$d/.env" MGMT_PREFIX)|g" \
      -e "s|__LIGHTRAG_PORT__|$(envget "$d/.env" LIGHTRAG_PORT)|g" -e "s|__PORTAL_PORT__|$(envget "$d/.env" PORTAL_PORT)|g" \
      -e "s|__API_KEY__|$key|g" -e "s|__HTPASSWD__|$NGINX_CONF_DIR/$p.htpasswd|g" \
      -e "s|__SESSION__|$(envget "$d/.env" MMRAG_SESSION)|g" -e "s|__COOKIE__|mmrag_${p//-/_}|g" -e "s|__URL_ROOT__|$URL_ROOT|g" \
      "$ROOT/stack/nginx.tmpl.conf" > "$d/nginx.conf"
  printf '%s:%s\n' "$p" "$(openssl passwd -apr1 "$key")" > "$d/htpasswd"
  chmod 600 "$d/nginx.conf" "$d/htpasswd"
  install_nginx "$p"
}

install_nginx() {
  local p="$1"
  command -v nginx >/dev/null || { warn "nginx missing; install $(pdir "$p")/nginx.conf and htpasswd manually"; return 0; }
  [[ -d "$NGINX_CONF_DIR" ]] || { sudo_if_needed "$(dirname "$NGINX_CONF_DIR")"; $SUDO mkdir -p "$NGINX_CONF_DIR"; }
  sudo_if_needed "$NGINX_CONF_DIR"
  $SUDO install -m 0640 "$(pdir "$p")/nginx.conf" "$NGINX_CONF_DIR/$p.conf"      # holds the key: root/nginx only
  $SUDO install -m 0644 "$(pdir "$p")/htpasswd" "$NGINX_CONF_DIR/$p.htpasswd"    # hash only; workers must read it
  reload_nginx
}

show_key() {
  local p="$1" d; need_proj "$p"; d="$(pdir "$p")"
  echo "project : $p"
  echo "key     : $(envget "$d/.env" LIGHTRAG_API_KEY)"
  echo "API     : curl -H 'X-API-Key: <key>' $(envget "$d/.env" LIGHT_RAG_URL)/documents/status_counts"
  echo "portal  : $(envget "$d/.env" LIGHT_RAG_MGMT_URL)/   (user: $p, password: <key>)"
}

rotate_key() {
  local p="$1" d; need_proj "$p"; d="$(pdir "$p")"
  envset "$d/.env" LIGHTRAG_API_KEY "$(randkey)"
  envset "$d/.env" MMRAG_SESSION "$(randkey)"     # also signs out every browser
  dc "$p" up -d rag-app portal          # both read the key from .env
  apply_env "$p"
  write_nginx "$p"
  wait_healthy "$p" || warn "services not healthy yet"
  ok "new key for $p — scripts and browsers must use it now:"; show_key "$p"
}

reload_nginx() {
  [[ "$NGINX_RELOAD" == 1 ]] || { warn "NGINX_RELOAD=0: reload nginx yourself"; return 0; }
  local S=""; [[ $EUID -ne 0 ]] && S=sudo
  if $S nginx -t >/dev/null 2>&1; then $S nginx -s reload && ok "nginx reloaded"
  else $S nginx -t || true; die "nginx config test failed — fix before continuing"; fi
}

init_project() {
  local p="$1" d; d="$(pdir "$p")"
  mkdir -p "$d/prompts"
  local lp pp; lp=$(next_port); PORT_BASE=$((lp+1)) pp=$(next_port)
  sed -e "s|__GENERATED__|$(rand)|" "$ROOT/stack/lightrag.env.tmpl" > "$d/.env"
  sed -i "0,/^NEO4J_PASSWORD=.*/s||NEO4J_PASSWORD=$(rand)|" "$d/.env"
  sed -i "s|__WEBUI_TITLE__|'RAG · $p'|" "$d/.env"
  cat >> "$d/.env" <<EOF

# ── mmrag deployment (setup.sh) ──
PROJECT=$p
BASE_URL=$BASE_URL
API_PREFIX=$URL_ROOT/$p/api
MGMT_PREFIX=$URL_ROOT/$p/mgmt
LIGHT_RAG_URL=$BASE_URL$URL_ROOT/$p/api
LIGHT_RAG_MGMT_URL=$BASE_URL$URL_ROOT/$p/mgmt
LIGHTRAG_PORT=$lp
PORTAL_PORT=$pp
#LIGHTRAG_IMAGE=ghcr.io/hkuds/lightrag:v1.5.2   # must match patches/pipeline.py
EOF
  chmod 600 "$d/.env"
  ok "created $d/.env (ports $lp/$pp, fresh DB passwords)"
}

deploy() {
  local p="$1" d; valid_name "$p"; d="$(pdir "$p")"
  check || die "prerequisites failed (see above)"
  mkdir -p "$d"
  [[ -f "$d/.env" ]] || init_project "$p"
  ensure_key "$p"
  mkdir -p "$CA_DIR"                   # company CAs, mounted read-only into the portal (see trust-ca)
  cp "$ROOT/stack/docker-compose.yml" "$d/docker-compose.yml"
  dc "$p" config -q || die "compose config invalid"
  dc "$p" up -d --build --remove-orphans
  apply_env "$p"
  write_nginx "$p"
  wait_healthy "$p" || warn "not all services healthy yet — check: ./setup.sh status $p"
  echo; ok "project $p:"; echo "   API:  $(envget "$d/.env" LIGHT_RAG_URL)/"; echo "   Mgmt: $(envget "$d/.env" LIGHT_RAG_MGMT_URL)/"
  echo "   Access: user '$p' / the project key  (./setup.sh key $p)"
}

# rag-app reads .env when it starts (stack/lightrag_run.py), not when compose creates it: restart it if the file
# changed since. Compares with the hash the running container reports in its status file.
apply_env() {
  local p="$1" want have i
  want=$(sha256sum "$(pdir "$p")/.env" | cut -d' ' -f1)
  for i in 1 2 3 4 5 6 7 8 9 10; do     # a just-created container writes its status within a second
    have=$(dc "$p" exec -T rag-app cat /mmrag/control/status.json 2>/dev/null | grep -o '"env_sha256": *"[0-9a-f]*"' | grep -o '[0-9a-f]\{64\}' || true)
    [[ -n "$have" ]] && break; sleep 1
  done
  [[ "$want" == "$have" ]] && return 0
  dc "$p" restart rag-app >/dev/null && ok "rag-app restarted to load the current .env"
}

wait_healthy() {
  local p="$1" i
  for i in $(seq 1 60); do
    local states; states=$(dc "$p" ps --format '{{.Service}} {{.Health}}' 2>/dev/null)
    if ! grep -qvE ' healthy$' <<<"$states"; then ok "all services healthy"; return 0; fi
    sleep 5
  done
  echo "$states"; return 1
}

need_proj() { [[ -n "${1:-}" ]] || die "project name required"; valid_name "$1"; [[ -f "$(pdir "$1")/.env" ]] || die "unknown project '$1'"; }

status() {
  if [[ -n "${1:-}" ]]; then need_proj "$1"; dc "$1" ps; return; fi
  for d in "$PROJECTS"/*/; do [[ -f "$d/.env" ]] && { echo "== $(basename "$d")"; dc "$(basename "$d")" ps --format 'table {{.Service}}\t{{.Status}}'; }; done
}

list() {
  printf "%-14s %-50s %s\n" PROJECT API MGMT
  for d in "$PROJECTS"/*/; do [[ -f "$d/.env" ]] && printf "%-14s %-50s %s\n" "$(basename "$d")" \
    "$(envget "$d/.env" LIGHT_RAG_URL)/" "$(envget "$d/.env" LIGHT_RAG_MGMT_URL)/"; done
}

# ---------------------------------------------------------------- backup / restore
vol() { echo "mmrag-$1_$2"; }

backup() {
  local p="$1" d ts work out; need_proj "$p"; d="$(pdir "$p")"
  ts=$(date +%Y%m%d-%H%M%S); work=$(mktemp -d); out="$BACKUPS/$p-$ts.tar.gz"; mkdir -p "$BACKUPS"
  trap 'rm -rf "$work"' RETURN
  local was_up=0; dc "$p" ps -q | grep -q . && was_up=1
  echo "stopping $p for a consistent (cold) backup…"; dc "$p" stop
  mkdir -p "$work/volumes" "$work/config"
  for v in "${VOLUMES[@]}"; do
    docker volume inspect "$(vol "$p" "$v")" >/dev/null 2>&1 || { warn "volume $v missing, skipped"; continue; }
    docker run --rm -v "$(vol "$p" "$v")":/v:ro -v "$work/volumes":/out alpine tar -C /v -czf "/out/$v.tar.gz" .
    ok "volume $v"
  done
  cp -a "$d/.env" "$d/docker-compose.yml" "$d/prompts" "$work/config/" ; [[ -f "$d/nginx.conf" ]] && cp "$d/nginx.conf" "$work/config/"
  [[ -f "$d/htpasswd" ]] && cp "$d/htpasswd" "$work/config/"
  cp "$ROOT/patches/pipeline.py" "$work/config/pipeline.py"
  { echo "project=$p"; echo "created=$ts"; echo "host=$(hostname)"; echo "format=1"
    echo "lightrag_image=$(docker inspect --format '{{.Config.Image}}' "$(dc "$p" ps -aq rag-app 2>/dev/null | head -1)" 2>/dev/null || true)"; } > "$work/MANIFEST"
  (cd "$work" && find . -type f ! -name SHA256SUMS -print0 | xargs -0 sha256sum > SHA256SUMS)
  tar -C "$work" -czf "$out" .
  [[ $was_up == 1 ]] && dc "$p" start >/dev/null && ok "restarted $p"
  ok "backup: $out ($(du -h "$out" | cut -f1))"
}

restore() {
  local p="$1" a="${2:-}" d work; valid_name "$p"; [[ -f "$a" ]] || die "archive not found: $a"; d="$(pdir "$p")"
  work=$(mktemp -d); trap 'rm -rf "$work"' RETURN
  echo "sanity checks…"
  tar -tzf "$a" >/dev/null || die "archive is corrupt"
  tar -C "$work" -xzf "$a"
  [[ -f "$work/MANIFEST" && -f "$work/SHA256SUMS" ]] || die "not an mmrag backup (no MANIFEST/SHA256SUMS)"
  (cd "$work" && sha256sum -c --quiet SHA256SUMS) || die "checksum mismatch"; ok "checksums"
  local src; src=$(grep ^project= "$work/MANIFEST" | cut -d= -f2)
  [[ "$src" == "$p" ]] || warn "archive is from project '$src', restoring into '$p' (URLs/ports will be rewritten)"
  for v in "${VOLUMES[@]}"; do [[ -f "$work/volumes/$v.tar.gz" ]] || die "archive lacks volume $v"; done; ok "all volumes present"
  check >/dev/null || die "prerequisites failed (run ./setup.sh check)"
  cmp -s "$work/config/pipeline.py" "$ROOT/patches/pipeline.py" || warn "patches/pipeline.py differs from the one in the backup"
  local free need; free=$(df -Pk "$ROOT" | awk 'NR==2{print $4}'); need=$(du -sk "$work" | cut -f1)
  (( free > need * 3 )) || die "not enough disk space"
  ok "sanity checks passed"
  if [[ -f "$d/.env" ]]; then
    read -r -p "Project '$p' exists — its data will be REPLACED. Type the project name to continue: " ans
    [[ "$ans" == "$p" ]] || die "aborted"
    dc "$p" down || true
  fi
  mkdir -p "$d"; cp -a "$work/config/.env" "$work/config/docker-compose.yml" "$d/"; rm -rf "$d/prompts"; cp -a "$work/config/prompts" "$d/"
  if [[ "$src" != "$p" ]]; then
    local lp pp; lp=$(next_port); PORT_BASE=$((lp+1)) pp=$(next_port)
    envset "$d/.env" PROJECT "$p"; envset "$d/.env" API_PREFIX "$URL_ROOT/$p/api"; envset "$d/.env" MGMT_PREFIX "$URL_ROOT/$p/mgmt"
    envset "$d/.env" LIGHT_RAG_URL "$BASE_URL$URL_ROOT/$p/api"; envset "$d/.env" LIGHT_RAG_MGMT_URL "$BASE_URL$URL_ROOT/$p/mgmt"
    envset "$d/.env" LIGHTRAG_PORT "$lp"; envset "$d/.env" PORTAL_PORT "$pp"
  fi
  ensure_key "$p"
  dc "$p" create
  for v in "${VOLUMES[@]}"; do
    docker run --rm -v "$(vol "$p" "$v")":/v -v "$work/volumes":/in:ro alpine sh -c "rm -rf /v/* /v/.[!.]* 2>/dev/null; tar -C /v -xzf /in/$v.tar.gz"
    ok "restored $v"
  done
  dc "$p" up -d
  write_nginx "$p"
  wait_healthy "$p" || warn "services not healthy yet"
  ok "restore of $p complete"; list | grep -E "^($p|PROJECT) "
}

destroy() {
  local p="$1"; need_proj "$p"
  read -r -p "DELETE all data of '$p'? Type the project name: " ans; [[ "$ans" == "$p" ]] || die "aborted"
  dc "$p" down -v
  if [[ -f "$NGINX_CONF_DIR/$p.conf" ]]; then sudo_if_needed "$NGINX_CONF_DIR"; $SUDO rm -f "$NGINX_CONF_DIR/$p.conf" "$NGINX_CONF_DIR/$p.htpasswd"; command -v nginx >/dev/null && reload_nginx; fi
  rm -rf "$(pdir "$p")"; ok "destroyed $p"
}

# ---------------------------------------------------------------- company CA trust
# Internal servers often use certificates from a company CA that the portal (public CAs only) doesn't know:
#   "SSL: CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate".
# trust-ca collects the CA certificates (from the server's chain, its AIA "CA Issuers" links, or files from IT),
# shows them for confirmation, stores them in config/ca/ and restarts the portals, which trust them on top of the
# public CAs.

ca_info() {  # pretty one-liner for a PEM cert
  printf '  subject : %s\n  issuer  : %s\n  expires : %s\n  sha256  : %s\n' \
    "$(openssl x509 -in "$1" -noout -subject -nameopt oneline 2>/dev/null | sed 's/^subject= *//')" \
    "$(openssl x509 -in "$1" -noout -issuer -nameopt oneline 2>/dev/null | sed 's/^issuer= *//')" \
    "$(openssl x509 -in "$1" -noout -enddate 2>/dev/null | cut -d= -f2)" \
    "$(openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
}

split_pem() {  # split_pem <file-with-many-certs> <out-prefix>  -> out-prefix-1.pem, -2.pem ...
  awk -v pre="$2" '/-----BEGIN CERTIFICATE-----/{n++; p=1} p{print > (pre "-" n ".pem")} /-----END CERTIFICATE-----/{p=0}' "$1"
}

to_pem() {  # any of PEM / DER / PKCS#7 -> PEM certificates on stdout
  if grep -q "BEGIN CERTIFICATE" "$1" 2>/dev/null; then cat "$1"
  elif openssl x509 -inform DER -in "$1" 2>/dev/null; then :
  elif openssl pkcs7 -inform DER -in "$1" -print_certs 2>/dev/null | grep -q BEGIN; then openssl pkcs7 -inform DER -in "$1" -print_certs
  elif openssl pkcs7 -in "$1" -print_certs 2>/dev/null | grep -q BEGIN; then openssl pkcs7 -in "$1" -print_certs
  else return 1; fi
}

aia_parent() {  # download the issuer of a cert via its AIA "CA Issuers" URL (best effort) -> PEM path or ""
  local cert="$1" out="$2" url
  [[ "$(openssl x509 -in "$cert" -noout -subject -nameopt oneline)" == "subject=$(openssl x509 -in "$cert" -noout -issuer -nameopt oneline | sed 's/^issuer=//')" ]] && return 1  # self-signed root
  url=$(openssl x509 -in "$cert" -noout -text 2>/dev/null | grep -o 'CA Issuers - URI:[^ ,]*' | head -1 | sed 's/CA Issuers - URI://')
  [[ -n "$url" ]] || return 1
  curl -fsS -m 15 "$url" -o "$out.raw" 2>/dev/null || return 1
  to_pem "$out.raw" > "$out" 2>/dev/null && grep -q BEGIN "$out"
}

trust_ca() {
  mkdir -p "$CA_DIR"
  if [[ "${1:-}" == "--list" ]]; then
    local f n=0; for f in "$CA_DIR"/*.crt; do [[ -f "$f" ]] || continue; n=$((n+1)); echo "$(basename "$f")"; ca_info "$f"; done
    (( n )) || echo "no company CAs trusted yet (config/ca/ is empty)"; return 0
  fi
  local yes=0 a; local -a args=()
  for a in "$@"; do [[ "$a" == "-y" ]] && yes=1 || args+=("$a"); done
  (( ${#args[@]} )) || die "usage: ./setup.sh trust-ca <https://host[:port] | host[:port] | ca-cert-file>... [-y]"
  local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  local -a cands=() urls=()
  local i=0
  for a in "${args[@]}"; do
    i=$((i+1))
    if [[ -f "$a" ]]; then
      to_pem "$a" > "$tmp/in$i.pem" || die "$a: not a certificate (PEM, DER or PKCS#7 expected)"
      split_pem "$tmp/in$i.pem" "$tmp/f$i"; cands+=("$tmp"/f$i-*.pem)
      ok "read $(ls "$tmp"/f$i-*.pem | wc -l) certificate(s) from $a"
      continue
    fi
    local hp host port
    hp="${a#*://}"; hp="${hp%%/*}"; host="${hp%%:*}"; port="443"; [[ "$hp" == *:* ]] && port="${hp##*:}"
    urls+=("https://$host:$port/")
    openssl s_client -connect "$host:$port" -servername "$host" -showcerts </dev/null >"$tmp/s$i.txt" 2>/dev/null || true
    split_pem "$tmp/s$i.txt" "$tmp/s$i"
    local -a chain=( $(ls "$tmp"/s$i-*.pem 2>/dev/null | sort -t- -k2 -n) )
    (( ${#chain[@]} )) || die "could not read certificates from $host:$port (reachable from this host? try a .crt file from IT)"
    ok "$host:$port presents ${#chain[@]} certificate(s)"
    local top
    if (( ${#chain[@]} > 1 )); then cands+=("${chain[@]:1}"); top="${chain[-1]}"   # CAs only, skip the server's own cert
    else cands+=("${chain[0]}"); top="${chain[0]}"; warn "$host sends only its own certificate; trusting it directly (prefer the CA file from IT)"; fi
    local lvl; for lvl in 1 2 3; do                      # complete the chain upwards via AIA
      aia_parent "$top" "$tmp/aia$i-$lvl.pem" || break
      ok "fetched issuer via AIA: $(openssl x509 -in "$tmp/aia$i-$lvl.pem" -noout -subject -nameopt oneline | sed 's/^subject= *//')"
      cands+=("$tmp/aia$i-$lvl.pem"); top="$tmp/aia$i-$lvl.pem"
    done
  done
  echo; echo "Certificates to trust:"; local c; for c in "${cands[@]}"; do ca_info "$c"; echo; done
  if (( ! yes )); then
    echo "Verify the sha256 fingerprints with your IT/security team (or their published CA page)."
    read -r -p "Trust these for all projects? [y/N] " ans; [[ "$ans" =~ ^[yY] ]] || die "aborted"
  fi
  local fp cn dest n=0
  for c in "${cands[@]}"; do
    fp=$(openssl x509 -in "$c" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | cut -c1-12)
    cn=$(openssl x509 -in "$c" -noout -subject -nameopt multiline | sed -n 's/^ *commonName *= *//p' | head -1 | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)
    dest="$CA_DIR/${cn:-ca}-$fp.crt"
    [[ -f "$dest" ]] && continue
    openssl x509 -in "$c" -out "$dest" && chmod 644 "$dest" && n=$((n+1))
  done
  ok "$n new CA certificate(s) in config/ca/"
  local d p; for d in "$PROJECTS"/*/; do
    p=$(basename "$d"); [[ -f "$d/.env" ]] || continue
    cp "$ROOT/stack/docker-compose.yml" "$d/docker-compose.yml"
    dc "$p" ps -q portal 2>/dev/null | grep -q . && dc "$p" up -d portal >/dev/null 2>&1 && ok "portal of $p restarted with the new trust"
  done
  local u ctr; ctr=$(docker ps --format '{{.Names}}' | grep -m1 -E '^mmrag-.*-portal-1$' || true)
  [[ -n "$ctr" ]] || { warn "no running portal to test from"; return 0; }
  sleep 3
  for u in "${urls[@]}"; do
    if docker exec "$ctr" python -c "import sys,httpx; from mmrag_connectors import tls; httpx.get(sys.argv[1], verify=tls.verify_for(True), timeout=15)" "$u" >/dev/null 2>"$tmp/err"; then
      ok "TLS to $u verified from inside $ctr"
    else warn "still failing from $ctr: $(tail -1 "$tmp/err")"; fi
  done
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  check)   check && ok "ready" ;;
  deploy)  [[ -n "${1:-}" ]] || die "usage: setup.sh deploy <proj>"; deploy "$1" ;;
  down)    need_proj "${1:-}"; dc "$1" down ;;
  status)  status "${1:-}" ;;
  list)    list ;;
  backup)  backup "${1:-}" ;;
  restore) [[ -n "${1:-}" && -n "${2:-}" ]] || die "usage: setup.sh restore <proj> <archive>"; restore "$1" "$2" ;;
  destroy) destroy "${1:-}" ;;
  key)     show_key "${1:-}" ;;
  rotate-key) rotate_key "${1:-}" ;;
  trust-ca) trust_ca "$@" ;;
  *) sed -n '2,19p' "$0" ;;
esac
