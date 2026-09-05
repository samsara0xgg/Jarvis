# Inherent Swift upstream baseline

Imported 2026-09-04 from the clean tracked subtree in
`/Users/alllllenshi/Projects/jarvis-codex`.

- Repository commit: `65c39c79c35d30e75b777284696cb824ca3845fa`
- Subtree: `66e248c88873313f28760ee15820048817b0d6f8`
- Last subtree edit: `5fa923c242fff587f532553cf829adf0443214da`
- Import: `git archive` of the exact commit; no sibling uncommitted files,
  generated project, binary, build cache or unused OutputSpeechPlayer included.
- Toolchain: macOS 26.6 (25G70), Xcode 26.6 (17F113), Swift 6.3.3 arm64,
  XcodeGen 2.45.3.

## Pre-protocol verification

Clean archive: `/tmp/jarvis-inherent-baseline-65c39c7`.
`xcodegen generate` succeeded. The archived sources compiled successfully.
The built XCTest bundle passed **82 tests, zero failures**, in 10.943 seconds,
without entering the app's production controller entrypoint:

```sh
DYLD_LIBRARY_PATH=<archive>/desktop/inherent-swift/build-isolated/Build/Products/Debug/InherentCard.app/Contents/MacOS \
  /Applications/Xcode.app/Contents/Developer/usr/bin/xctest \
  <archive>/desktop/inherent-swift/build-isolated/Build/Products/Debug/InherentCard.app/Contents/PlugIns/InherentCardTests.xctest
```

Evidence: `/tmp/jarvis-inherent-baseline-65c39c7/direct-xctest-r2.log`.
This is the existing XCTest behavioral baseline, not UI or live v2 acceptance.

Two `xcodebuild test` launch attempts compiled the sources but never started
running tests; they were explicitly interrupted. The first used an isolated
PRODUCT_BUNDLE_IDENTIFIER that disagreed with the fixed Info.plist value.
The second added `INFOPLIST_FILE= GENERATE_INFOPLIST_FILE=YES` and fresh
`build-isolated` derived data; launch still stalled. Both were run under a
sandbox-exec rule denying outbound localhost:8006 so the old app host could
not intentionally attach to the production surface. The host-launch cause is
not established. Logs: `xcodebuild.log`, `xcodebuild-isolated.log`; result
bundles are interrupted/incomplete and must not be reported as passes.
A direct XCTest attempt first lacked the app dylib search path; the successful
command above supplies it. Retain these failures in the migration evidence.

The repository runner `bash scripts/test_inherent_swift.sh` reproduces the
headless path. The imported source plus fixture normalization passed 82 tests
with zero failures in 10.965 seconds; local evidence is
`desktop/inherent-swift/build/xctest.log`. Its acceptance-only generated app
bundle/Info.plist is not a release build and must not be deployed.

## Behavior disposition

`jarvis-cc` has the same tracked subtree. The dirty `jarvis-legacy` copy is
historical evidence only and was not imported as the baseline. Its committed
Swift subtree is byte-identical to this baseline apart from the excluded
`OutputSpeechPlayer.swift`, so its uncommitted resize work (dated 2026-08-26)
was migrated by copying those six files on top of the import: left-edge
resize with a fixed right edge, 300–900-point/screen-bounded width,
UserDefaults persistence (`InherentCardWidth`), double-click reset to 360,
and width-aware hit-testing, drag regions and history popover offset.
`DisplayMathTests.test_clampWidth` moved its floor from 360 to `MIN_WIDTH`
300 with that change.

Also migrated from the `jarvis-legacy` 2026-05-11 stash, by Allen's
decision: the auto-growing input field (`NativeInputTextSizing`, 36–164
points, wrap-aware drag policy), the selectable NSTextView answer renderer
with auto-copy on selection (`NativeSelectableMarkdownText`), per-line code
block metrics, and the attributed history chip. The same stash's
`OutputSpeechPlayer` TTS wiring (bridge op, controller player, dispatcher
stub and its test) was left out: ADR-0014 keeps Python the sole response
speech owner.

Existing hotkey, top-right anchoring/display changes, passthrough, dragging,
history, image input, voice recording and shutdown/watchdog behavior remain
baseline contracts. V2 migration must preserve them and replace anonymous
v1 streaming with keyed state. The old launcher still trusts an existing
binary and only uses YAML mtime for project regeneration; its invalidation
repair and isolated test-host behavior remain pending.

