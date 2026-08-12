---
name: always-true-guard-scan
description: distilled from run qb_inhouse_fix
---

# always-true guard scan

When reviewing boundary fixes, grep for guards of the form `len(x) >= 0` that are tautologically true; they silently disable the error path they were meant to protect.
