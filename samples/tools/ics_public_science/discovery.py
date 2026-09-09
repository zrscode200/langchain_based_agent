"""Bounded public DOI and relationship discovery; no scientific interpretation."""
from __future__ import annotations
import hashlib
import json
import re
from xml.etree import ElementTree as ET
from typing import Any

from adapters import pubmed
from http_client import status_failure
from validation import json_within_limits, fingerprint, bounded_text

PMID = re.compile(r"[0-9]{1,12}")
PMCID = re.compile(r"PMC[0-9]{1,12}", re.I)
RELATIONS = {"similar": "pubmed_pubmed", "references": "pubmed_pubmed_refs", "citing": "pubmed_pubmed_citedin"}

class DiscoveryError(ValueError):
    def __init__(self, code: str, detail: str, *, stage: str = "provider_body", retryable: bool = False):
        super().__init__(detail)
        self.code, self.detail, self.stage, self.retryable = code, detail, stage, retryable

async def fetch(http, source, path, *, params=None, xml=False, max_bytes=2*1024*1024):
    result = await http.get(source, path, params=params,
        accept="application/xml" if xml else "application/json", max_bytes=max_bytes)
    error = status_failure(result)
    if error:
        code, detail, retryable = error
        raise DiscoveryError(code, detail, stage="transport", retryable=retryable)
    if len(result.body) > max_bytes:
        raise DiscoveryError("response_too_large", "Provider body exceeded the operation limit")
    return result

def json_body(result):
    if "json" not in result.content_type.lower():
        raise DiscoveryError("unsupported_representation", "Expected provider JSON")
    try:
        value = json.loads(result.body)
    except (ValueError, RecursionError):
        raise DiscoveryError("provider_schema_drift", "Provider returned malformed JSON") from None
    if not json_within_limits(value):
        raise DiscoveryError("parser_limit_exceeded", "Provider JSON exceeded structural limits")
    if not isinstance(value, dict):
        raise DiscoveryError("provider_schema_drift", "Expected a JSON object")
    if any(value.get(k) not in (None, "", [], {}) for k in ("error", "errors", "errMsg", "errCode", "errorMessage", "errorMessages")) or value.get("status") in ("error", "failed"):
        raise DiscoveryError("provider_error_body", "Provider represented an error")
    return value

def xml_body(body: bytes, expected_root: str):
    """No entities or DTD resolution; cap depth/elements while building the tree."""
    if len(body) > 4*1024*1024 or b"\x00" in body:
        raise DiscoveryError("parser_limit_exceeded", "XML exceeded byte or encoding limits")
    if b"<!ENTITY" in body.upper():
        raise DiscoveryError("unsupported_representation", "XML entity declarations are not supported")
    declarations = re.findall(rb"<!DOCTYPE\b[^>]*>", body, re.I)
    if body.upper().count(b"<!DOCTYPE") != len(declarations) or len(declarations) > 1 or any(b"[" in d for d in declarations):
        raise DiscoveryError("unsupported_representation", "XML internal subsets are not supported")
    for declaration in declarations:
        body = body.replace(declaration, b"")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root = None
    try:
        for offset in range(0, len(body), 8192):
            parser.feed(body[offset:offset+8192])
            for event, element in parser.read_events():
                if event == "start":
                    if root is None: root = element
                    depth += 1; count += 1
                    if depth > 96 or count > 60_000:
                        raise DiscoveryError("parser_limit_exceeded", "XML exceeded structural limits")
                else: depth -= 1
        parser.close()
    except DiscoveryError:
        raise
    except (ET.ParseError, LookupError, ValueError):
        raise DiscoveryError("provider_schema_drift", "Malformed provider XML") from None
    if root is None or root.tag != expected_root:
        raise DiscoveryError("unsupported_representation", "Unexpected XML root")
    if root.findall(".//ERROR") or root.findall(".//Error"):
        raise DiscoveryError("provider_error_body", "Provider XML represented an error")
    return root

def nonnegative(value):
    if isinstance(value, bool): return None
    if isinstance(value, int): return value if 0 <= value < 2**63 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        parsed = int(value)
        return parsed if parsed < 2**63 else None
    return None

