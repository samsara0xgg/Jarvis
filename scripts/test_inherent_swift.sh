#!/bin/bash
# XCTest baseline without launching the production card/controller.
# Hosted UI, accessibility and device/live tests are separate acceptance gates.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
swift_root="$repo_root/desktop/inherent-swift"
test_build_dir="${INHERENT_TEST_BUILD_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/jarvis-inherent-tests.XXXXXX")}"
mkdir -p "$test_build_dir"
test_build_dir="$(cd "$test_build_dir" && pwd)"

cd "$swift_root"
xcodegen generate > "$test_build_dir/xcodegen.log" 2>&1
if ! xcodebuild build-for-testing \
  -project InherentCard.xcodeproj -scheme InherentCard \
  -destination 'platform=macOS' -derivedDataPath "$test_build_dir" \
  PRODUCT_BUNDLE_IDENTIFIER=com.allen.jarvis.acceptance.baseline \
  INFOPLIST_FILE= GENERATE_INFOPLIST_FILE=YES \
  > "$test_build_dir/build.log" 2>&1; then
  tail -60 "$test_build_dir/build.log"
  exit 1
fi

test_app="$test_build_dir/Build/Products/Debug/InherentCard.app"
xctest_bin="$(xcrun --find xctest)"
# Call the tool directly: /usr/bin/xcrun strips DYLD_* before exec.
# Loading the compiled app dylib resolves test symbols without calling @main.
if ! DYLD_LIBRARY_PATH="$test_app/Contents/MacOS" "$xctest_bin" \
  "$test_app/Contents/PlugIns/InherentCardTests.xctest" \
  > "$test_build_dir/xctest.log" 2>&1; then
  tail -80 "$test_build_dir/xctest.log"
  exit 1
fi
tail -5 "$test_build_dir/xctest.log"
printf 'XCTest evidence: %s\n' "$test_build_dir"
