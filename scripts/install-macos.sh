#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install_dir="$HOME/.local/share/session-writer"
config_dir="$HOME/.config/session-writer"
app_target="$HOME/Applications/Session Writer.app"
if [ -e "$install_dir" ] || [ -e "$app_target" ]; then
  printf 'Existing installation found. Move it to a backup before installing this release.\n' >&2
  exit 1
fi
sh "$project_dir/scripts/build-macos.sh"
mkdir -p "$install_dir" "$config_dir" "$HOME/Applications" "$HOME/.local/bin"
cp "$project_dir"/app/*.py "$project_dir"/app/windows_controller.ps1 "$install_dir/"
cp -R "$project_dir/dist/Session Writer.app" "$app_target"
if [ ! -e "$config_dir/config.json" ]; then
  cp "$project_dir/config.example.json" "$config_dir/config.json"
  chmod 600 "$config_dir/config.json"
fi
if [ ! -e "$HOME/.local/bin/swriter" ]; then
  cp "$project_dir/scripts/swriter" "$HOME/.local/bin/swriter"
  chmod 755 "$HOME/.local/bin/swriter"
fi
printf 'Installed without changing Codex/SSH settings. Configure %s before handoff.\n' "$config_dir/config.json"
