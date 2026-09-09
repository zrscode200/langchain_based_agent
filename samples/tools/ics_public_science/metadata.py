"""Selected, bounded provider metadata. Representation is not applicability."""
from __future__ import annotations
from typing import Any
import json

METADATA_FAMILIES = {"indexing", "publication_types", "dates", "access", "relationships", "funding"}


def project(value: Any, *, basis: str) -> dict:
    """Bound nested provider data and report every reduction at the family boundary."""
    remaining = 12_000
    truncated = False
    def visit(item, depth=0):
        nonlocal remaining, truncated
        if depth > 12 or remaining < 4:
            truncated = True
            remaining -= 4  # The emitted JSON null still consumes the budget.
            return None
        if isinstance(item, str):
            bounded = item[:2_000]
            while len(json.dumps(bounded, ensure_ascii=True)) > remaining and bounded:
                bounded = bounded[:len(bounded)//2]
            truncated |= bounded != item
            remaining -= len(json.dumps(bounded, ensure_ascii=True))
            return bounded
        if isinstance(item, (list, dict)):
            values = list(item.items()) if isinstance(item, dict) else list(enumerate(item))
            truncated |= len(values) > 100
            result = {} if isinstance(item, dict) else []
            remaining -= 2
            for key, child in values[:100]:
                # Include ordinary JSON separator spaces, conservatively also
                # charging a comma for the final item.
                key_cost = len(json.dumps(key, ensure_ascii=True)) + 4 if isinstance(item, dict) else 2
                if isinstance(item, dict) and (not isinstance(key, str) or len(key)>200):
                    truncated = True
                    continue
                if remaining < key_cost + 4:
                    truncated = True
                    break
                remaining -= key_cost
                out = visit(child, depth + 1)
                if isinstance(result, dict): result[key] = out
                else: result.append(out)
            return result
        if item is None or isinstance(item, (int, float, bool)):
            cost = len(json.dumps(item, allow_nan=False))
            if cost > remaining:
                truncated = True
                remaining -= 4
                return None
            remaining -= cost
            return item
        truncated = True
        remaining -= 4
        return None
    represented = value is not None and value != {} and value != []
    bounded = visit(value)
    state = value.get("state") if isinstance(value, dict) else None
    if not isinstance(state, str) or state not in {"represented", "not_represented", "not_requested", "unknown"}:
        state = None
    return {"state": state or ("represented" if represented else "not_represented"),
            "value": bounded, "truncated": truncated, "basis": basis}


def europe_metadata(row: dict, selected: list[str]) -> dict:
    values = {
        "indexing": {k: row[k] for k in ("meshHeadingList", "keywordList", "chemicalList") if k in row},
        "publication_types": row.get("pubTypeList"),
        "dates": {k: row[k] for k in ("firstPublicationDate", "electronicPublicationDate", "firstIndexDate", "dateOfCreation", "dateOfCompletion", "dateOfRevision", "pubYear") if k in row},
        "access": {k: row[k] for k in ("isOpenAccess", "inPMC", "inEPMC", "hasPDF", "hasSuppl", "license", "fullTextUrlList") if k in row},
        "relationships": {k: row[k] for k in ("commentCorrectionList", "isRetracted") if k in row},
        "funding": row.get("grantsList"),
    }
    return {name: project(values[name], basis="Europe PMC core metadata") for name in selected}


def pubmed_metadata(row: dict, selected: list[str]) -> dict:
    extension = row.get("extension") or {}
    values = {"indexing": extension.get("indexing"),
              "publication_types": extension.get("publication_roles"),
              "dates": row.get("dates"), "access": row.get("access"),
              "relationships": row.get("direct_relationships"), "funding": extension.get("funding")}
    output = {name: project(values[name], basis="PubMed EFetch metadata") for name in selected}
    if "access" in selected:
        output["access"]["full_text_routes"] = [
            {"identifier": a["identity"], "route": "Europe PMC article XML may be available; license not established by this alias"}
            for a in row.get("aliases", []) if a.get("identity", {}).get("kind") == "pmcid"]
    return output
