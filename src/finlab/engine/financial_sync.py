"""Incremental SEC acquisition with resumable issuer and accession checkpoints."""

import asyncio
import gzip
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from finlab.engine.status import read_sec_checkpoint, write_sec_checkpoint
from finlab.ingest.companyfacts import (
    FACT_COLUMNS,
    IngestError,
    fetch_poc_universe,
    ingest_poc,
    load_manifest,
    parse_companyfacts,
    read_raw,
    save_manifest,
)
from finlab.ingest.sec import SecClient, SecEtiquetteError, SecFailure, sec_failure
from finlab.ingest.statement_recovery import (
    SUPPORTED_FORMS,
    StatementRecoveryError,
    filing_url,
    load_recovery,
    recovery_path,
    store_recovery,
)
from finlab.ingest.submissions import (
    ingest_submissions,
    load_submissions,
    raw_path,
    validate_filing_columns,
)
from finlab.ingest.universe import load_poc_universe

log = structlog.get_logger(__name__)
STATEMENTS = {"10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F"}
ACCESSION, PERIOD, FILED, FORM = (
    FACT_COLUMNS.index(field) for field in ("accession_number", "period_end", "filed_date", "form")
)


class SecSyncIncomplete(RuntimeError):
    def __init__(self, report):
        self.retry_after = max(
            [
                3600 if report["pending"] else 0,
                report.get("archive_retry_after", 0),
                *(failure["retry_after"] or 0 for failure in report["issuer_errors"].values()),
            ]
        )
        self.permanent = any(
            not failure["retryable"] for failure in report["issuer_errors"].values()
        )
        super().__init__(
            f"SEC synchronization incomplete: {len(report['pending'])} pending statement(s), "
            f"{len(report['issuer_errors'])} issuer error(s); previous publication retained"
        )


def _cached_accessions(settings, cik):
    try:
        payload = read_raw(settings, cik)
        if int(payload.get("cik", 0)) != cik or not isinstance(payload.get("facts"), dict):
            return {}, "COMPANY_FACTS_CACHE_INVALID"
        statements = {}
        for row in parse_companyfacts(payload):
            statements.setdefault(row[ACCESSION], set()).add(
                (str(row[PERIOD]), str(row[FILED]), row[FORM])
            )
        return statements, None
    except FileNotFoundError:
        return {}, "COMPANY_FACTS_CACHE_MISSING"
    except IngestError:
        return {}, "COMPANY_FACTS_INVALID_OBSERVATIONS"
    except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError):
        return {}, "COMPANY_FACTS_CACHE_INVALID"


def _discover(settings, companies, previous, cutoff, through, failures):
    """Remember previously unresolved filings even outside the discovery window."""
    candidates = {(row["cik"], row["accession"]): row for row in previous}
    for company in companies:
        if str(company.cik) in failures:
            continue
        try:
            with gzip.open(raw_path(settings, company.cik), "rt") as handle:
                payload = json.load(handle)
            if int(payload.get("cik", 0)) != company.cik:
                raise ValueError("Issuer does not match the SEC submission")
            index = payload["filings"]["recent"]
            validate_filing_columns(index)
        except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError):
            failures[str(company.cik)] = asdict(SecFailure("SUBMISSIONS_CACHE_INVALID", True))
            continue
        tags = dict(zip(index["accessionNumber"], index.get("isXBRL", []), strict=False))
        inline = dict(zip(index["accessionNumber"], index.get("isInlineXBRL", []), strict=False))
        periods = dict(zip(index["accessionNumber"], index.get("reportDate", []), strict=False))
        documents = dict(
            zip(index["accessionNumber"], index.get("primaryDocument", []), strict=False)
        )
        for accession, form, filed in zip(
            index["accessionNumber"],
            index["form"],
            index["filingDate"],
            strict=True,
        ):
            key = (company.cik, accession)
            if (
                form.removesuffix("/A") not in STATEMENTS
                or filed > through
                or (filed < cutoff and key not in candidates)
            ):
                continue
            if form.endswith("/A") and tags.get(accession) in (0, "0"):
                candidates.pop(key, None)
                continue
            candidates[key] = dict(
                cik=company.cik,
                ticker=company.ticker,
                accession=accession,
                filed_date=filed,
                form=form,
                report_period=periods.get(accession) or None,
                inline_xbrl=inline.get(accession) in (1, "1") if accession in inline else None,
                primary_url=filing_url(company.cik, accession, documents.get(accession)),
            )
    return candidates


