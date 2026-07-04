# Tech Blog — Concrete-Evidence Quality Lift

**Date:** 2026-07-05
**Status:** Approved direction, pending spec review
**Scope:** `n8n-humanizer-seoul-workflow/scripts/trend_writer.py`

## Context

Unlike Seoul Picks (which had a rubber-stamp local judge), the tech blog pipeline is
already mature: `POST_VARIANTS=5` candidates, `POST_JUDGES=2`, `POST_MIN_SCORE=95`,
`POST_MAX_ROUNDS=2` revision rounds, a humanizer gate, a 15-dimension `QUALITY_RUBRIC`
(incl. 근거/출처/검증 8, 구체적 사례/재현 7, 사실관계/기술 정확성 10), supporting-reference
selection, and a revision-feedback loop.

So the "similar feel, higher quality" ask is NOT new infrastructure. The analog to Seoul
Picks' concrete-product enrichment is: make **concrete, verifiable evidence a hard publish
blocker**, so plausible-but-generic tech prose can't quietly score 95.

## Goal

Raise the floor on evidence and specificity without rewriting the pipeline: a tech article
should not pass the gate unless it cites real sources it was actually built from and, for
how-to / technical explainers, shows at least one concrete, reproducible artifact (command,
code/config snippet, version-pinned steps, or a specific benchmark/number).

## Non-goals

- No change to the multi-round gate architecture, humanizer, or scoring machinery.
- No new external data sources or scraping.
- No threshold changes (stay at 95/90) unless review shows the new rule is too strict.

## Changes (all in `trend_writer.py`)

1. **Judge prompt (`evaluate_post`)** — add explicit hard-blocker criteria:
   - A key/technical claim presented without a traceable source (the selected article or a
     named supporting reference) is a publish blocker.
   - A how-to / technical explainer with no concrete reproducible artifact (command, code
     or config block, version/date-pinned steps, or a specific measured number) is a
     publish blocker.
   - Generic filler that would fit any article on the topic (no specifics tied to THIS
     source) is a publish blocker.
   Keep the existing "95 = publishable, blockers required to go below" contract.

2. **Draft prompt (`generate_post`)** — instruct the writer to ground each key claim in the
   selected article or a named supporting reference, and to include at least one concrete
   reproducible artifact for how-to/technical topics. Prose stays human (humanizer
   unchanged).

3. **Deterministic evidence check (small, optional)** — a lightweight
   `evidence_coverage_issue(body, article, supporting)` helper mirroring Seoul Picks'
   deterministic gates: flag when the body contains zero source links/references while the
   selected article + supporting set had URLs available. Feed its result into the judge
   feedback (not a hard auto-fail, to avoid false positives on opinion pieces).

## Testing

- `evidence_coverage_issue` returns an issue for a body with no references when references
  were available, and "" when the body cites them.
- The judge prompt contains the new hard-blocker clauses (string presence test).
- Existing trend_writer tests still pass (`scripts/test_*` if present).

## Open risk

The concrete-artifact blocker could be too strict for non-technical/opinion tech posts.
Mitigation: scope the artifact requirement to how-to/technical explainer intent, and keep
the evidence check as judge feedback rather than a hard deterministic fail. Revisit
thresholds if publish rate drops too far after review.
