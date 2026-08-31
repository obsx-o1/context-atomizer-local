#!/bin/sh
set -eu

install_root=${ATOMIZER_MACOS_INSTALL_ROOT:-"$HOME/Library/Application Support/Context Atomizer/runtime"}

"$install_root/atomizer-local-manager" uninstall --claude-settings "$HOME/.claude/settings.json"
for name in atomizer-local-runtime atomizer-local-manager atomizer-local-open-library atomizer-codex-hook atomizer-claude-hook atomizer-local-mcp runtime-build-identity.json; do
    rm -f "$install_root/$name"
done
rm -rf "$install_root/portable_plugin"
rm -f "$install_root/uninstall"
rmdir "$install_root" 2>/dev/null || true
