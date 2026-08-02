"""The `concept_map` tag normalisation layer and its coverage check.

Loads the hand-maintained YAML in `finlab/data/concept_map.yaml` into the
`concept_map` table, and measures how often each canonical concept actually
resolves. Anything below the threshold means a missing tag mapping, which does
not raise an error at query time — it silently drops the company from a
cross-sectional screen. That is why coverage is a command that exits non-zero
rather than a note in a docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import yaml

CONCEPT_MAP_PATH = Path(__file__).resolve().parent.parent / "data" / "concept_map.yaml"

DEFAULT_VALID_FROM = date(1900, 1, 1)
DEFAULT_VALID_TO = date(9999, 12, 31)

# Below this, a concept is missing tags and will quietly corrupt any
# cross-sectional comparison that uses it.
COVERAGE_THRESHOLD = 0.95


class ConceptMapError(RuntimeError):
    """The concept map file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ConceptTag:
    canonical: str
    kind: str  # 'flow' (has a period) or 'instant' (a point in time)
    taxonomy: str
    tag: str
    priority: int
    sign: int
    valid_from: date
    valid_to: date


@dataclass(frozen=True, slots=True)
class CoverageRow:
    canonical: str
    resolved: int
    expected: int

    @property
    def fraction(self) -> float:
        return self.resolved / self.expected if self.expected else 0.0

    @property
    def passing(self) -> bool:
        return self.fraction >= COVERAGE_THRESHOLD


def load_concept_map_file(path: Path | None = None) -> list[ConceptTag]:
    """Parse the YAML into rows, validating priorities are unique per concept."""
    target = path or CONCEPT_MAP_PATH
    if not target.is_file():
        raise ConceptMapError(f"concept map not found: {target}")

    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    default_taxonomy = str(document.get("taxonomy") or "us-gaap")
    concepts = document.get("concepts") or {}
    if not concepts:
        raise ConceptMapError(f"{target} declares no concepts")

    rows: list[ConceptTag] = []
    for canonical, body in concepts.items():
        kind = str((body or {}).get("kind") or "")
        if kind not in {"flow", "instant"}:
            raise ConceptMapError(
                f"{target}: concept {canonical!r} must declare kind: flow or instant"
            )
        entries = (body or {}).get("tags") or []
        if not entries:
            raise ConceptMapError(f"{target}: concept {canonical!r} has no tags")
        seen: dict[int, str] = {}
        for entry in entries:
            priority = int(entry["priority"])
            tag = str(entry["tag"])
            if priority in seen:
                raise ConceptMapError(
                    f"{target}: concept {canonical!r} reuses priority {priority} "
                    f"for {seen[priority]!r} and {tag!r}; resolution would be arbitrary"
                )
            seen[priority] = tag
            rows.append(
                ConceptTag(
                    canonical=str(canonical),
                    kind=kind,
                    taxonomy=str(entry.get("taxonomy") or default_taxonomy),
                    tag=tag,
                    priority=priority,
                    sign=int(entry.get("sign", 1)),
                    valid_from=entry.get("valid_from") or DEFAULT_VALID_FROM,
                    valid_to=entry.get("valid_to") or DEFAULT_VALID_TO,
                )
            )
    return rows


def canonical_concepts(path: Path | None = None) -> tuple[str, ...]:
    return tuple(sorted({row.canonical for row in load_concept_map_file(path)}))


def load_concept_map(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> int:
    """Replace the `concept_map` table with the contents of the YAML file."""
    rows = load_concept_map_file(path)
    frame = pl.DataFrame(
        {
            "canonical": [r.canonical for r in rows],
            "taxonomy": [r.taxonomy for r in rows],
            "tag": [r.tag for r in rows],
            "priority": [r.priority for r in rows],
            "sign": [r.sign for r in rows],
            "valid_from": [r.valid_from for r in rows],
            "valid_to": [r.valid_to for r in rows],
        },
        schema={
            "canonical": pl.Utf8,
            "taxonomy": pl.Utf8,
            "tag": pl.Utf8,
            "priority": pl.Int32,
            "sign": pl.Int16,
            "valid_from": pl.Date,
            "valid_to": pl.Date,
        },
    )
    con.register("_concept_map", frame)
    con.execute("DELETE FROM concept_map")
    con.execute(
        "INSERT INTO concept_map (canonical, taxonomy, tag, priority, sign, valid_from, valid_to) "
        "SELECT canonical, taxonomy, tag, priority, sign, valid_from, valid_to FROM _concept_map"
    )
    con.unregister("_concept_map")
    return len(rows)


# A concept is only "expected" on a (company, period) where the filer reported
# something of the same kind. Counting `total_assets` as missing on a period for
# which the company filed only an income statement would measure the shape of
# the filing calendar, not the completeness of the mapping.
_COVERAGE_SQL = """
WITH resolved AS (
  SELECT DISTINCT canonical, cik, period_end,
         CASE WHEN duration_bucket = 'instant' THEN 'instant' ELSE 'flow' END AS kind
  FROM fundamentals_complete_asof(?)
  WHERE period_end >= ?
    -- All flow windows count, not just discrete quarters. Cash-flow statements
    -- inside a 10-Q are cumulative, so cfo and capex arrive as 'half'/'ytd9';
    -- excluding those would score a mapped concept as missing.
    AND duration_bucket IN ('annual', 'quarter', 'half', 'ytd9', 'instant')
),
periods AS (
  SELECT DISTINCT r.cik, r.period_end, r.kind
  FROM resolved r JOIN entity e USING (cik)
),
expected AS (
  SELECT c.canonical, p.cik, p.period_end
  FROM periods p JOIN _concept_kinds c USING (kind)
)
SELECT e.canonical,
       count(*) FILTER (WHERE r.canonical IS NOT NULL) AS resolved,
       count(*) AS expected
FROM expected e
LEFT JOIN resolved r
  ON r.canonical = e.canonical AND r.cik = e.cik AND r.period_end = e.period_end
GROUP BY e.canonical
ORDER BY e.canonical
"""


def coverage(
    con: duckdb.DuckDBPyConnection,
    as_of: date,
    *,
    since: date | None = None,
    path: Path | None = None,
) -> list[CoverageRow]:
    """Fraction of company-periods for which each canonical concept resolves.

    Restricted to periods ending on or after `since` (default 2015-01-01):
    coverage over the full 2009-2026 history mostly measures how much the
    us-gaap taxonomy has changed, not whether our mapping is complete today.
    """
    floor = since or date(2015, 1, 1)
    definitions = load_concept_map_file(path)
    kinds = pl.DataFrame(
        sorted({(r.canonical, r.kind) for r in definitions}),
        schema={"canonical": pl.Utf8, "kind": pl.Utf8},
        orient="row",
    )
    con.register("_concept_kinds", kinds)
    try:
        rows = con.execute(_COVERAGE_SQL, [as_of, floor]).fetchall()
    finally:
        con.unregister("_concept_kinds")
    return [CoverageRow(canonical=r[0], resolved=int(r[1]), expected=int(r[2])) for r in rows]
