# ADR 0353 — Her voice is set for each device, and a change says how long it lasts

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0174, one shared holder of `current` and `default` for every device, and `current` returning to `default` when any conversation mode ends

## Context

- Her volume and speed live in one holder for the whole brain (ADR 0174). Every synthesis reads it,
  whichever device will play the answer: a Mac, a terminal (ADR 0172) or a phone (ADR 0209).
  「大声点」 said to the phone also makes her louder on the Mac.
- The loudness she needs differs by device: a speaker across a desk, a phone at arm's length or in a
  pocket. Each device plays her at its own system volume, mixed with its other sound.
- A change he did not ask to keep returns to the default when conversation mode ends. The result
  carries `remembered` and `reset`; only the tool's description says the change ends with the
  conversation. By the audit of 2026-10-20 she had called `set_voice` 17 times.
- A turn's answer plays on the device the turn came from (ADR 0172, ADR 0209).
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): one setting per device, and
  a result that says how long the change lasts.

## Decision

Keep her volume and speed for each device that plays her, change only the device the turn came
from, and say in the result how long the change lasts.

- **Each device its own.** Every device that plays her voice has its own current and default, and
  whatever is synthesized for a device uses that device's. A device with no default of its own
  starts from the one default saved before this decision, or else the factory voice. Steps, bounds
  and the ceiling stay as ADR 0174 sets them.
- **The turn's device.** `set_voice` changes the device whose turn asked. A typed turn changes the
  device he typed on.
- **How long.** The result says whether the change lasts until this conversation ends or, when he
  asked to keep it, from now on on that device.
- **Its end.** A device's conversation ending returns that device, and no other, to its default.
  `reset` returns the turn's device to the factory voice and forgets its default.

## Alternatives rejected

- **Leave loudness to each device's own volume control.** The system volume also moves his music,
  calls and videos, while she is too quiet only next to them. Her level inside the mix is set where
  she is synthesized.
- **Keep one voice and say in the result that every device changed.** He asked the device in his
  hand. The Mac would still speak louder in a room he may have left.

## Consequences

- A voice he keeps on one device does not follow him to another; he sets each once.
- The saved voice becomes one entry per device, and unpairing a device has one more thing to
  forget.
