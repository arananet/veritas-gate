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

## Name what you withhold

Raw evidence is often too large to send: 320 per-repetition traces can exceed
a provider's tokens-per-minute limit on their own. Supply the aggregates, and
name the rest instead of leaving it out silently:

```yaml
artifact:
  withheld:
    - evidence/20260713T191740Z/CB-VAL-*
  withheld_reason: "Raw traces archived with the release; too large for the judges' token limit."
```

Each judge is told these files exist and that their absence is not a defect.
A claim resting only on them is reported as unverified (minor, declared), not
as missing evidence. `veritas files` shows how many files are withheld. A path
both in `paths` and `withheld` is supplied.

## Prepare the submission

Point Veritas at the built PDF and the venue:

```yaml
checks:
  pdf-inspection:
    target: paper/main.pdf
  venue:
    venue: tmlr            # tmlr, arxiv or preprint
    target: paper/main.pdf
    source: paper/main.tex
    identity_terms: ["Acme Corp"]   # anything beyond CITATION.cff and the remote
```

pdf-inspection reports unresolved references (??), replacement characters,
fonts not embedded, missing title metadata, and the TeX log's overfull boxes
and undefined labels. venue applies the venue's rules: for a double-blind
venue it searches every identity term (author names, ORCID, email and archive
DOIs from CITATION.cff, the repository owner from the git remote) in the PDF
text, its metadata and the source; it checks the mandatory template and its
options, that the appendix follows the references, the main-text page count,
and a broader impact statement.

`veritas package -o supplementary.zip` builds the anonymized supplementary
archive: git-tracked files only, local configuration excluded, identity terms
replaced in text, then re-scanned. A binary that still contains a term (a
screenshot archive, say) is listed and the command fails, so it is never
uploaded unseen.

## Present it like reviewed work

Set the presentation check's target to your manuscript:

```yaml
checks:
  presentation:
    target: paper/manuscript.md
```

