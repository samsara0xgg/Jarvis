# 字幕翻译

<!-- impeccable:product-schema 1 -->

## Platform
Native macOS desktop.

## Users and purpose
Allen wants to read live English captions in Chinese while watching an existing window. The supplied recording shows a scrolling English transcript with an unfinished sentence continually revised and extended.

## Approved scope
A standalone Mac utility: choose one window, mark its caption region, start/pause recognition, and read recent Chinese sentences in a draggable floating window. The user approved this scope with “go”.

## Implementation assumptions
SwiftUI and AppKit; ScreenCaptureKit for user-selected window capture; Vision for English OCR; Apple's on-device Translation framework. English to Simplified Chinese is the first version's fixed language pair. Requires macOS 26 or newer. The local translation choice and platform floor are implementation decisions, not additional user requirements.

## Constraints
Keep the tool independent of the Jarvis runtime. No screenshot uploads, API credentials, audio recording, or transcript persistence. System language downloads may require a first-use confirmation. Recognition and translation performance must be measured rather than promised.