def _check_statement(settings, filing, observations):
    """Return an exact availability result without making a provider call."""
    accession = filing["accession"]
    available, cache_error = observations
    if accession in available and (
        not filing.get("report_period")
        or (filing["report_period"], filing["filed_date"], filing["form"]) in available[accession]
    ):
        return None, None
    if filing.get("primary_url") and filing.get("inline_xbrl") and filing.get("report_period"):
        try:
            verified, provenance, _ = load_recovery(
                recovery_path(settings, filing["cik"], accession)
            )
            keys = ("cik", "accession", "filed_date", "report_period", "form", "primary_url")
            if all(verified[key] == filing[key] for key in keys):
                return None, provenance
            return "PRIMARY_FILING_IDENTITY_MISMATCH", None
        except FileNotFoundError:
            pass
        except (OSError, EOFError, ValueError, TypeError, KeyError):
            return "PRIMARY_FILING_CACHE_INVALID", None
    if accession in available:
        return "COMPANY_FACTS_PERIOD_MISMATCH", None
    return cache_error or "ACCESSION_MISSING_IN_COMPANY_FACTS", None


async def _recover_primary(settings, pending, report=None):
    """Use the official primary document only for verified Company Facts gaps."""
    results = {}
    async with SecClient(settings) as client:
        for filing in pending:
            accession = filing["accession"]
            try:
                body = await client.get_bytes(filing["primary_url"])
                if body is None:
                    results[accession] = ("PRIMARY_FILING_NOT_AVAILABLE", None)
                    continue
                results[accession] = (None, store_recovery(settings, filing, body))
            except StatementRecoveryError as exc:
                results[accession] = (exc.code, None)
            except httpx.HTTPError as exc:
                failure = sec_failure(exc)
                results[accession] = (f"PRIMARY_FILING_{failure.code}", None)
                if report is not None:
                    report["archive_retry_after"] = max(
                        report.get("archive_retry_after", 0), failure.retry_after or 0
                    )
                    if not failure.retryable:
                        report["issuer_errors"][str(filing["cik"])] = asdict(
                            SecFailure(f"PRIMARY_FILING_{failure.code}", False)
                        )
                if failure.code in {"HTTP_401", "HTTP_403", "RATE_LIMITED"}:
                    break  # Let the provider's access response govern future requests.
    return results


def inspect_cached_financials(settings, *, now=None):
    """Report known SEC cache gaps without asserting a successful synchronization."""
    now = now or datetime.now(UTC)
    directory = settings.data_dir / "control"
    previous = read_sec_checkpoint(directory)
    companies = load_poc_universe(settings.universe_path)
    absent = {company.cik for company in companies if not raw_path(settings, company.cik).is_file()}
    errors = {
        str(cik): asdict(SecFailure("SUBMISSIONS_CACHE_MISSING", True)) for cik in sorted(absent)
    }
    candidates = _discover(
        settings,
        companies,
        previous["pending"],
        (now.date() - timedelta(days=30)).isoformat(),
        now.date().isoformat(),
        errors,
    )
    available = {cik: _cached_accessions(settings, cik) for cik in {key[0] for key in candidates}}
    recovered, pending = {}, []
    for (cik, accession), row in sorted(candidates.items()):
        reason, provenance = _check_statement(settings, row, available[cik])
        if reason:
            pending.append({**row, "reason": reason})
        elif provenance:
            recovered[accession] = {**row, **provenance}
    return {
        **previous,
        "state": "degraded" if pending or errors else previous["state"],
        "updated_at": now.isoformat(),
        "observation": "local_cache",
        "pending": pending,
        "verified_recoveries": {**previous.get("verified_recoveries", {}), **recovered},
        "issuer_errors": errors,
    }


def sync_financials(con, settings, *, full: bool = False) -> int:
    """Refresh working observations; the caller may publish only after verification."""
    directory = settings.data_dir / "control"
    previous = read_sec_checkpoint(directory)
    now = datetime.now(UTC)
    resumed = previous["state"] in {"running", "degraded"} and previous.get("full") == full
    prior_start = previous.get("cycle_started_at") if resumed else None
    # Short retries finish the same discovery cycle. Later runs refresh the index
    # again so new accessions can be discovered during a prolonged incident.
    since = datetime.fromisoformat(prior_start) if prior_start else None
    if since and now - since >= timedelta(hours=12):
        since = None
    report = dict(
        state="running",
        full=full,
        cycle_started_at=(since or now).isoformat(),
        updated_at=now.isoformat(),
        last_success_at=previous.get("last_success_at"),
        pending=previous.get("pending", []),
        verified_recoveries=previous.get("verified_recoveries", {}),
        issuer_errors={},
        staging_required=previous.get("staging_required", False),
    )
    write_sec_checkpoint(directory, report)
    try:
        count = _synchronize(con, settings, report, directory, full, since, now)
    except SecEtiquetteError as exc:
        report["issuer_errors"]["provider"] = asdict(SecFailure("INVALID_SEC_CONTACT", False))
        report["state"] = "degraded"
        raise SecSyncIncomplete(report) from exc
    except Exception:
        report["state"] = "degraded"
        raise
    else:
        report.update(state="complete", last_success_at=datetime.now(UTC).isoformat())
        return count
    finally:
        report["updated_at"] = datetime.now(UTC).isoformat()
        write_sec_checkpoint(directory, report)
        log.info(
            "sec_sync_finished",
            state=report["state"],
            pending=len(report["pending"]),
            failed_issuers=report["issuer_errors"],
            affected_issuers=report.get("affected_issuers", 0),
        )