async def resolve_pubmed_doi(http, doi: str) -> str:
    params = pubmed._ncbi_params({"db": "pubmed", "term": f'"{doi}"[Article Identifier]',
        "retmode": "json", "retmax": 2, "retstart": 0, "sort": "relevance"})
    payload = json_body(await fetch(http, "pubmed", "esearch.fcgi", params=params))
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise DiscoveryError("provider_schema_drift", "DOI lookup omitted ESearch results")
    count, ids = nonnegative(result.get("count")), result.get("idlist")
    if count is None or not isinstance(ids, list) or len(ids)>2 or any(not isinstance(i,str) or not PMID.fullmatch(i) for i in ids):
        raise DiscoveryError("provider_schema_drift", "Invalid DOI lookup population")
    if nonnegative(result.get("retstart")) != 0 or nonnegative(result.get("retmax")) != len(ids):
        raise DiscoveryError("query_fidelity_unknown", "DOI lookup page not confirmed")
    if any(pubmed._has_signal(result.get(k)) for k in ("warninglist", "errorlist")):
        if count != 0 or not pubmed._verified_no_items(count=count, ids=ids, offset=0, warning_list=result.get("warninglist"), error_list=result.get("errorlist")):
            raise DiscoveryError("query_repaired", "Provider warned about the DOI lookup")
    if count == 0 and not ids:
        raise DiscoveryError("provider_not_found", "DOI was not found in the selected index")
    if count > 1:
        raise DiscoveryError("ambiguous_identity", "DOI lookup matched multiple records; select an exact identifier")
    if count != 1 or len(ids) != 1:
        raise DiscoveryError("provider_schema_drift", "DOI lookup count and IDs disagree")
    # ESearch may translate Article Identifier to All Fields. This is candidate
    # discovery only: the caller MUST verify the DOI in the fetched record aliases.
    return ids[0]

def link_seed(source: str, identifier: str):
    clean = identifier.strip()
    if source == "pubmed" and PMID.fullmatch(clean): return "MED", clean
    if source == "europe_pmc":
        if PMID.fullmatch(clean): return "MED", clean
        if clean.upper().startswith("MED:") and PMID.fullmatch(clean[4:]): return "MED", clean[4:]
        if clean.upper().startswith("PMC:") and PMCID.fullmatch(clean[4:]): return "PMC", clean[4:].upper()
    raise DiscoveryError("invalid_identifier", "Use a PMID or an exact Europe PMC MED:<PMID>/PMC:<PMCID> record identity from literature_get", stage="validation")

