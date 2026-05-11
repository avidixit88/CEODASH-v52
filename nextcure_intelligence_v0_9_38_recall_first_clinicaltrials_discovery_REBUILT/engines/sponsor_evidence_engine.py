"""Adaptive sponsor-evidence layer.

ClinicalTrials.gov can show protocol intent, but it often does not contain
posted efficacy values for ongoing trials. This module takes the sponsors
surfaced from the clinical-trials pull, checks available market/news handles,
and classifies whether recent sponsor communications mention readouts, future
data timing, or clinically meaningful endpoint language.

The output is intentionally structured as evidence states, not curated prose.
The executive summary then decides what deserves surface area.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import time
from typing import Any, Iterable, Protocol



class DiscoveredSponsorLike(Protocol):
    sponsor_name: str
    normalized_name: str
    matched_lanes: tuple[str, ...]
    program_terms: tuple[str, ...]
    relevance_score: int
    evidence_queries: tuple[str, ...]

try:  # optional in tests/fallbacks
    import yfinance as yf
except Exception:  # pragma: no cover - environment-specific
    yf = None  # type: ignore[assignment]

from config.sponsor_evidence_sources import (
    MAX_NEWS_ITEMS_PER_TICKER,
    MAX_SPONSORS_PER_RUN,
    SPONSOR_EVIDENCE_LOOKUP,
    SponsorEvidenceSource,
)


RESULT_TERMS = (
    "orr", "objective response", "overall response", "response rate",
    "pfs", "progression-free", "duration of response", "dor",
    "overall survival", "os", "complete response", "partial response",
)
SAFETY_TERMS = (
    "safety", "tolerability", "adverse event", "toxicity", "grade 3",
    "discontinuation", "dose limiting", "recommended phase 2", "rp2d",
)
DATA_TIMING_TERMS = (
    "asco", "aacr", "esmo", "sitc", "present", "presentation", "abstract",
    "data", "readout", "topline", "updated results", "oral presentation",
)
CLINICAL_CONTEXT_TERMS = (
    "ovarian", "cdh6", "b7-h4", "b7h4", "antibody-drug conjugate", "adc",
    "platinum-resistant", "gynecologic", "gynecological",
)

# Broad oncology words such as "cancer" or "phase 2" are intentionally not
# sufficient for executive-facing evidence. They are too noisy when searching
# large sponsors like Merck, Pfizer, or BMS. Evidence must overlap with the
# monitored target/indication/modality vocabulary or sponsor-specific program
# terms from config.sponsor_evidence_sources.


@dataclass(frozen=True)
class SponsorEvidenceItem:
    sponsor: str
    ticker: str
    title: str
    publisher: str
    published_at: str
    url: str
    evidence_state: str
    matched_terms: tuple[str, ...]
    relevance_score: int
    overlap_terms: tuple[str, ...] = ()
    provenance: str = "media/news article"
    relevance_tier: str = "low"
    evidence_route: str = "ticker_news"


@dataclass(frozen=True)
class SponsorEvidenceSummary:
    source_status: str
    fetched_at_utc: str
    sponsors_checked: tuple[str, ...]
    items: tuple[SponsorEvidenceItem, ...]
    source_errors: tuple[str, ...]
    discovered_sponsors: tuple[str, ...] = ()
    unmapped_sponsors: tuple[str, ...] = ()
    evidence_search_links: tuple[str, ...] = ()

    @property
    def result_items(self) -> list[SponsorEvidenceItem]:
        return [i for i in self.items if i.evidence_state == "reported_data_signal"]

    @property
    def timing_items(self) -> list[SponsorEvidenceItem]:
        return [i for i in self.items if i.evidence_state == "future_data_timing_signal"]

    @property
    def clinical_items(self) -> list[SponsorEvidenceItem]:
        return [i for i in self.items if i.evidence_state in {"reported_data_signal", "future_data_timing_signal", "clinical_context_signal"}]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("–", "-").split())


def _matches_any(text: str, terms: Iterable[str]) -> list[str]:
    haystack = _norm(text)
    return [term for term in terms if term in haystack]


def _source_for_sponsor(sponsor: str) -> SponsorEvidenceSource | None:
    sponsor_l = _norm(sponsor)
    candidates: list[tuple[int, SponsorEvidenceSource]] = []
    for source in SPONSOR_EVIDENCE_LOOKUP:
        names = (source.sponsor, *source.aliases)
        if any(_norm(name) in sponsor_l or sponsor_l in _norm(name) for name in names):
            candidates.append((source.priority, source))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def _select_sponsor_sources(sponsors: Iterable[str]) -> list[SponsorEvidenceSource]:
    selected: dict[str, SponsorEvidenceSource] = {}
    for sponsor in sponsors:
        source = _source_for_sponsor(sponsor)
        if source is not None:
            selected[source.sponsor] = source
    return sorted(selected.values(), key=lambda s: s.priority)[:MAX_SPONSORS_PER_RUN]


def _dynamic_sources_for_discovered(discovered_sponsors: Iterable[DiscoveredSponsorLike] | None) -> tuple[list[SponsorEvidenceSource], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Resolve discovered sponsors into searchable sources when possible.

    The discovered sponsor list is the source of truth. Static ticker mappings are
    only optional enrichment. Unmapped sponsors are preserved with evidence-search
    links instead of being silently dropped.
    """
    if discovered_sponsors is None:
        return [], (), (), ()

    resolved: dict[str, SponsorEvidenceSource] = {}
    discovered_names: list[str] = []
    unmapped: list[str] = []
    links: list[str] = []

    for sponsor in sorted(discovered_sponsors, key=lambda s: getattr(s, "relevance_score", 0), reverse=True):
        name = getattr(sponsor, "sponsor_name", "") or getattr(sponsor, "normalized_name", "")
        if not name or name in discovered_names:
            continue
        discovered_names.append(name)
        mapped = _source_for_sponsor(name)
        if mapped is not None:
            # Add discovered program terms so relevance scoring reflects the trial
            # that caused this sponsor to be discovered.
            terms = tuple(dict.fromkeys((*mapped.evidence_terms, *getattr(sponsor, "program_terms", ()))))
            resolved[mapped.sponsor] = SponsorEvidenceSource(
                sponsor=mapped.sponsor,
                tickers=mapped.tickers,
                aliases=tuple(dict.fromkeys((*mapped.aliases, name))),
                priority=mapped.priority,
                evidence_terms=terms,
            )
        else:
            unmapped.append(name)
            for link in getattr(sponsor, "evidence_queries", ())[:3]:
                if link not in links:
                    links.append(link)

    return sorted(resolved.values(), key=lambda s: s.priority), tuple(discovered_names), tuple(unmapped), tuple(links[:24])


