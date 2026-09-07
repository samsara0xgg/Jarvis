---
paths:
  - "**/*.swift"
---

# Swift verification

- Swift tests for the repository-owned Inherent app live in
  `desktop/inherent-swift/InherentCardTests` and run with
  `bash scripts/test_inherent_swift.sh` (xcodegen + xcodebuild
  build-for-testing + xctest, no production `@main` launch).
- Protocol, reducer, and UI-state tests there are the explicit exception
  to the no-unit-test policy; they exist for strict concurrency,
  reconnect, and UI-state verification.
- `launcher.py` rebuilds when the `.app` is older than any source it
  compiles. A card launched by any other means (a direct exec of the
  binary) still needs a manual rebuild.