It reports, for nothing: process narration left in the paper ("review
pending", tool failures, test tallies, session notes), long hashes repeated in
prose, a multi-paragraph or over-long abstract, figures the text never cites,
and a paper with no figure. The presentation judge then weighs what needs
judgement: whether the main result is visible early, contributions and
research questions are explicit, captions stand alone (takeaway, metric, n,
what the error bars are), and where a figure would show a pattern better than
a table, drawn from evidence you already have.

## Declare what you generate

Figures, tables and the LaTeX are generated from something. Declare it, and a
stale PDF or a hand-edited figure stops being something a reviewer finds:

```yaml
execution:
  allow: [node, python3]
derived:
  - id: heatmap
    kind: figure
    command: [python3, scripts/plot_heatmap.py]
    inputs: [evidence/run/verdicts.json, scripts/plot_heatmap.py]
    outputs: [paper/figures/heatmap.pdf]
  - id: latex
    command: [node, scripts/paper.mjs, latex]
    inputs: [paper/manuscript.md, paper/template.tex]
    outputs: [paper/manuscript.tex]
checks:
  derived-freshness:
    target: paper/manuscript.md   # also report figures no derivation produces
```

`veritas build` runs each command (allow-listed, no shell) and records the
hashes of its inputs and outputs in `veritas.lock.json`; commit it. The
derived-freshness check then reports, without running anything, an output
whose inputs changed (stale), an output changed by hand, a derivation never
built, and a figure in the manuscript that no derivation produces. A
derivation may read frozen evidence but never write it: a script that
rewrites a frozen file is rejected and the file restored.

## Make every number traceable

A reader should be able to find where each number in the paper comes from.
Name the manuscript and the evidence, and Veritas checks every number:

```yaml
checks:
  numeric-traceability:
    enabled: true
    target: paper/manuscript.md
    sources: [evidence/run/verdicts.json, evidence/run/run_meta.json]
    ignore_numbers: ["128"]      # parameters, not results
```

A number is traced when the evidence contains it, rounded as written, as a
percentage, or as the ratio of two evidence numbers whose denominator the
paper itself states (135/100 = 1.35). Identifiers, dates, versions, hashes,
section references and integers below 11 are skipped. A derived count such as
9 x 7 x 5 = 315 is reported: compute it in a declared script and supply its
output, so the reader can check it too.

## Freeze your evidence

A repair agent asked to resolve "the report contradicts the defect record"
may simply rewrite the report. List the files that record what an experiment
produced, and no repair can change them:

```yaml
artifact:
  frozen:
    - evidence/**
```

Exclude files that change on purpose, such as an index or a stable pointer,
with `!`, as in `.gitignore`:

```yaml
  frozen:
    - evidence/**
    - "!evidence/README.md"
```

The frozen-integrity check asks git whether any frozen file was changed by a
commit after it was first committed, or is changed now: frozen evidence is
append-only, so a correction goes in a new file (for example
`evidence/<run>/corrections/`), never over the original. It also compares
CITATION.cff's `version` with the git tags and, with `target` set, the
manuscript's archive DOIs with CITATION.cff.

Withheld paths are frozen too. The agent is told, and any change it makes to a
frozen file is reverted in the workspace and reported, so the patch you are
offered never contains one. Correct a frozen record in the paper or an errata
file.

## Check the package before arXiv does

For a LaTeX paper the `arxiv-package` check (on in `scientific-paper`)
reports what would break or leak on arXiv: missing `\input`, figures or
`.bib`, a missing `.bbl` (arXiv does not run BibTeX), undefined citation
keys, incomplete references, malformed DOIs, private comments (arXiv
publishes the source) and packages over 50 MB. It reads files only. With
several `.tex` files carrying `\documentclass`, name yours:

```yaml
checks:
  arxiv-package:
    target: paper/v3/main.tex
```

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

## Declare who you are once, and let the files follow

A citable paper needs a `CITATION.cff`, a licence for its code, and a statement
of which licence covers the text, figures and data. None of it is about the
argument, and all of it is easy to forget. The `scientific-paper` profile checks
for these files (`archival-files`), and `veritas scaffold` creates the missing
ones from metadata you declare. It never invents a value and never overwrites a
file.

Facts that do not change between papers go in `~/.config/veritas/metadata.yaml`,
once:

```yaml
authors:
  - given: Your
    family: Name
    orcid: 0000-0000-0000-0000
licence:
  code: MIT
  content: CC-BY-4.0
```

Facts about this paper go in its `veritas.yaml`, and override the user file
field by field:

```yaml
metadata:
  title: "The title of this work"
  repository: https://github.com/you/this-work
  # doi: 10.5281/zenodo.NNNNNNN   add it once you have deposited
```

```bash
veritas scaffold . --dry-run   # what would be written
veritas scaffold .             # write the missing files
```

The full `LICENSE` text is not scaffolded: choose and add it yourself. A DOI
appears in `CITATION.cff` only once you declare one; re-run `scaffold` after
deleting the old file, or edit it by hand.

## Say what you set out to show

```yaml
thesis:
  - "Reusing an identifier overwrites in shared mappings and is retained in separate instances."
  - "Model-only tool rejection depends on the installed host handler, not the SDK default."
```

Judges receive these as your intended claims, to be evaluated like any other —
listing a claim does not make it established. What changes is the advice: where
the evidence falls short, a judge recommends the narrowest wording it supports
instead of removal. The repair agent is told to keep each claim and bound it to
its evidence, never delete it.

## Declare your limitations, and they stop blocking

Judges read the work's own limitations and threats-to-validity sections first.
A problem the work explicitly acknowledges — and acknowledges no more weakly than
the evidence requires — is reported as `declared`, with the acknowledging
passage quoted, and weighed as minor. A critical finding is never softened this
way, and a judge that calls something declared without quoting where is not
believed. If the abstract or conclusion claims more than the declared limitation
allows, that contradiction is reported as a defect.

## Read the last block first

Every evaluation and every loop ends with **Next steps**: what to change in
`veritas.yaml`, what a repair run can fix, what only you can decide, and how many
declared limitations need nothing from you — with the command for each.

## Measure the gate itself

`veritas benchmark benchmarks/manifest.yaml --config veritas.yaml` runs
Veritas on papers whose problems are already known and reports how many it
found (recall) and what else it reported. A case's source can be a local
path, a git URL or an arXiv URL; each known issue gives regular expressions a
finding must match, a minimum severity, and where the issue is documented.
Unmatched findings are candidates to read, not proven false positives. After
refining patterns, `--rescore .veritas/benchmark/<run>` rescores the saved
runs for free.
