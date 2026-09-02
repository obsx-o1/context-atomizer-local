# Install and run

This development snapshot targets Windows 10 or later on x64-compatible systems. It also contains an experimental macOS foundation for Apple Silicon and Intel. Baseline native CI validation exists, but this v0.2 branch has not yet completed its hosted CI run or a human host smoke. Generated installers and macOS archives are unsigned development artifacts.

## From source

Use Python 3.11 or later in an isolated environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r release\requirements-build.txt .
.\.venv\Scripts\atomizer-local-setup.exe
.\.venv\Scripts\atomizer-local-open-library.exe
```

The setup flow creates local runtime state under the current user profile. Codex integration is optional and explicit. The browser extension is loaded unpacked from a package produced by `release/build_browser.py`.

Claude Code capture is also optional and explicit. Standalone read-only memory uses the installed `atomizer-local-mcp` command through the portable plugin mappings. The Library UI controls direct, managed-exclusive, and disabled access. Managed-exclusive mode requires a compatible separately verified manager; selecting the mode alone grants no manager authority.

After loading the extension, open Library through the Start Menu shortcut, select **Create one-time pairing code**, and paste that code into the extension options page. Pairing normally occurs only on first setup, after explicit revocation, after an extension-profile reset, or after credential recovery. Ordinary capture then authenticates automatically; the paired secret is not rotated for every message.

## Build artifacts

```powershell
python release\build_browser.py --output "$env:TEMP\ContextAtomizer-Chromium-v0.2.0-dev0.zip"
python release\build_runtime.py --output "$env:TEMP\context-atomizer-runtime"
python release\build_windows.py --runtime "$env:TEMP\context-atomizer-runtime" --output "$env:TEMP\context-atomizer-artifacts" --compiler "$env:TEMP\nsis-3.12\makensis.exe"
```

NSIS 3.12 is the approved permissively licensed Windows installer builder. CI downloads the official portable archive, verifies its pinned SHA-256, and uses `SetCompressor zlib`; no global installation or third-party plugin is required. Validate installers only against disposable profile, registry, and state locations; the repository CI performs that lifecycle on a disposable runner.

## Experimental macOS artifact

Native GitHub-hosted macOS jobs produce separate thin `arm64` and `x86_64`
tar archives. After extraction, the user-level development install is:

```sh
./ContextAtomizerLocal/install.sh
```

No `sudo`, LaunchDaemon, signing, or notarization is used. State is stored
under `~/Library/Application Support/Context Atomizer`, credentials are stored
in the user's Keychain, and startup is registered through the user's
`~/Library/LaunchAgents` directory. Run the installed `uninstall` command to
remove owned runtime state while preserving the Library database. See
[MACOS.md](MACOS.md) for limitations and current platform evidence.
