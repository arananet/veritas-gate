# Making an artifact evaluable

Everything here was learned by running Veritas against a real paper and
watching the same findings come back round after round. None of it is about
writing a better paper. It is about not wasting judges — and days — on
questions that were never about the work.

## Cite a name that will not change

Evidence that is regenerated should be cited by a name that survives
regeneration.

A run that writes `evidence/browser-run-<random>/` gives every regeneration a
new name. The manuscript cites one of them. The next run breaks that citation,
an evaluation reports missing evidence, the reference is repaired to the new
name, and the run after that breaks it again. The loop cannot converge, because
each pass reintroduces what the last one fixed.

Keep the per-run directories — they are the record — and add a pointer that
does not move:

```bash
ln -sfn "browser-run-$SUFFIX" evidence/current-run
```

Cite `evidence/current-run/report.json` everywhere, and say which run is
canonical in one place, such as an evidence README, rather than in twenty
links. Veritas reads through the link, so the files beneath it are evaluated
under the stable name the documents cite. A link that points outside the
directory it sits in is not followed.

## Do not describe your repository's state inside the repository

A `REPRODUCTION.md` said "this working tree has not yet been committed". The
tree had been committed for hours, but the sentence had not been updated, and
the judges believed it. The artifact was accusing itself.

Veritas judges what the artifact says about itself. A claim about mutable state
— committed or not, which commit, how many tests passed, where the canonical
run lives — goes stale the moment that state changes, and then the artifact is
wrong in a way no amount of judging can fix.

Write mutable facts where they are generated: a provenance file the tooling
writes, not prose a person maintains.

## Supply everything you cite

`artifact.paths` decides what judges can see. A file that exists but is not
listed is invisible, and a judge reports it as missing evidence — correctly,
from where it is standing.

The `reference-integrity` check tells the two apart:

- *Cited path does not exist* — the document is wrong.
- *Cited path exists but was not supplied* — the document is right and
  `veritas.yaml` is incomplete.

If you find yourself editing correct prose to satisfy a judge, check which of
those two you are actually looking at.

Not everything cited should be supplied. A lockfile, a built PDF, a directory
of prior manuscript versions — excluding these is how an evaluation stays
affordable, and ours went from $10.60 a round to $1.42 by narrowing
`artifact.paths` and evaluating one judge at a time while iterating. An
unsupplied path is reported as minor and under its own category
(`check/<name>/unsupplied`), so a deliberate exclusion can be accepted as a
known risk without silencing a genuine dangling reference:

```yaml
gate:
  accepted_risks:
    - category: check/reference-integrity/unsupplied
      reason: "Lockfiles, PDFs and prior versions are excluded to keep evaluation affordable."
```

## Declare every run you kept

Four directories from the same experiment, one cited and three unmentioned,
reads as selection whether or not it was. Two of ours had aborted before any
test ran and two had passed identically; saying so took three lines and turned
an apparent cherry-pick into a replication.

State how many runs completed, which is canonical and why, and that none was
discarded. Then say plainly what the retained runs do *not* establish — two
runs from one machine in one session rule out flakiness, not independent
reproduction.

## Let the cheap checks be cheap

A dangling path, a missing DOI, a link to a mutable branch: each is a rule, and
a rule belongs in a deterministic check that costs nothing and runs every time.
Whether the DOI identifies the right version, whether the comparison supports
the claim — those need judgement, and that is what judges are for.

We paid eight judges, three times over, to find a directory that was not there.
A path lookup would have found it in the first second of the first run.

## Give each judge only what it needs

By default every judge reads the whole artifact. On a real paper that was about
300,000 input tokens per judge, eight judges per evaluation, roughly ten dollars
a round — and eight large requests at once exceeded a 500,000 tokens-per-minute
limit. `judge_paths` narrows each judge to the paths it actually needs:

```yaml
concurrency: 2           # fewer judges in flight at once, under your rate limit

judge_paths:
  archival:       [paper/manuscript.md, paper/evidence/README.md, paper/REPRODUCTION.md]
  citations:      [paper/manuscript.md]
  statistics:     [paper/manuscript.md, tests]
  reproducibility: [paper/REPRODUCTION.md, paper/EXECUTION.md, paper/evidence/README.md]
  # methodology, evidence, repo-consistency and adversarial keep the whole artifact:
  # their job is to notice when the code and the paper disagree.
```

A judge not listed reads everything, so narrowing is always a deliberate choice.
A judge that cannot see the code cannot tell you the code contradicts the paper.
Deterministic checks are unaffected and still see the whole artifact.

When a provider rate-limits a request and says how long to wait, Veritas waits
that long (up to two minutes) instead of burning its retries in a few seconds.
