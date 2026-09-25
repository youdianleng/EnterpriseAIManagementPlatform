# Issue tracker

**Where issues live:** local markdown files under `.scratch/<feature-slug>/issues/`.

This repo is a solo private project. There is no GitHub Issues workflow in use, and no `gh` CLI dependency.

## Layout

```
.scratch/
  <feature-slug>/
    issues/
      01-<slug>.md
      02-<slug>.md
      ...
```

- One ticket per file. **Never** a single combined file.
- Files are numbered from `01` in **dependency order** — blockers first.
- The number in the filename is the ticket's identity. Other tickets reference it as `NN`.

## Reading rules (for skills)

1. List `.scratch/<feature-slug>/issues/` and read every file before planning work.
2. A ticket's **Blocked by** line lists the ticket numbers that must complete first. `None — can start immediately` means it is on the frontier.
3. The **frontier** is every ticket whose blockers are all done. Always work the frontier, lowest number first.
4. `Status: ready-for-agent` means an agent may pick it up without human triage.
5. Ticket bodies intentionally avoid file paths and code snippets — they go stale. Decision-rich shapes (state machines, schemas, type shapes) may be inlined when prose would be ambiguous.
6. When a ticket is completed, do not delete it. Flip `Status:` to `done` and leave the file in place as a record.

## Writing rules (for skills)

- Apply the `to-tickets` template exactly: `# <NN> — <title>`, then **What to build**, **Blocked by**, **Status**, then `- [ ]` acceptance criteria.
- Publish blockers first so blocking edges can reference real numbers.
- Do **not** close or modify a parent issue or the design spec (`docs/DESIGN.md`) from a ticket.

## Source of truth

`docs/DESIGN.md` is the implementation baseline. If a ticket and the design doc disagree, **the design doc wins** — fix the ticket.
