"""Tools — organised by caller principal, not by functional domain.

Layout rule: subdirectory name == default caller_principal for tools inside.
  observer/  → read-only observations (git, file, clipboard, screen)
  worker/    → worker_agent scope (spawn, submit_report, runners)
  verify/    → jarvis_llm verification tools (test runners, postcondition checks)
  write/     → worker scope writes (write_file, git_commit)
  system/    → system_maintenance scope (projection rebuild, backup)

Do NOT create tools/smart_home/ or tools/git/ — that mixes principals.
"""