def _synchronize(con, settings, report, directory, full, since, now):
    companies = load_poc_universe(settings.universe_path)
    submissions = ingest_submissions(
        con,
        settings,
        force=True,
        refresh_archives=full,
        resume_since=since,
    )
    errors = {
        str(cik): asdict(
            getattr(submissions, "errors", {}).get(cik, SecFailure("SUBMISSIONS_MISSING", True))
        )
        for cik in submissions.missing
    }
    manifest = load_manifest(settings)
    candidates = _discover(
        settings,
        companies,
        report["pending"],
        (now.date() - timedelta(days=30)).isoformat(),
        now.date().isoformat(),
        errors,
    )
    by_issuer = {}
    for (cik, accession), row in candidates.items():
        entry = manifest.get(str(cik), {})
        checked = entry.get("checked_statement_accessions", [])
        # V6 treated every absent amendment as narrative. Verify previously
        # unchecked amendment evidence once unless explicitly non-XBRL.
        if accession not in checked or (
            row["form"].endswith("/A") and accession not in entry.get("verified_amendments", [])
        ):
            by_issuer.setdefault(cik, {})[accession] = row
    report["pending"] = [row for rows in by_issuer.values() for row in rows.values()]
    report["issuer_errors"] = errors
    affected = [
        company
        for company in companies
        if (full or company.cik in by_issuer) and str(company.cik) not in errors
    ]
    report["affected_issuers"] = len(affected)
    report["staging_required"] = report["staging_required"] or bool(affected)
    write_sec_checkpoint(directory, report)
    fetch = []
    for company in affected:
        required = by_issuer.get(company.cik, {})
        fetched_at = manifest.get(str(company.cik), {}).get("fetched_at")
        if since and fetched_at and datetime.fromisoformat(fetched_at) >= since:
            cached = _cached_accessions(settings, company.cik)
            if not cached[1] and all(
                not _check_statement(settings, row, cached)[0] for row in required.values()
            ):
                continue
        fetch.append(company)
    if fetch:
        fetched = asyncio.run(fetch_poc_universe(fetch, settings, force=True))
        for cik in fetched.missing:
            errors[str(cik)] = asdict(
                getattr(fetched, "errors", {}).get(cik, SecFailure("COMPANY_FACTS_MISSING", True))
            )
    pending, recoverable = {}, []
    for cik, records in by_issuer.items():
        failure = errors.get(str(cik))
        cached = _cached_accessions(settings, cik) if not failure else ({}, failure["code"])
        for accession, row in records.items():
            reason, provenance = (
                _check_statement(settings, row, cached) if not failure else (failure["code"], None)
            )
            if provenance:
                report["verified_recoveries"][accession] = {**row, **provenance}
            if not reason:
                continue
            pending[accession] = {**row, "reason": reason}
            if (
                reason
                in {
                    "ACCESSION_MISSING_IN_COMPANY_FACTS",
                    "COMPANY_FACTS_PERIOD_MISMATCH",
                    "PRIMARY_FILING_CACHE_INVALID",
                }
                and not cached[1]
                and row.get("inline_xbrl")
                and row.get("primary_url")
                and row.get("report_period")
                and row["form"] in SUPPORTED_FORMS
            ):
                recoverable.append(row)
    if recoverable and not any(
        failure["code"] in {"HTTP_401", "HTTP_403", "RATE_LIMITED"} for failure in errors.values()
    ):
        for accession, (reason, provenance) in asyncio.run(
            _recover_primary(settings, recoverable, report)
        ).items():
            if provenance:
                report["verified_recoveries"][accession] = {**pending.pop(accession), **provenance}
                report["verified_recoveries"][accession].pop("reason")
            elif reason:
                pending[accession]["reason"] = reason
    if pending or errors:
        report["pending"] = sorted(pending.values(), key=lambda row: (row["cik"], row["accession"]))
        raise SecSyncIncomplete(report)
    if report["staging_required"]:
        # Staging is deferred until every issuer and archive page is verified.
        # If interrupted before committing checkpoints, re-stage from cached bytes.
        loaded = ingest_poc(con, settings, force=False)
        if loaded.fetch.missing:
            raise RuntimeError(
                f"SEC staging requires a complete issuer set: {loaded.fetch.missing}"
            )
        load_submissions(con, companies, settings)
        manifest = load_manifest(settings)
        for cik, records in by_issuer.items():
            entry = manifest.setdefault(str(cik), {})
            entry["checked_statement_accessions"] = sorted(
                set(entry.get("checked_statement_accessions", [])) | records.keys()
            )
            entry["verified_amendments"] = sorted(
                set(entry.get("verified_amendments", []))
                | {accession for accession, row in records.items() if row["form"].endswith("/A")}
            )
            entry["statements_checked_at"] = now.isoformat()
        save_manifest(settings, manifest)
        report["staging_required"] = False
    report["pending"] = []
    return len(affected)