async def links(http, *, source: str, identifier: str, relation: str, limit: int, state: dict) -> dict:
    collection, seed = link_seed(source, identifier)
    if source == "europe_pmc" and relation == "similar":
        raise DiscoveryError("unsupported_combination", "Similar-article discovery is supported through PubMed ELink", stage="validation")
    offset = state.get("offset", 0)
    pagination_issue = False
    if source == "pubmed":
        link_name = RELATIONS[relation]
        params = pubmed._ncbi_params({"dbfrom":"pubmed", "db":"pubmed", "id":seed,
            "linkname":link_name, "cmd":"neighbor_score", "retmode":"xml"})
        result = await fetch(http, source, "elink.fcgi", params=params, xml=True)
        root = xml_body(result.body, "eLinkResult")
        sets = root.findall("LinkSet")
        if len(sets) != 1 or sets[0].findtext("DbFrom") != "pubmed" or [x.text for x in sets[0].findall("IdList/Id")] != [seed]:
            raise DiscoveryError("identity_mismatch", "ELink did not represent the exact requested seed")
        groups = sets[0].findall("LinkSetDb")
        if any(g.findtext("DbTo") != "pubmed" or g.findtext("LinkName") != link_name for g in groups) or len(groups)>1:
            raise DiscoveryError("identity_mismatch", "ELink returned a different or ambiguous relationship")
        all_rows = []
        for link in groups[0].findall("Link") if groups else []:
            ident = link.findtext("Id")
            if not isinstance(ident,str) or not PMID.fullmatch(ident):
                raise DiscoveryError("provider_schema_drift", "ELink returned an invalid PMID")
            # ELink may include the seed among computational neighbors.
            if relation == "similar" and ident == seed: continue
            score = nonnegative(link.findtext("Score")) if relation == "similar" else None
            all_rows.append({"identity":{"kind":"pmid","value":ident}, "provider_score":score})
        if len({r["identity"]["value"] for r in all_rows}) != len(all_rows):
            raise DiscoveryError("provider_schema_drift", "ELink returned duplicate identities")
        digest = fingerprint(all_rows)
        if state.get("snapshot") and state["snapshot"] != digest:
            raise DiscoveryError("snapshot_drift", "ELink relationship set changed; restart discovery", stage="currentness")
        total = len(all_rows)
        if offset and offset >= total:
            raise DiscoveryError("pagination_boundary", "ELink page is beyond its represented set")
        rows = all_rows[offset:offset+limit]
        next_state = {"offset":offset+len(rows),"snapshot":digest} if offset+len(rows)<total else None
        provenance = {"endpoint":"elink.fcgi", "link_name":link_name, "seed":{"kind":"pmid","value":seed}}
    else:
        endpoint = "references" if relation == "references" else "citations"
        page_index = offset // limit
        params = {"format":"json", "pageSize":limit, "page":page_index+1}
        payload = json_body(await fetch(http,source,f"{collection}/{seed}/{endpoint}",params=params))
        echo = payload.get("request", {})
        # Live pages 2 and 3 echo offSet=1 and 2: this is a zero-based
        # page index, despite its name, not the result offset we track locally.
        if echo.get("id") != seed or echo.get("source") != collection or nonnegative(echo.get("pageSize")) != limit or nonnegative(echo.get("offSet")) != page_index:
            raise DiscoveryError("query_fidelity_unknown", "Europe PMC did not echo the requested seed and page")
        total = nonnegative(payload.get("hitCount"))
        key = "reference" if relation == "references" else "citation"
        values = payload.get(key+"List", {}).get(key)
        if total is None or not isinstance(values,list) or len(values)>limit or total<offset+len(values):
            raise DiscoveryError("provider_schema_drift", "Invalid Europe PMC relationship page")
        if total>offset and not values:
            raise DiscoveryError("pagination_boundary", "Empty relationship page before the observed boundary")
        if "count" in state and state["count"] != total:
            raise DiscoveryError("snapshot_drift", "Relationship count changed; restart discovery", stage="currentness")
        rows=[]
        for row in values:
            ident, src = row.get("id"), row.get("source")
            # External relations can point beyond MED/PMC; retain provider identity,
            # but do not promise those targets are retrievable by this connector.
            if not isinstance(ident,str) or not ident or len(ident)>255 or not isinstance(src,str) or not re.fullmatch(r"[A-Z]{2,8}",src):
                raise DiscoveryError("provider_schema_drift", "Relation omitted its provider identity")
            if (src == "MED" and not PMID.fullmatch(ident)) or (src == "PMC" and not PMCID.fullmatch(ident)):
                raise DiscoveryError("identity_mismatch", "Relationship target has an invalid supported-collection identity")
            rows.append({"identity":{"kind":"europe_pmc","collection":src,"value":ident},
                         "title":bounded_text(row.get("title"),1000),
                         "retrievable_here":src in {"MED","PMC"}, "provider_score":None})
        if len({fingerprint(r["identity"]) for r in rows}) != len(rows):
            raise DiscoveryError("provider_schema_drift", "Duplicate relationship identities")
        pagination_issue = offset + len(rows) < total and len(rows) < limit
        next_state={"offset":offset+len(rows),"count":total} if offset+len(rows)<total and not pagination_issue else None
        provenance={"endpoint":f"{collection}/{seed}/{endpoint}","seed":{"kind":"europe_pmc","collection":collection,"value":seed}}
    return {"relations":[{"relation":relation, "target":row, "provider_position":offset+i} for i,row in enumerate(rows)],
        "provenance":provenance, "next_state":next_state, "pagination_issue":pagination_issue,
        "coverage":{"returned":len(rows), "provider_observed_count":total,
                    "exhaustive":False, "represented_set_exhausted":offset+len(rows)>=total,
                    "limitations":["Indexed links are incomplete and are not evidence strength.",
                      "ELink is bounded by its represented provider set." if source=="pubmed" else "Page counts do not establish a stable snapshot; inspect duplicates across pages."]}}
