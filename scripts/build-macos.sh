#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bundle="$project_dir/dist/Session Writer.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources/backend"
python3 "$project_dir/scripts/make-plist.py" "$bundle/Contents/Info.plist"
cp "$project_dir"/app/*.py "$project_dir"/app/windows_controller.ps1 "$bundle/Contents/Resources/backend/"
xcrun swiftc -parse-as-library -O "$project_dir/app/session_writer_ui.swift" -framework AppKit -o "$bundle/Contents/MacOS/session-writer-ui"
codesign --force --sign - "$bundle"
codesign --verify --strict "$bundle"
printf 'Built: %s\n' "$bundle"
