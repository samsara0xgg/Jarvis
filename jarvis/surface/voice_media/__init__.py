"""Persistent L5 media owner for generation-safe streaming speech output (ADR 0374).

The owner runs one asyncio loop for the daemon lifetime.  Surface watchers
submit committed response events into its bounded command queue; only this
actor may open TTS sessions, mutate playback generations, or own fallback
processes.  The PortAudio callback remains a lock-free consumer and reports
bounded software milestones back to the actor.

The actor is one class, ``pipeline.StreamingTTSPipeline``.  ``config``,
``contract``, ``registry``, ``response`` and ``pcm`` hold what it is
configured with, answers with and keeps; none of them reads an owner field.
"""
