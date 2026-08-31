# Experimental macOS foundation

Context Atomizer Local has an experimental macOS user-runtime foundation for
Apple Silicon (`arm64`) and Intel (`x86_64`). Native GitHub-hosted CI validation
runs on both architectures, and merge eligibility requires both native lanes to
pass. It has not completed a human host smoke test, is not signed, and is not
notarized. This is not a claim of full macOS support. CI artifacts are uploaded
only after their native test and packaged-lifecycle steps pass.

The macOS layer changes only operating-system boundaries. Capture, Library,
SQLite, migrations, retrieval, pairing, HMAC/replay, and local security policy
remain the same implementation used on Windows.

## Current platform decisions

- Application state uses `~/Library/Application Support/Context Atomizer`.
- Small runtime credentials use the current user's macOS Keychain. Tests set
  `ATOMIZER_MACOS_KEYCHAIN` to an isolated temporary keychain and never access a
  developer's normal Keychain. Keychain ACLs trust only the current executable
  and the installed manager/runtime siblings so the two runtime processes can
  share their separated credentials without broadening access to other apps.
  The current process is represented by Keychain Services' native calling-tool
  identity; installed sibling executables remain explicit paths.
  Because development artifacts are unsigned, replacing an executable can
  change its code identity and make an existing ACL stale. That experimental
  upgrade case requires user-assisted credential repair; the runtime does not
  weaken or silently replace an existing ACL.
- Login startup uses one exactly owned user LaunchAgent at
  `~/Library/LaunchAgents/com.contextatomizer.local.runtime.plist`. It does not
  install a LaunchDaemon or system service and requires no root privileges.
- Library opening delegates to `/usr/bin/open` only after the existing runtime
  validates and returns a loopback Library capability URL.
- The existing non-Windows `fcntl.flock` single-instance boundary is used on
  macOS. Windows continues to use its existing `msvcrt` lock.
- Native artifacts are separate thin `arm64` and `x86_64` tar archives. They
  are unsigned development artifacts, not Universal binaries.

## Verified primary sources

These interfaces and labels were checked on 2026-08-31:

- [GitHub runner selection](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job):
  for public repositories, standard hosted `macos-15` is an M1 `arm64` runner
  and `macos-15-intel` is an Intel runner. CI uses those explicit stable labels
  and checks both `runner.arch` and `uname -m` before claiming native coverage.
- [GitHub runner images](https://github.com/actions/runner-images#available-images):
  the image inventory independently maps `macos-15` to the macOS 15 arm64
  image and `macos-15-intel` to the macOS 15 x64 image.
- [Apple Application Support directory](https://developer.apple.com/documentation/foundation/url/applicationsupportdirectory):
  a non-sandboxed macOS app's user Application Support directory is under
  `~/Library/Application Support`.
- [Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services):
  Keychain Services is the native encrypted store for small user secrets.
- [Apple `SecAccessCreate`](https://developer.apple.com/documentation/security/secaccesscreate%28_%3A_%3A_%3A%29):
  the trusted-application list controls which applications may access a
  sensitive Keychain item; a `nil` list trusts only the calling application.
- [Apple `SecTrustedApplicationCreateFromPath`](https://developer.apple.com/documentation/security/sectrustedapplicationcreatefrompath%28_%3A_%3A%29):
  a trusted-application object binds an ACL entry to the designated executable;
  the nullable path represents the application or tool making the call.
- [Apple `SecKeychainItemCreateFromContent`](https://developer.apple.com/documentation/security/1393225-seckeychainitemcreatefromcontent?changes=_3_1___9_2&language=objc):
  the initial access instance is installed atomically when the Keychain item is
  created. Credential rotation modifies only the item's payload and preserves
  that creation-time access policy.
- [Apple DTS Keychain ACL guidance](https://developer.apple.com/forums/thread/836816):
  setting access while creating an item avoids the authorization prompt that
  can accompany a later ACL change.
- [Apple Code Signing Guide](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/AboutCS/AboutCS.html):
  Keychain access recognizes updated applications through their code-signing
  designated requirement; unsigned development replacements do not have that
  stable signed identity.
- [Apple Launch Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html):
  per-user background processes use `launchd`; the user's `Library/LaunchAgents`
  directory is loaded at login, and `Label` plus `ProgramArguments` are the
  required job identity and command fields.
- [Apple Command Line Primer](https://developer.apple.com/library/archive/documentation/OpenSource/Conceptual/ShellScripting/CommandLInePrimer/CommandLine.html):
  the `open` command launches an application or opens a file with an
  application; the macOS adapter passes the already validated loopback URL as
  an argument vector to `/usr/bin/open`.
- [Python `fcntl`](https://docs.python.org/3/library/fcntl.html): the standard
  `fcntl` module exposes Unix file-control and locking operations on macOS.

## Development artifact

On a native macOS runner:

```sh
python release/build_runtime.py --output "$RUNNER_TEMP/context-atomizer-runtime"
python release/build_macos.py \
  --runtime "$RUNNER_TEMP/context-atomizer-runtime" \
  --output "$RUNNER_TEMP/context-atomizer-artifacts" \
  --architecture "$(uname -m)"
```

Extract the resulting archive and run `./ContextAtomizerLocal/install.sh` as
the intended desktop user. The script installs only into that user's
Application Support directory. `uninstall` removes the owned runtime files,
LaunchAgent, and credentials while preserving the Library database.
