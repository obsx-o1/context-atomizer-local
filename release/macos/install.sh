#!/bin/sh
set -eu

bundle_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_root=${ATOMIZER_MACOS_INSTALL_ROOT:-"$HOME/Library/Application Support/Context Atomizer/runtime"}
state_root="$HOME/Library/Application Support/Context Atomizer"

umask 077
mkdir -p "$install_root"
for name in atomizer-local-runtime atomizer-local-manager atomizer-local-open-library atomizer-codex-hook atomizer-claude-hook atomizer-local-mcp; do
    test -f "$bundle_root/bin/$name"
    cp "$bundle_root/bin/$name" "$install_root/.$name.tmp"
    chmod 700 "$install_root/.$name.tmp"
    mv "$install_root/.$name.tmp" "$install_root/$name"
done
test -f "$bundle_root/portable_plugin/plugin.json"
rm -rf "$install_root/.portable_plugin.tmp"
cp -R "$bundle_root/portable_plugin" "$install_root/.portable_plugin.tmp"
rm -rf "$install_root/portable_plugin"
mv "$install_root/.portable_plugin.tmp" "$install_root/portable_plugin"
cp "$bundle_root/bin/runtime-build-identity.json" "$install_root/.runtime-build-identity.json.tmp"
chmod 600 "$install_root/.runtime-build-identity.json.tmp"
mv "$install_root/.runtime-build-identity.json.tmp" "$install_root/runtime-build-identity.json"
cp "$bundle_root/uninstall.sh" "$install_root/.uninstall.tmp"
chmod 700 "$install_root/.uninstall.tmp"
mv "$install_root/.uninstall.tmp" "$install_root/uninstall"

if test -f "$state_root/runtime.json"; then
    "$install_root/atomizer-local-manager" update
else
    "$install_root/atomizer-local-manager" install
fi
