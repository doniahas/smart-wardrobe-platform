"""Catalog source adapters.

Each adapter implements the SourceAdapter protocol (see base.py): it knows
how to fetch and normalize products for one source only. Concurrency,
retries, aggregation, and SourceStatus tracking all live in
app/services/crawler.py, not here.
"""