# Verification Log

This file records manual / human-loop verifications for commits that have not
been automatically tested. The `post-commit` hook appends entries here when a
commit body contains a `Verify:` trailer.

Format per entry:

```
## <commit-sha-short> — <yyyy-mm-dd> — <subject>
<verification body>
```

(Empty — first entry will be appended automatically.)
