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
PROJECTS="$ROOT/projects"; BACKUPS="$ROOT/backups"
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
  for t in curl tar sha256sum openssl; do command -v $t >/dev/null && ok "$t" || { warn "$t missing"; bad=1; }; done
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
  cp "$ROOT/stack/docker-compose.yml" "$d/docker-compose.yml"
  dc "$p" config -q || die "compose config invalid"
  dc "$p" up -d --build --remove-orphans
  write_nginx "$p"
  wait_healthy "$p" || warn "not all services healthy yet — check: ./setup.sh status $p"
  echo; ok "project $p:"; echo "   API:  $(envget "$d/.env" LIGHT_RAG_URL)/"; echo "   Mgmt: $(envget "$d/.env" LIGHT_RAG_MGMT_URL)/"
  echo "   Access: user '$p' / the project key  (./setup.sh key $p)"
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
  *) sed -n '2,17p' "$0" ;;
esac
