# Development

## Architecture

Host adapters normalize capture events. Repository owners persist authoritative local data. A bounded maintenance worker composes existing deterministic repositories to refresh derived retrieval state. Retrieval and policy layers remain local and side-effect bounded.

Schema changes live in `src/atomizer_local_client/history/migrations`. Migration identifiers are sequential and must never be reused.

## Dependency governance

No paid, revenue-triggered, commercial-use, usage-based, or otherwise restrictive build/runtime dependency may be introduced without explicit user approval. Permissively licensed dependencies are allowed.

NSIS 3.12 is an approved permissively licensed Windows build dependency. The installer uses its built-in zlib compressor and no third-party plugins.

## Required checks

```powershell
python tools/repository_audit.py .
python -B -m unittest discover -s tests -v
node --test browser_extension/tests/*.test.js
python -m compileall -q src tests tools release browser_extension/package_extension.py
python -m build --wheel
```

Also build the Chromium package, runtime executables, and Windows installer. Installer lifecycle validation must use disposable state and must not target a developer's live profile.

Technical-preview Windows installers may be unsigned when SHA-256 checksums are published. Broad commercial Windows releases require Authenticode signing.

The public release manifest is a curated publication artifact derived from release validation and is not intended to reproduce internal CI receipt output.

Before publishing, confirm the repository contains no database, logs, credentials, build outputs, absolute developer paths, internal QA fixtures, or local machine state.
