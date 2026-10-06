# Audit query record

Your working notes for Task 4.9, one section per Step. Nothing assesses what you write here:
the answers `poe verify` checks are in `submission.yaml`. Keep the notes anyway; they are the
record behind your answers, and the pull request description draws on them.

Use the measurement exception named in `README.md` for every command.

## Step 1 - Measure the slow query and read its plan

- When you loaded the history, and the event count `poe audit-seed-large` printed:
- The `poe audit-benchmark` output before any change, with the time you ran it:

Paste the plan `poe audit-explain` printed, and mark the step that reads far more rows than
the query returns. Note its rows, its loops and, where it filtered rows, its
`Rows Removed by Filter`, and how many rows the query returned.

```text
(paste the plan here)
```

## Step 2 - Make one change and confirm the plan uses it

- What the query filters on and what it orders by:
- The indexes `poe audit-explain` listed, and whether any of them matches the filter and the
  order:
- Your change (index or rewrite), and why it gives the database a path to one exception's
  events:
- For an index: the revision `poe migrate` named:
- The `poe audit-trail-compare` result:

Paste the new plan next to the old one, and mark the step that now reads the audit table.

```text
(paste the new plan here)
```

## Step 3 - Measure the difference and name the cost

- The `poe audit-benchmark` output after the change, with the time you ran it:
- Both medians and the ratio between them:
- For an index: its size from `poe audit-index-size`, what every audit write now also does,
  and whether applying your migration blocks writes to the audit table while the index
  builds:
- For a rewrite: what the new query form assumes about the data:
- What these numbers do and do not show about a production database:
