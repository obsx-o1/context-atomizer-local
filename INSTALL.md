# Install and run

This development snapshot targets Windows 10 or later on x64-compatible systems. Generated installers are unsigned development artifacts.

## From source

Use Python 3.11 or later in an isolated environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r release\requirements-build.txt .
.\.venv\Scripts\atomizer-local-setup.exe
.\.venv\Scripts\atomizer-local-open-library.exe
```

The setup flow creates local runtime state under the current user profile. Codex integration is optional and explicit. The browser extension is loaded unpacked from a package produced by `release/build_browser.py`.

After loading the extension, open Library through the Start Menu shortcut, select **Create one-time pairing code**, and paste that code into the extension options page. Pairing normally occurs only on first setup, after explicit revocation, after an extension-profile reset, or after credential recovery. Ordinary capture then authenticates automatically; the paired secret is not rotated for every message.

## Build artifacts

```powershell
python release\build_browser.py --output "$env:TEMP\ContextAtomizer-Chromium-v0.1.0-dev0.zip"
python release\build_runtime.py --output "$env:TEMP\context-atomizer-runtime"
python release\build_windows.py --runtime "$env:TEMP\context-atomizer-runtime" --output "$env:TEMP\context-atomizer-artifacts" --compiler "$env:TEMP\nsis-3.12\makensis.exe"
```

NSIS 3.12 is the approved permissively licensed Windows installer builder. CI downloads the official portable archive, verifies its pinned SHA-256, and uses `SetCompressor zlib`; no global installation or third-party plugin is required. Validate installers only against disposable profile, registry, and state locations; the repository CI performs that lifecycle on a disposable runner.
