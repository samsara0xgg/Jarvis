---
name: Agents — Long Exposure B01
description: Scoped visual reference for the Agents inline-expansion presentation.
colors:
  desk-top: "#10122a"
  desk-bottom: "#08091a"
  sky-top: "#03040b"
  sky-bottom: "#070814"
  accent: "rgb(157 180 255)"
  ink: "#EEF0FB"
  muted: "rgb(196 204 238 / .72)"
  faint: "rgb(196 204 238 / .62)"
  line: "rgb(157 180 255 / .11)"
  waiting: "#FFC98F"
  working: "#6C9CFF"
  packing: "rgb(187 148 255)"
  done: "#6FE0B4"
  error: "#FF6A5A"
typography:
  title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif'
    fontSize: "15px"
    fontWeight: 600
    lineHeight: "20px"
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif'
    fontSize: "14px"
    lineHeight: 1.7
  inline-words:
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif'
    fontSize: "13px"
    lineHeight: 1.6
  label:
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif'
    fontSize: "11px"
  time:
    fontFamily: '"IBM Plex Mono", ui-monospace, Menlo, monospace'
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.3
rounded:
  control: "6px"
  compact: "10px"
  decision: "16px"
  composer: "20px"
spacing:
  trail-row: "27px"
  conversation-inset: "24px"
components:
  horizon:
    height: "56px"
  session-name:
    height: "22px"
    rounded: "{rounded.control}"
  inline-words:
    width: "480px"
    typography: "{typography.inline-words}"
  decision-card:
    width: "560px"
    rounded: "{rounded.decision}"
    padding: "15px 16px 12px"
  conversation:
    width: "680px"
    typography: "{typography.body}"
  composer:
    rounded: "{rounded.composer}"
    padding: "10px 10px 8px 14px"
---

# Agents — Long Exposure B01

## Overview

This reference applies only to the Agents Long Exposure presentation. The
visual authority is the selected **B01 · 方案二 · 行里展开** in the
[published artifact](https://claude.ai/artifact/SuuzwKu6bATLjUSHe6JqJC), retained
as [the local snapshot](agents-long-exposure.html). The implementation lives
in `desktop/resonance/src/agents/exposure/`.

The window reads as a night sky above a conversation. Session lights unfold
into time trails; the selected turn makes space inside its trail while the
right-hand names stay still. This is a scoped extension of the existing
Agents interface, not a design system for all Jarvis surfaces. The original
presentation and live conversation renderer remain available.

[ADR 0081](../adr/0081-agents-attention-through-long-exposure.md) owns the
architectural decision. [The specification](../spec.html) owns interaction,
data provenance, approval, and attention-ownership requirements. This document
records visual application only.

## Colors

The primary accent is pale blue against the desk's navy gradient. The expanded
sky is darker than the conversation below it. Low-opacity violet and blue
radial light at the desk's bottom corners preserves the reference's depth.

The waiting, working, packing, done, and error colors distinguish session
states and their trails. Read sessions recede into the neutral text color.
Status words and distinct glyphs accompany color. Warm light marks pending
decisions; the window accent does not follow the character's changing light.

Ink carries conversation and selected labels; muted and faint carry metadata
at the values above.

## Typography

System UI text keeps the Chinese-first interface compact. The time stack is
monospaced and uses tabular numerals. The named font stacks include fallbacks;
this reference does not imply that an external font asset is loaded.

The title, conversation, inline words, and metadata use the frontmatter roles.
Session names use medium-weight UI text and become semibold when active.
Time sits in a narrow column beside the inline words; the user's words retain
their own bubble and the agent's last paragraph reads beneath them.

## Layout

All dimensions are CSS pixels. Component widths above are preferred or
maximum widths, not minimum sizes. The horizon remains at the top; expanding
the sky overlays and dims the conversation without moving its reading position.

The time axis begins at 30px and reaches “now” at the window width minus 262px.
Trail rows follow the `trail-row` rhythm. The last 110px before “now” stay
straight; expansion bends only the portion of lower trails beside the words,
then returns them to the fixed name rows. Words stop before the names and
connect back to a nearby point with a fine line.

Inline words are capped by the available width as well as their nominal
maximum. A tall sky scrolls its trails and text together. At widths up to
900px, header and inline-word spacing tighten. Decision cards fit within a
20px viewport margin and scroll when their content exceeds the available
height. Preserve these bounded desktop adaptations without turning this
surface into a different mobile composition.

## Elevation & Depth

Depth comes from the darker sky, dimmed conversation, half-pixel inset edges,
and restrained emitted light. The inline expansion is a shallow blue wash
inside the timeline. It has no floating card shell. The decision card is the
raised surface: a dark violet gradient with a warm inset edge and diffuse
glow over a dimmed desk. The composer stays dark and translucent, gaining a
blue edge and quiet outer focus ring when active.

## Shapes

Use the compact and control radii for small controls and supporting surfaces;
the decision card and composer use their dedicated radii. User bubbles keep
an asymmetric lower-right corner. Session lights and the character remain
canvas-drawn forms with distinct state silhouettes. The Electron surface
fills its window; the rounded prototype window is not a second frame inside it.

## Components

The horizon is sparse: lights, the current title, the expansion control, and
the character. Expanded session names remain anchored while the timeline
opens around the selected words. The same live conversation and composer
occupy the desk in either presentation.

Motion preserves this spatial relationship. Springs use frequency in hertz
and a damping ratio, integrated by the shared `motion.ts` helper:

| Part | Frequency | Damping |
| --- | ---: | ---: |
| Sky opening | 2.2 | 0.92 |
| Inline gap and displaced trail segments | 2.6 | 0.9 |
| Inline words, horizontal and vertical | 2.8 | 0.86 |
| Row focus | 2.4 | 1 |
| Time needle, horizontal | 2.8 | 0.82 |
| Time needle, vertical | 2.8 | 0.86 |
| Character light, surfacing / receding | 1.5 / 0.7 | 1 |
| Character scale | 2.1 | 0.45 |

Small control transitions use 120ms or 240ms. Inline words fade in over 260ms
on first opening and 180ms between turns. The pause offer uses a 380ms spring
easing; the decision card travels from the character over 460ms using the
same easing. Preserve the easing samples in source rather than approximating
them with a generic bounce. Reduced motion snaps spring values to their
targets and disables CSS and element animations.

## Do's and Don'ts

- **Do** compare this surface with the pinned B01 inline-expansion reference,
  including motion and the fixed-name geometry.
- **Do** preserve the live renderer, focus visibility, reduced-motion behavior,
  and readable content when the sky or decision card exceeds the window.
- **Do** keep these tokens scoped to this presentation.
- **Don't** replace the inline expansion with the artifact's floating preview
  variant or import its prototype control bar into the app.
- **Don't** treat prototype demonstration content as session history or copy
  this palette into the original mode and unrelated Jarvis surfaces.
