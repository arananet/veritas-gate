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
links.

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