def _news_items_for_ticker(ticker: str) -> list[dict[str, Any]]:
    if yf is None:
        raise RuntimeError("yfinance is not available")
    raw = yf.Ticker(ticker).news or []  # type: ignore[union-attr]
    return raw[:MAX_NEWS_ITEMS_PER_TICKER]


def _extract_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("content", {}).get("title") or "").strip()


def _extract_publisher(item: dict[str, Any]) -> str:
    return str(item.get("publisher") or item.get("content", {}).get("provider", {}).get("displayName") or "").strip()


def _extract_url(item: dict[str, Any]) -> str:
    return str(item.get("link") or item.get("content", {}).get("canonicalUrl", {}).get("url") or "").strip()


def _extract_published_at(item: dict[str, Any]) -> str:
    ts = item.get("providerPublishTime") or item.get("content", {}).get("pubDate")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, UTC).date().isoformat()
        except Exception:
            return ""
    return str(ts or "").strip()[:10]


def _classify_provenance(title: str, publisher: str, url: str) -> str:
    text = _norm(" ".join([title, publisher, url]))
    if any(term in text for term in ("press release", "businesswire", "prnewswire", "globenewswire", "investor relations")):
        return "press release / IR"
    if any(term in text for term in ("asco", "aacr", "esmo", "sitc", "abstract", "oral presentation", "poster")):
        return "conference / abstract"
    if any(term in text for term in ("sec", "10-k", "10-q", "8-k", "annual report")):
        return "filing / investor update"
    return "media/news article"


