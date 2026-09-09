#!/usr/bin/env python3
"""Capture a fixed, bounded set of public API contracts. No credentials or program data.

Explicit opt-in: python3 tools/public_science_capture.py --output /path/to/capture
Responses are fixtures, not scientific evidence about a pharmaceutical program.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

BASES = {"pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/",
         "europe_pmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/"}

def requests():
    for name, query in [("atm", "asthma"),
                        ("fields", '"Asthma"[MeSH Terms] AND "Review"[Publication Type] AND 2020:2025[Date - Publication]'),
                        ("doi", '"10.1093/nar/gks1195"[Article Identifier]')]:
        yield "pubmed_" + name, "pubmed", "esearch.fcgi", {"db": "pubmed", "term": query, "retmode": "json", "retmax": 2, "retstart": 0, "sort": "relevance"}
    yield "pubmed_metadata", "pubmed", "efetch.fcgi", {"db": "pubmed", "id": "23193287", "retmode": "xml", "rettype": "abstract"}
    for kind in ["pubmed_pubmed", "pubmed_pubmed_refs", "pubmed_pubmed_citedin"]:
        yield kind, "pubmed", "elink.fcgi", {"dbfrom": "pubmed", "db": "pubmed", "id": "23193287", "linkname": kind, "cmd": "neighbor_score", "retmode": "xml"}
    for name, query, synonym in [("metadata", '(PMCID:"PMC10767826") AND (SRC:MED OR SRC:PMC)', False),
                                   ("doi", '(DOI:"10.1093/nar/gkad1085") AND (SRC:MED OR SRC:PMC)', False),
                                   ("expanded", '(TITLE_ABS:antibody) AND (SRC:MED OR SRC:PMC)', True)]:
        yield "epmc_"+name, "europe_pmc", "search", {"query": query, "format": "json", "resultType": "core", "pageSize": 2, "cursorMark": "*", "sort": "", "synonym": str(synonym).lower()}
    yield "epmc_article", "europe_pmc", "PMC10767826/fullTextXML", {}
    for ident in ["37994696", "23193287"]:
        for relation in ["references", "citations"]:
            yield "epmc_"+ident+"_"+relation, "europe_pmc", "MED/"+ident+"/"+relation, {"format":"json","pageSize":2,"page":1}
    for page in (2, 3):
        yield f"epmc_37994696_citations_page{page}", "europe_pmc", "MED/37994696/citations", {"format":"json","pageSize":2,"page":page}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", action="append", help="Capture only this fixed request name; repeat to select several")
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    manifest=[]; opener=urllib.request.build_opener(NoRedirect())
    for name,provider,path,params in requests():
        if args.only and name not in args.only:
            continue
        time.sleep(0.4)
        url=BASES[provider]+path
        req=urllib.request.Request(url+("?"+urllib.parse.urlencode(params) if params else ""), headers={"User-Agent":"ics-public-contract-capture/1.0", "Accept-Encoding":"identity"})
        entry={"name":name,"provider":provider,"path":path,"params":params,"observed_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
        try:
            try:
                response=opener.open(req,timeout=15)
            except urllib.error.HTTPError as exc:
                response=exc
            with response:
                body=response.read(2*1024*1024+1)
                entry.update(status=response.code,content_type=response.headers.get("Content-Type",""))
            if len(body)>2*1024*1024:
                raise ValueError("response exceeded 2 MiB capture cap")
            suffix=".json" if "json" in entry["content_type"] else ".xml" if "xml" in entry["content_type"] else ".txt"
            entry.update(file=name+suffix,bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
            (args.output/entry["file"]).write_bytes(body)
        except Exception as exc:
            entry["error"]=type(exc).__name__
        manifest.append(entry)
        (args.output/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
        print(json.dumps(entry),flush=True)
if __name__=="__main__": main()
