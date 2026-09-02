# Development

## Architecture

Host adapters normalize capture events. Repository owners persist authoritative local data. A bounded maintenance worker composes existing deterministic repositories to refresh derived retrieval state. Retrieval and policy layers remain local and side-effect bounded.

Managed access is additive: persisted policy, in-memory authority/turn exchange, a closed privileged router over `LibraryQueryService`, thin loopback wiring, and native hook formatting are separate modules. MCP always remains the direct-frontier caller. Do not add privilege parameters or duplicate lexical, vector, RRF, reranking, or vendor-specific retrieval.

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
python -m atomizer_local_client.evaluation.quality_benchmark
```

Also build the Chromium package, runtime executables, and Windows installer. Installer lifecycle validation must use disposable state and must not target a developer's live profile.

The macOS jobs build runtime executables and `release/build_macos.py` artifacts
natively on both explicit runner architectures. macOS validation must set
`ATOMIZER_MACOS_KEYCHAIN` to a temporary keychain created for that CI job; it
must never use a developer's normal Keychain. LaunchAgent tests must use
temporary paths rather than the live user registration.

Technical-preview Windows installers may be unsigned when SHA-256 checksums are published. Broad commercial Windows releases require Authenticode signing.

The public release manifest is a curated publication artifact derived from release validation and is not intended to reproduce internal CI receipt output.

The deterministic quality benchmark builds a disposable two-project Library through canonical ingestion and elected-document operations. It reports Recall@8, MRR, stale/superseded rate, contradiction coverage, project bleed, provenance, latency, result count, and bounded character/token estimates. It does not call a model or change retrieval algorithms.

Before publishing, confirm the repository contains no database, logs, credentials, build outputs, absolute developer paths, internal QA fixtures, or local machine state.
