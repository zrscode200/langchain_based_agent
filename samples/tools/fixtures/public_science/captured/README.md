# Captured public API contracts

`manifest.json` records fixed public GET requests, UTC observation times, HTTP
status, content type, byte count and SHA-256 for each body. Capture with
`tools/public_science_capture.py` only when intentionally refreshing fixtures.
Do not overwrite these during ordinary tests. The capture includes actual error
bodies; HTTP 503 is not an empty scientific result.
Citation pages 2 and 3 were captured after a live MCP check exposed that Europe
PMC's `offSet` echo is a zero-based page index, not a record offset. They retain
their own observation times and original response bodies in the manifest.

PubMed examples use PMID 23193287, DOI 10.1093/nar/gks1195 (Europe PMC: a full-text
literature database for the life sciences and platform for innovation), plus
public asthma query examples. Europe PMC metadata and article XML use PMID
37994696 / PMC10767826 / DOI 10.1093/nar/gkad1085 (Europe PMC in 2023). The article
XML carries a Creative Commons Attribution 4.0 statement; its authors, title,
publication details and full license statement are retained in the fixture.
Sources: https://pubmed.ncbi.nlm.nih.gov/23193287/ and
https://europepmc.org/articles/PMC10767826 . These are provider-contract examples,
not scientific evidence about any ICS program.

`public_science_check.py` distinguishes captured responses from synthetic
surrounding metadata and intentionally mutated failure/edge fixtures. Replay
proves connector behavior for the captured representation; live MCP checks are
separate and do not consume these files.
