---
name: 字幕翻译
description: A standalone native Mac utility for reading live English captions in Chinese.
typography:
  translation:
    fontFamily: "macOS system"
    fontSize: "23pt"
    fontWeight: 500
  empty-state-title:
    fontFamily: "macOS system"
    fontSize: "24pt"
    fontWeight: 600
  english-source:
    fontFamily: "macOS system"
    fontSize: "14pt"
rounded:
  notice: "10pt"
spacing:
  compact: "8pt"
  control-group: "12pt"
  section: "16pt"
  content: "24pt"
components:
  caption-window:
    width: "580pt"
    height: "470pt"
  crop-sheet:
    width: "760pt"
    height: "560pt"
    padding: "{spacing.content}"
  recovery-notice:
    rounded: "{rounded.notice}"
    padding: "14pt"
---

# Design System: 字幕翻译

## Overview

A simple native Mac utility with Chinese captions as the main reading surface. SwiftUI controls, AppKit window behavior, and system typography provide the visual language. The implementation uses no custom branding, fonts, or decorative media.

This document records the current `App.swift` and `RegionView.swift` implementation. Dimensions are macOS points; semantic text styles and platform controls retain system metrics.

## Colors

The window background, primary text, separators, control surfaces, and prominent-button accent use the platform appearance. Supporting copy uses SwiftUI `.secondary`. The activity indicator uses `Color.green` when running and `Color.secondary` otherwise; adjacent status text also communicates state. Recovery notices use `Color.orange.opacity(0.10)`.

The crop preview uses black at 5% opacity behind the image, black at 48% outside the selection, a white selection stroke, and `Color.accentColor.opacity(0.08)` within it. These overlays support image inspection rather than establish a brand palette. Native semantic colors are deliberately not replaced with fixed CSS color values in the frontmatter.

## Typography

All text uses the macOS system font and its system fallback for Chinese. The frontmatter records the initial explicit font sizes. Chinese translation is medium weight, adjustable from 16–36 points in two-point steps, with 9 points of additional line spacing. Optional English appears below it, in secondary foreground with 5 points of additional line spacing.

Status, source labels, and recovery detail use `.callout`; auxiliary footer text uses `.caption`; empty-state supporting text uses `.body`; the recovery heading uses `.headline`; and the crop title uses `.title2` with semibold weight. Keep those semantic styles instead of fixing their platform-resolved metrics. Translation, English source, and error detail support text selection.

## Layout

The resizable caption window uses the frontmatter's default size with a minimum size of 460 × 380 points. A status row sits above a left-aligned scrolling reading area; a divider separates the fixed bottom controls. Main content has the `content` horizontal inset. Chinese comes first, with optional English beneath it. Translation and source blocks have the `content` vertical separation.

The footer places the source name above the action row, then local-processing status below. Source names stay on one line and truncate in the middle. Controls have large native sizing. English display and floating mode are initially enabled.

The crop sheet uses the fixed dimensions and inset in the frontmatter. Its preview preserves the captured image's aspect ratio and centers it within the available area, with a minimum preview height of 250 points. Explanatory text sits above; whole-window selection, cancel, and confirmation actions sit below.

## Elevation & Depth

Depth comes from the native window and sheet presentation, a divider, and the subtle recovery-notice fill. There are no custom shadows, material effects, or authored animations. Floating mode switches the AppKit window between `.floating` and `.normal`; the window supports all Spaces and full-screen auxiliary presentation and can be dragged by its background.

## Shapes

Standard buttons, menus, and toggles retain native shapes and focus treatments. The recovery notice uses the rounded shape recorded in the frontmatter. The running-state indicator is a six-point circle. Crop selection stays rectangular with a two-point white outline.

## Components

- **Window controls:** choose or replace a source with a standard button; start/pause uses `.borderedProminent`; recropping uses a link-style button. Busy and incomplete-source states disable the relevant actions.
- **Display controls:** a button-style pin toggle controls floating mode. A borderless ellipsis menu offers English visibility, type size, and copying the translation. Icon controls have Chinese accessibility labels and help text.
- **Reading area:** the empty state explains source selection and local language preparation. Once available, recent Chinese translation occupies the primary text area; optional English remains visually secondary.
- **Recovery notice:** a headline and explanatory text accompany the applicable recovery action: recrop, prepare local translation, or select another window.
- **Crop sheet:** dragging creates a normalized, image-bounded rectangle. Whole-window selection provides an alternative to dragging. Confirming begins translation; confirmation is disabled if either selected dimension is below 3% of the image. Escape cancels and Return confirms through native keyboard shortcuts.
- **Menu commands:** Command-O selects a window, Command-R starts or pauses, and Command-Shift-P toggles floating mode.

## Do's and Don'ts

- **Do** preserve Chinese-first reading order, selectable text, system appearance, and native control behavior.
- **Do** show status and recovery instructions in words as well as using visual state indicators.
- **Do** keep the crop image undistorted and the selected region visible.
- **Don't** invent fixed replacements for native semantic colors or custom fonts for this utility.
- **Don't** add decorative cards, branding, or motion absent from the approved native direction.
