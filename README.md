# Context Atomizer Local

Context Atomizer Local is a Windows-first, local-only capture and retrieval client. It stores supported chat history and user-elected documents in a SQLite Library, builds retrieval state locally, and exposes a loopback-only Library UI.

This repository is a development snapshot. It is not a hosted service, browser-store release, or signed production installer.

## What is included

- ChatGPT Web capture through an unpacked Chromium extension.
- Explicit Codex hook integration.
- User-elected local document indexing.
- Lexical search and local semantic, entity, claim, temporal, contradiction, and verification state.
- Automatic bounded enrichment while the Library runtime is active.
- Authenticated Library sessions and explicitly paired browser capture.
- Windows runtime, installer, and validation tooling.

All capture, indexing, and retrieval in this repository remain on the local machine. The runtime binds to loopback and has no remote synchronization path.

## Development validation

```powershell
python -m pip install -r release/requirements-build.txt .
python tools/repository_audit.py .
python -B -m unittest discover -s tests -v
node --test browser_extension/tests/*.test.js
python -m build --wheel
```

See [INSTALL.md](INSTALL.md), [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md), [SUPPORTED_CLIENTS.md](SUPPORTED_CLIENTS.md), and [DEVELOPMENT.md](DEVELOPMENT.md).

## License

Context Atomizer Local is licensed under the Mozilla Public License 2.0 (MPL-2.0). See [LICENSE](LICENSE).

The MPL-2.0 license applies only to software contained in this repository. Separate Context Atomizer services and software not included in this repository are not licensed by this repository.
