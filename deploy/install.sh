#!/usr/bin/env bash
# Ubuntu/Debian systemd install/update. Does not start WeChat or copy local secrets.
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo 'Run with sudo bash deploy/install.sh' >&2
  exit 1
fi
for command in python3 node npm runuser systemctl install; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 1; }
done
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=/opt/wechat-didi-assistant
state_dir=/var/lib/wechat-didi-assistant
config_file=/etc/wechat-didi-assistant.env
if [[ "$source_dir" == "$app_dir" ]]; then
  echo 'Run the installer from a separate source checkout, not /opt/wechat-didi-assistant.' >&2
  exit 1
fi
if ! id wechat-didi >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$state_dir" --shell /usr/sbin/nologin wechat-didi
fi
systemctl stop wechat-didi.service 2>/dev/null || true
install -d -m 0755 "$app_dir"
install -d -m 0700 -o wechat-didi -g wechat-didi "$state_dir"
for file in agent.py main.py wechat.py memory_mcp.py memory_bridge.mjs place_search.py admin_ui.py admin.html package.json package-lock.json; do
  install -m 0644 "$source_dir/$file" "$app_dir/$file"
done
# Install the locked dependencies without lifecycle scripts, under an unprivileged account.
chown -R wechat-didi:wechat-didi "$app_dir"
runuser -u wechat-didi -- env HOME="$state_dir" npm_config_cache="$state_dir/npm-cache" \
  npm ci --prefix "$app_dir" --omit=dev --ignore-scripts --no-audit --no-fund
chown -R root:root "$app_dir"
if [[ ! -e "$config_file" ]]; then
  install -m 0640 -o root -g wechat-didi "$source_dir/.env.example" "$config_file"
fi
install -m 0644 "$source_dir/deploy/wechat-didi.service" /etc/systemd/system/wechat-didi.service
install -m 0755 "$source_dir/deploy/assistant" /usr/local/bin/wechat-didi
systemctl daemon-reload
echo 'Installed. Existing configuration and runtime data were preserved.'
echo 'Next: edit /etc/wechat-didi-assistant.env, complete WeChat login, then enable the service.'
echo 'Follow README.md. The service has not been started.'
