"""FinLab Data Engine package.

Owns ingestion, warehouse publication, SEC/market providers, and scheduled
writer operations. This package must use the single-writer connection for
all native DuckDB writes.
"""