The v2 realtime core lives in the new `InherentRealtime` library target, and
the baseline `InherentCard` target, its settings, and v1 files are unchanged,
except that card 1's relocated `RealtimeProtocol.swift` is the only file that moved.

## Import normalization

`NativeCardModelTests.swift` spells the two trailing Markdown spaces in one
multiline fixture as `\u{20}\u{20}`. Its runtime string is unchanged; this keeps
the repository whitespace check meaningful. The manifest below remains the
original upstream hash, not the locally normalized file hash.

## Original imported SHA-256 manifest

These hashes describe the exact upstream bytes, before local migration edits.

```text
7860fcba7cb9fc0d3b19fb7f9e2844331ea04bb171868b8ff4b75f110e8fe8ba  .gitignore
408d81c357fb9c670ffa6f1673e43f33f3c8a063c21ecd89b9ba6ba77f716d05  InherentCard/BridgeBackend.swift
454d039b1fc4644af6e21e283d45f8654be726cc532ab17a35af02d08cd59e34  InherentCard/CardPanel.swift
7184c2357e2cbffec4cee26d12aa9e1f4f70565a8a7352fe83149744e623c6f2  InherentCard/DisplayManager.swift
e888bdb5c6b1c60539d2bbfd6b1fcd6114f989fe2749a541504e7ad11d15c8ad  InherentCard/FadeController.swift
0ef11e688f1362c81bec9576b9428339fdbdd9537fa2ae63b16fa4f27aa8d1f5  InherentCard/HotkeyManager.swift
3139b4fcd1c04428f88e12980b2a836a4b74d70ff8509b99ea5da2f995624443  InherentCard/Info.plist
cc87ccf7a1101a692fca93a4a65e13c8eb991e3e3861298007b54e4aa05b1eb4  InherentCard/InherentCardApp.swift
0d2cf5cde47f938693fdb49b5d5def9ef036ed5054539e2355926da636fa5f7f  InherentCard/NativeCardController.swift
77529c3bc11b0248a2a7f74a64116ac807e1dd3b7fc1776782c03f5fcd829a78  InherentCard/NativeCardModel.swift
ea4c53a6d6bdc05a436207769a486d773562a10d4e293288b1458342d4cc18da  InherentCard/NativeCardView.swift
561e4d09ddb5008e17cc6ad4e5c87e9c06eeb61c91d9e12970cd003bf18e04db  InherentCard/NativeVoiceRecorder.swift
e76903beabefa1ea4f0211dff3ddeba8282a1e6981e6ee855875b9c553d8386f  InherentCard/ParentWatchdog.swift
f24bf63faf635eaec3813c5a0336087eccc83689c64e35a789916fe5b7307642  InherentCard/SystemAudioDucker.swift
d7db2bd9e7225e8241b9316a5acea7477f4d3442c8fa73a03593b48292cf4eb5  InherentCardTests/BridgeDispatchTests.swift
3ad14788d9a5ed122516d902a7acb3b2d0a622663c088a05366be5c64aad69d7  InherentCardTests/DisplayManagerTests.swift
179db059d704863a8a5d72efd3c6c81fb7feddac52cf510910307c740e438e5f  InherentCardTests/DisplayMathTests.swift
a8766ae1c989fa4d57e284b422cb19e4eccf895a5f76eeed3906ce7202ea6543  InherentCardTests/FadeControllerTests.swift
7594ddc35d72424a1c1ff73ec4a46a21d9a4601f64e9035cc3035be542672a69  InherentCardTests/NativeCardModelTests.swift
8c03661a1b80b6fe7bbabb402be6360fcadd9ddbbe32e9a49928589ad3a6743a  InherentCardTests/ReconnectBackoffTests.swift
4708748f3e3ee297a88e8522218cbc54216f6238ce3e02fb84d7bacfb6f2a6dd  InherentCardTests/SubmitRequestTests.swift
b306da378708cd4b85c6efda2bc817f45885034e1e1dc016678df1668db97529  InherentCardTests/SystemAudioDuckerTests.swift
51d84cafa3518ee55ce882e40368424b3bb18096fe502a815bdd81f1079264fe  Project.yml
a5cdd2ad40663af545e50b34966e94ad65b1229199b718173852abe9280d189e  README.md
b2f015592bc0b949d12cb5a4f509ee6c1be42bc8f793cc615b90dc8861c19aad  launcher.py
```
