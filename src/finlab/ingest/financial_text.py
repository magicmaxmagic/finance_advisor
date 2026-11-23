"""Archive reviewed SEC earnings releases for local text analysis."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4

import polars as pl
from selectolax.parser import HTMLParser

from finlab.db.conn import parquet_files
from finlab.ingest.sec import SecClient
from finlab.ingest.warehouse import publish_table

SOURCE = re.compile(
    r"https://www\.sec\.gov/Archives/edgar/data/[0-9]+/[0-9]{18}/[a-zA-Z0-9._-]+\.html?"
)
MAX_DOCUMENT_BYTES = 10_000_000


def earnings_text(body: bytes) -> str:
    """Use narrative before the forward-looking statements and accounting tables."""
    if not body or len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError("The earnings release is empty or too large for local parsing")
    document = HTMLParser(body)
    paragraphs = []
    for node in document.css("div,p"):
        if any(child != node for child in node.css("div,p,table")):
            continue
        current = node.parent
        inside_table = False
        while current is not None:
            if current.tag == "table":
                inside_table = True
                break
            current = current.parent
        if inside_table:
            continue
        text = " ".join(node.text(separator=" ", strip=True).split())
        if text.casefold() in {"forward-looking statements", "forward looking statements"}:
            break
        if 30 <= len(text) <= 5000:
            paragraphs.append(text)
    text = "\n".join(dict.fromkeys(paragraphs))
    if len(text) < 100:
        raise ValueError("The filing does not provide a supported earnings narrative")
    return text[:24_000]


async def acquire_earnings_text(con, settings) -> tuple[int, int]:
    """Only fetch releases already identified by the reviewed calendar."""
    paths = [str(path) for path in parquet_files(settings.warehouse_dir / "financial_event")]
    if not paths:
        return 0, 0
    candidates = con.execute(
        """SELECT DISTINCT cik,announced_on,source_url,label FROM read_parquet(?)
        WHERE kind='earnings_release' AND status='historical' AND occurred_on IS NOT NULL
        ORDER BY announced_on DESC LIMIT 20""",
        [paths],
    ).fetchall()
    root = settings.warehouse_dir / "financial_text"
    previous = [str(path) for path in parquet_files(root)]
    known = (
        set(
            con.execute("SELECT source_url FROM read_parquet(?)", [previous]).fetchnumpy()[
                "source_url"
            ]
        )
        if previous
        else set()
    )
    rows, unavailable = [], 0
    async with SecClient(settings) as client:
        for cik, availability, source, title in candidates:
            if not SOURCE.fullmatch(source) or source in known:
                continue
            body = await client.get_bytes(source)
            if body is None:
                unavailable += 1
                continue
            try:
                text = earnings_text(body)
            except ValueError:
                unavailable += 1
                continue
            digest = hashlib.sha256(body).hexdigest()
            normalized_hash = hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()
            archive: Path = settings.raw_dir / "financial_text"
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / f"{digest}.htm"
            if not target.exists():
                temporary = archive / f".{uuid4().hex}.tmp"
                temporary.write_bytes(body)
                temporary.replace(target)
            rows.append(
                dict(
                    cik=cik,
                    available_on=availability,
                    source_url=source,
                    title=title,
                    source="SEC EDGAR",
                    event_kind="earnings_release",
                    version="sec-earnings-narrative-v1",
                    document_sha256=digest,
                    text_sha256=normalized_hash,
                    text=text,
                )
            )
    if rows:
        con.register("_news_incoming", pl.DataFrame(rows))
        try:
            if previous:
                con.execute(
                    """CREATE OR REPLACE TEMP TABLE _working_news AS
                    SELECT * FROM read_parquet(?) UNION ALL BY NAME SELECT * FROM _news_incoming""",
                    [previous],
                )
            else:
                con.execute(
                    "CREATE OR REPLACE TEMP TABLE _working_news AS SELECT * FROM _news_incoming"
                )
            con.execute("""CREATE OR REPLACE TEMP TABLE _deduplicated_news AS
                SELECT * FROM _working_news QUALIFY row_number() OVER (
                PARTITION BY cik,text_sha256 ORDER BY available_on,source_url)=1""")
            publish_table(
                con,
                source="_deduplicated_news",
                root=root,
                order_by=("cik", "available_on", "source_url"),
            )
        finally:
            con.unregister("_news_incoming")
    return len(rows), unavailable
