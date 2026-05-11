# NextCure Intelligence System — v0.9.32 Adaptive Outcome Evidence

This build refines the first real external intelligence lane: ClinicalTrials.gov.

## What changed

- Removed manually seeded/pseudo patent, grant, and funding placeholders from the executive flow.
- Kept ClinicalTrials.gov as the first real live external source.
- Rewrote ClinicalTrials.gov synthesis so the four executive buckets receive interpreted reads instead of raw source-count language.
- Improved usefulness of the trial lane by emphasizing direct-lane activity, active sponsor/phase density, repeated trial-design language, ovarian ADC activity, side-channel reads, and positioning implications.
- Preserved the future database hook via `persistence_payload`.
- No Streamlit cache was added; each run performs a fresh lightweight pull.

## Audit

- Python compile check
- Pytest suite
- Direct analysis smoke test
- ZIP integrity check

## v0.9.36 dynamic sponsor-discovery patch

This patch upgrades the ClinicalTrials.gov intelligence layer from a narrow one-page signal pull into a bounded discovery system:

- Expands CDH6, B7-H4, ovarian ADC, gynecologic ADC, and broader ADC query terms with target/program aliases.
- Uses larger page sizes and bounded pagination so the backend can discover more sponsors before executive filtering.
- Adds `engines/sponsor_discovery_engine.py` to build a dynamic sponsor registry from ClinicalTrials.gov lead sponsors and collaborators.
- Normalizes sponsor names so subsidiaries/legal variants collapse into one sponsor entity.
- Updates sponsor evidence routing so static ticker mappings are optional enrichment only, not the gatekeeper for who gets discovered.
- Preserves unmapped/private/academic sponsors in a drill-down table with generated evidence-search links for IR/press release follow-up.
- Feeds the sponsor discovery and evidence status back into the Executive Summary four-question board through the existing clinical signal path.

Audit run: `PYTHONPATH=. pytest -q` passed with 10 tests.


## v0.9.38 Recall-First ClinicalTrials.gov Discovery

This build replaces the prior owned-program guardrail patch with a sponsor-agnostic, recall-first ClinicalTrials.gov discovery layer. Discovery now runs by target, condition, intervention/modality, title/acronym, and broad ADC context rather than by hardcoded sponsor. Sponsors are extracted only after trials are surfaced from ClinicalTrials.gov payloads.

Key additions:
- Multi-family ClinicalTrials.gov query expansion across `query.term`, `query.intr`, `query.titles`, and `query.cond`.
- Bounded pagination with page-cap diagnostics.
- Per-query discovery audit table showing fetched records, retained records, available totals, search area, query family, and truncation state.
- NCT-level deduplication that preserves discovery provenance and matched relevance fields.
- Trial table now includes query family, matched fields, relevance score, and discovery provenance.
- Sponsor registry remains dynamic: sponsors and collaborators are extracted from discovered trial records, not predefined as the discovery source.

Build audit: `PYTHONPATH=. pytest -q` → 12 passed.
