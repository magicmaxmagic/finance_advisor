"""Recover consolidated observations from an exact SEC primary Inline XBRL filing."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from finlab.config import Settings

IX = "{http://www.xbrl.org/2013/inlineXBRL}"
XBRL = "{http://www.xbrl.org/2003/instance}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
PRIMARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?")
SUPPORTED_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
UNITS = {"iso4217:USD": "USD", "xbrli:shares": "shares"}
NUMBER_FORMATS = {
    "ixt:num-dot-decimal",
    "ixt:numdotdecimal",
}
MAX_BYTES = 35_000_000


class StatementRecoveryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def filing_url(cik: int, accession: str, filename: str | None) -> str | None:
    """Only request the primary file named by SEC Submissions."""
    if not ACCESSION.fullmatch(accession) or not filename or not PRIMARY.fullmatch(filename):
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{filename}"


def _number(node: ET.Element) -> Decimal | None:
    data = "".join(node.itertext()).strip()
    form = node.get("format")
    scale = node.get("scale", "0")
    sign = node.get("sign")
    if node.get(XSI_NIL) in {"true", "1"} or sign not in {None, "-"}:
        return None
    if not re.fullmatch(r"-?[0-9]{1,2}", scale) or not -9 <= int(scale) <= 12:
        return None
    if form in {"ixt:fixed-zero", "ixt-sec:zerodash"}:
        return Decimal(0) if data.strip() in {"-", "\u2013", "—", "0"} else None
    if form not in {None, *NUMBER_FORMATS} or not re.fullmatch(
        r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", data
    ):
        return None
    amount = Decimal(data.replace(",", "")).scaleb(int(scale))
    if amount >= Decimal("1e17"):
        return None
    return -amount if sign else amount


def _best_value(observations: set[tuple[Decimal, int]]) -> float | None:
    """Resolve duplicate presentations only when their disclosed precisions agree."""
    precision = max(level for _, level in observations)
    exact = {value for value, level in observations if level == precision}
    if len(exact) != 1:
        return None
    chosen = next(iter(exact))
    if any(abs(chosen - value) > Decimal("0.5").scaleb(-level) for value, level in observations):
        return None
    return float(chosen)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return datetime.strptime(" ".join(value.split()), "%B %d, %Y").date()


def _validate_identity(header: dict[str, set[str]], filing: dict, end: date) -> tuple[int, str]:
    def single(key: str) -> str:
        value = header.get(key, set())
        if len(value) != 1:
            raise ValueError
        return next(iter(value))

    try:
        form = single("DocumentType")
        amendment = filing["form"].endswith("/A")
        actual_amendment = single("AmendmentFlag").lower()
        year = int(single("DocumentFiscalYearFocus"))
        quarter = single("DocumentFiscalPeriodFocus")
        if (
            int(single("EntityCentralIndexKey")) != filing["cik"]
            or _date_value(single("DocumentPeriodEndDate")) != end
            or (form not in {filing["form"], filing["form"].removesuffix("/A")})
            or actual_amendment not in ({"true", "1"} if amendment else {"false", "0"})
            or not 1990 <= year <= 2100
            or (quarter != "FY" if form.startswith("10-K") else quarter not in {"Q1", "Q2", "Q3"})
        ):
            raise ValueError
    except ValueError as exc:
        raise StatementRecoveryError("PRIMARY_FILING_IDENTITY_MISMATCH") from exc
    return year, quarter


def extract_statement(body: bytes, filing: dict) -> dict:
    """Return a restricted Company Facts-shaped payload for the reported period."""
    if not body or len(body) > MAX_BYTES:
        raise StatementRecoveryError("PRIMARY_FILING_INVALID")
    try:
        end = date.fromisoformat(filing["report_period"])
        filed = date.fromisoformat(filing["filed_date"])
    except (ValueError, TypeError) as exc:
        raise StatementRecoveryError("PRIMARY_FILING_IDENTITY_MISMATCH") from exc
    if end > filed or filing["form"] not in SUPPORTED_FORMS:
        raise StatementRecoveryError("PRIMARY_FILING_IDENTITY_MISMATCH")

    identities: dict[str, set[str]] = {}
    try:
        parser = ET.iterparse(io.BytesIO(body), events=("start-ns", "end"))
        for event, item in parser:
            if event == "start-ns":
                name, uri = item
                identities.setdefault(name, set()).add(uri)
    except ET.ParseError as exc:
        raise StatementRecoveryError("PRIMARY_FILING_PARSE_ERROR") from exc
    for name, family in (
        ("us-gaap", "http://fasb.org/us-gaap/"),
        ("dei", "http://xbrl.sec.gov/dei/"),
        ("iso4217", "http://www.xbrl.org/2003/iso4217"),
    ):
        declared = identities.get(name, set())
        if len(declared) != 1 or not next(iter(declared)).startswith(family):
            raise StatementRecoveryError("PRIMARY_FILING_UNSUPPORTED_TAXONOMY")

    root = parser.root
    contexts = {}
    for context in root.iter(XBRL + "context"):
        identifier = context.find(f".//{XBRL}identifier")
        if identifier is None or (identifier.text or "").lstrip("0") != str(filing["cik"]):
            continue
        if identifier.get("scheme") != "http://www.sec.gov/CIK" or any(
            list(part) for name in ("segment", "scenario") for part in context.iter(XBRL + name)
        ):
            continue
        instant = context.findtext(f".//{XBRL}instant")
        closing = instant or context.findtext(f".//{XBRL}endDate")
        start = context.findtext(f".//{XBRL}startDate")
        if closing != str(end):
            continue
        if not instant:
            try:
                length = (end - date.fromisoformat(start or "")).days
            except ValueError:
                continue
            if not any(
                first <= length <= last
                for first, last in ((80, 100), (170, 190), (260, 285), (350, 380))
            ):
                continue
        contexts[context.get("id")] = start
    units = {}
    for unit in root.iter(XBRL + "unit"):
        measure = unit.findtext(XBRL + "measure")
        divide = unit.find(XBRL + "divide")
        if len(unit) == 1 and measure in UNITS:
            units[unit.get("id")] = UNITS[measure]
        elif (
            len(unit) == 1
            and divide is not None
            and divide.findtext(f"{XBRL}unitNumerator/{XBRL}measure") == "iso4217:USD"
            and divide.findtext(f"{XBRL}unitDenominator/{XBRL}measure") == "xbrli:shares"
            and len(list(divide.iter(XBRL + "measure"))) == 2
        ):
            units[unit.get("id")] = "USD/shares"

    header: dict[str, set[str]] = {}
    for node in root.iter(IX + "nonNumeric"):
        name = node.get("name", "")
        if name.startswith("dei:") and node.get("contextRef") in contexts:
            header.setdefault(name[4:], set()).add("".join(node.itertext()).strip())
    year, quarter = _validate_identity(header, filing, end)

    data: dict[tuple[str, str, str | None], set[tuple[Decimal, int]]] = {}
    for node in root.iter(IX + "nonFraction"):
        context = node.get("contextRef")
        unit = units.get(node.get("unitRef"))
        tag = node.get("name", "")
        if context not in contexts or not unit or not tag.startswith("us-gaap:"):
            continue
        number = _number(node)
        decimals = node.get("decimals", "")
        if number is not None and (decimals == "INF" or re.fullmatch(r"-?[0-9]{1,2}", decimals)):
            precision = 100 if decimals == "INF" else int(decimals)
            data.setdefault((tag[8:], unit, contexts[context]), set()).add((number, precision))

    compatible = {
        key: amount for key, values in data.items() if (amount := _best_value(values)) is not None
    }
    flows = [
        (tag, date.fromisoformat(start))
        for tag, unit, start in compatible
        if unit == "USD" and start
    ]
    income = {"NetIncomeLoss", "ProfitLoss", "IncomeLossFromContinuingOperations"}
    cash = {
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    }
    annual = filing["form"].startswith("10-K")
    if (
        compatible.get(("Assets", "USD", None), 0) <= 0
        or not any(
            tag in income
            and (350 <= (end - start).days <= 380 if annual else 80 <= (end - start).days <= 100)
            for tag, start in flows
        )
        or not any(tag in cash for tag, _ in flows)
    ):
        raise StatementRecoveryError("PRIMARY_STATEMENTS_INCOMPLETE")

    tags: dict[str, dict] = {}
    for (tag, unit, start), amount in compatible.items():
        observation = dict(
            end=str(end),
            val=amount,
            accn=filing["accession"],
            fy=year,
            fp=quarter,
            filed=str(filed),
            form=filing["form"],
        )
        if start:
            observation["start"] = start
        tags.setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(observation)
    return {"cik": filing["cik"], "facts": {"us-gaap": tags}}


def recovery_path(settings: Settings, cik: int, accession: str) -> Path:
    if not ACCESSION.fullmatch(accession):
        raise ValueError("Invalid filing accession")
    return settings.raw_dir / "statement_recovery" / f"{cik:010d}" / f"{accession}.json.gz"


def store_recovery(settings: Settings, filing: dict, body: bytes) -> dict:
    facts = extract_statement(body, filing)
    source_url = filing["primary_url"]
    if source_url != filing_url(filing["cik"], filing["accession"], source_url.rsplit("/", 1)[-1]):
        raise StatementRecoveryError("PRIMARY_FILING_IDENTITY_MISMATCH")
    digest = hashlib.sha256(body).hexdigest()
    provenance = dict(
        method="sec_primary_inline_v1",
        source_url=source_url,
        source_sha256=digest,
        retrieved_at=datetime.now(UTC).isoformat(),
        observation_count=sum(
            len(items)
            for tag in facts["facts"]["us-gaap"].values()
            for items in tag["units"].values()
        ),
    )
    target = recovery_path(settings, filing["cik"], filing["accession"])
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = target.parent / f"{filing['accession']}-{digest}.htm.gz"
    for path, contents in (
        (raw, body),
        (target, json.dumps({"filing": filing, "provenance": provenance}, sort_keys=True).encode()),
    ):
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        temporary.write_bytes(gzip.compress(contents, mtime=0))
        temporary.replace(path)
    return provenance


def load_recovery(path: Path) -> tuple[dict, dict, dict]:
    with gzip.open(path, "rt") as handle:
        entry = json.load(handle)
    filing = entry["filing"]
    provenance = entry["provenance"]
    digest = provenance["source_sha256"]
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or path.name != f"{filing['accession']}.json.gz"
        or path.parent.name != f"{filing['cik']:010d}"
        or filing["primary_url"] != provenance["source_url"]
    ):
        raise StatementRecoveryError("PRIMARY_FILING_CACHE_INVALID")
    with gzip.open(path.parent / f"{filing['accession']}-{digest}.htm.gz", "rb") as handle:
        body = handle.read(MAX_BYTES + 1)
    if hashlib.sha256(body).hexdigest() != digest:
        raise StatementRecoveryError("PRIMARY_FILING_CACHE_INVALID")
    return filing, provenance, extract_statement(body, filing)
