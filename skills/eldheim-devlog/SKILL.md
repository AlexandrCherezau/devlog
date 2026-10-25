---
name: eldheim-devlog
description: Record completed Eldheim changes in the configured public GitHub devlog after implementation, meaningful verification or deployment. Use for Eldheim code, plugins and server configuration work; skip read-only questions and unrelated projects.
---

After a task produces meaningful changes under the configured project root, add
one concise factual record before the final reply. The configured owner has
authorized automatic publication of these curated records to their devlog.
This does not grant deployment permission or permission to publish other data.

Use the installed collector; it never commits the project's working tree:

```bash
python3 ~/.agents/skills/eldheim-devlog/scripts/record_update.py add \
  --project guild --title 'Кланы используют общий уровень' \
  --change 'Проверки создания и вступления читают общий уровень персонажа' \
  --status deployed-prod \
  --verification 'Показатели кланов сверены с API на подключённых игроках'
```

Choose the narrowest true status:

- `implemented`: code/config changed; meaningful checks have not passed yet.
- `tested`: relevant checks passed, but the change has not been deployed.
- `deployed-test`: installed and observed starting on the isolated test server.
- `deployed-prod`: installed and verified on the production server.

Describe resulting player/admin behavior, the component and practical limits in
Russian. Include exact checks through repeated `--verification`; use `--note`
for missing client acceptance or an unfinished part. Do not call a build a
gameplay check, or a deployed change fully accepted without evidence. For mixed
deployment scopes, create separate records with their own statuses. Use
`--source-revision` only when the referenced Git commit actually contains the work.

Publish only a curated summary. Do not copy conversations, raw logs, credentials,
IP addresses, local filesystem paths, account/player identifiers or private
customer data. A built-in filter catches common secrets and private paths; it
does not replace your review. Public source names and generic gameplay facts
are sufficient.

Identical facts are deduplicated. A later deployment or a materially different
fix deserves its own record. `--id` may provide a stable task identifier; never
reuse it with different facts. Independent agents can record disjoint tasks;
the lead agent records the integrated outcome without duplicating identical work.

The collector attempts immediate delivery. If offline, it retains the record
for the installed timer. Report the actual result: `Published` means the source
record reached GitHub; the `updates` publication is a separate Actions job.
`Queued` alone means pending delivery, not published. Do not block unrelated
task completion on repeated network retries.

Read queue health with:

```bash
python3 ~/.agents/skills/eldheim-devlog/scripts/record_update.py status
```