def _classify_item(source: SponsorEvidenceSource, ticker: str, item: dict[str, Any]) -> SponsorEvidenceItem | None:
    title = _extract_title(item)
    if not title:
        return None
    publisher = _extract_publisher(item)
    url = _extract_url(item)
    published_at = _extract_published_at(item)
    text = " ".join([title, publisher, url])
    result_terms = _matches_any(text, RESULT_TERMS)
    safety_terms = _matches_any(text, SAFETY_TERMS)
    timing_terms = _matches_any(text, DATA_TIMING_TERMS)
    context_terms = _matches_any(text, CLINICAL_CONTEXT_TERMS)
    sponsor_program_terms = _matches_any(text, source.evidence_terms)
    overlap_terms = tuple(dict.fromkeys(context_terms + sponsor_program_terms))

    # Guardrail: a large sponsor's unrelated oncology/news headline is not enough.
    # We require monitored target/indication/modality overlap, or sponsor-specific
    # program overlap, before an item can enter the executive evidence stream.
    if not overlap_terms:
        return None
    if not any([result_terms, safety_terms, timing_terms, context_terms, sponsor_program_terms]):
        return None

    provenance = _classify_provenance(title, publisher, url)
    relevance = (
        len(result_terms) * 4
        + len(safety_terms) * 3
        + len(timing_terms) * 2
        + len(context_terms) * 2
        + len(sponsor_program_terms) * 3
    )
    if provenance in {"press release / IR", "conference / abstract"}:
        relevance += 3

    if (result_terms or safety_terms) and len(overlap_terms) >= 1:
        state = "reported_data_signal"
    elif timing_terms and len(overlap_terms) >= 1:
        state = "future_data_timing_signal"
    else:
        state = "clinical_context_signal"

    if relevance >= 12:
        tier = "high"
    elif relevance >= 7:
        tier = "moderate"
    else:
        tier = "low"

    # Suppress low-confidence media/news items unless they carry data timing or
    # result language plus strong overlap. They remain discoverable later through
    # raw source expansion, but should not clutter the executive brief.
    if tier == "low" and provenance == "media/news article" and state == "clinical_context_signal":
        return None

    terms = tuple(dict.fromkeys(result_terms + safety_terms + timing_terms + context_terms + sponsor_program_terms))
    return SponsorEvidenceItem(
        sponsor=source.sponsor,
        ticker=ticker,
        title=title,
        publisher=publisher,
        published_at=published_at,
        url=url,
        evidence_state=state,
        matched_terms=terms,
        relevance_score=relevance,
        overlap_terms=overlap_terms,
        provenance=provenance,
        relevance_tier=tier,
    )


def build_sponsor_evidence_summary(
    sponsors: Iterable[str],
    discovered_sponsors: Iterable[DiscoveredSponsorLike] | None = None,
) -> SponsorEvidenceSummary:
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")

    dynamic_sources, discovered_names, unmapped_sponsors, search_links = _dynamic_sources_for_discovered(discovered_sponsors)
    legacy_sources = _select_sponsor_sources(sponsors)

    # Discovered sources win; legacy selection remains as a backward-compatible
    # fallback for tests and any callers that have not yet adopted the registry.
    source_by_name: dict[str, SponsorEvidenceSource] = {s.sponsor: s for s in legacy_sources}
    for source in dynamic_sources:
        source_by_name[source.sponsor] = source
    sources = sorted(source_by_name.values(), key=lambda s: s.priority)[:MAX_SPONSORS_PER_RUN]

    checked: list[str] = []
    items: list[SponsorEvidenceItem] = []
    errors: list[str] = []

    for source in sources:
        checked.append(source.sponsor)
        for ticker in source.tickers:
            try:
                for raw_item in _news_items_for_ticker(ticker):
                    item = _classify_item(source, ticker, raw_item)
                    if item is not None:
                        items.append(item)
            except Exception as exc:  # upstream news failure should not break analysis
                errors.append(f"{source.sponsor} / {ticker}: {type(exc).__name__}: {exc}")
            time.sleep(0.03)

    deduped: dict[tuple[str, str], SponsorEvidenceItem] = {}
    for item in items:
        key = (_norm(item.title), item.ticker)
        existing = deduped.get(key)
        if existing is None or item.relevance_score > existing.relevance_score:
            deduped[key] = item
    ordered = sorted(deduped.values(), key=lambda i: (i.relevance_score, i.published_at), reverse=True)[:12]

    if ordered:
        status = "live"
    elif checked and errors:
        status = "degraded"
    elif checked:
        status = "empty"
    elif discovered_names or unmapped_sponsors:
        status = "discovered_unmapped"
    else:
        status = "unmapped"

    return SponsorEvidenceSummary(
        source_status=status,
        fetched_at_utc=fetched_at,
        sponsors_checked=tuple(checked),
        items=tuple(ordered),
        source_errors=tuple(errors),
        discovered_sponsors=tuple(discovered_names),
        unmapped_sponsors=tuple(unmapped_sponsors),
        evidence_search_links=tuple(search_links),
    )


def sponsor_evidence_table(summary: SponsorEvidenceSummary):
    import pandas as pd

    return pd.DataFrame([
        {
            "Sponsor": item.sponsor,
            "Ticker": item.ticker,
            "Evidence State": item.evidence_state,
            "Title": item.title,
            "Publisher": item.publisher,
            "Published": item.published_at,
            "Matched Terms": ", ".join(item.matched_terms),
            "Overlap Terms": ", ".join(item.overlap_terms),
            "Provenance": item.provenance,
            "Relevance Tier": item.relevance_tier,
            "Relevance Score": item.relevance_score,
            "Evidence Route": item.evidence_route,
            "URL": item.url,
        }
        for item in summary.items
    ])
