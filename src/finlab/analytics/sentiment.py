"""Transparent, local descriptive tone for SEC earnings narratives."""

import re
from datetime import timedelta

VERSION = "earnings-lexical-tone-v1"
POSITIVE = {
    "improved",
    "improving",
    "strengthened",
    "record",
    "profitable",
    "exceeded",
    "outperformed",
    "resilient",
}
NEGATIVE = {
    "deteriorated",
    "impairment",
    "impairments",
    "weakness",
    "declined",
    "unprofitable",
    "adverse",
    "underperformed",
}
NEGATION = {"not", "never", "no", "neither"}


def classify_tone(text: str) -> dict:
    positive = negative = 0
    matched = set()
    for sentence in re.split(r"[.!?\n;]+", text.casefold()):
        words = re.findall(r"[a-z]+", sentence)
        for index, word in enumerate(words):
            sign = 1 if word in POSITIVE else -1 if word in NEGATIVE else 0
            if not sign:
                continue
            if NEGATION.intersection(words[max(0, index - 3) : index]):
                sign *= -1
            positive += sign > 0
            negative += sign < 0
            matched.add(word)
    count = positive + negative
    signal = positive - negative
    return dict(
        tone="insufficient"
        if not count
        else "positive"
        if signal > 0
        else "negative"
        if signal < 0
        else "neutral",
        intensity=abs(signal) / count if count else None,
        matched_terms=sorted(matched),
        matches=count,
        balance=signal / count if count else None,
    )


def sentiment_context(con, security_id, as_of, tables):
    output = {
        "version": VERSION,
        "status": "unavailable",
        "documents": [],
        "attention_30d": 0,
        "prior_attention_30d": 0,
        "balance_change": None,
        "limitation": "Lexical tone from available SEC earnings narratives. Coverage is sparse and tone has no validated predictive interpretation.",
    }
    if "financial_text" not in tables:
        return output
    rows = con.execute(
        """SELECT n.available_on,n.source,n.source_url,n.title,n.event_kind,
        n.document_sha256,n.text_sha256,n.version,n.text
        FROM financial_text n JOIN security s ON n.cik=s.cik WHERE s.security_id=?
        AND n.available_on BETWEEN ? AND ? ORDER BY n.available_on DESC,n.source_url LIMIT 35""",
        [security_id, str(as_of - timedelta(days=180)), str(as_of)],
    ).fetchall()
    balances = [[], []]
    for available, source, url, title, kind, digest, text_hash, version, text in rows:
        score = classify_tone(text)
        output["documents"].append(
            dict(
                available_on=available,
                source=source,
                source_url=url,
                title=title,
                event_kind=kind,
                document_sha256=digest,
                text_sha256=text_hash,
                document_version=version,
                **score,
            )
        )
        if available > str(as_of - timedelta(days=30)):
            output["attention_30d"] += 1
            if score["balance"] is not None:
                balances[0].append(score["balance"])
        elif available > str(as_of - timedelta(days=60)):
            output["prior_attention_30d"] += 1
            if score["balance"] is not None:
                balances[1].append(score["balance"])
    if rows:
        output["status"] = "available"
    if all(balances):
        recent, prior = balances
        output["balance_change"] = sum(recent) / len(recent) - sum(prior) / len(prior)
    return output
