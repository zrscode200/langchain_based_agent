#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1", "httpx>=0.27", "pydantic>=2", "truststore>=0.9"]
# ///
"""Opt-in bounded live MCP stdio smoke test using public queries/identifiers only.

Run: python tools/public_science_live_check.py --output /path/to/live-results.json
Does not load .mcp.json, MUSE, credentials, program state or cached fixture bodies.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from importlib.metadata import version
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main(output):
    evidence={"observed_at":datetime.now(timezone.utc).isoformat(),"transport":"MCP stdio", "runtime":{"python":sys.version,"mcp":version("mcp"),"httpx":version("httpx"),"pydantic":version("pydantic")}, "calls":[],"passed":False}
    def save():output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
    # Deliberately no ICS_* secrets/operator identifiers in the test subprocess.
    env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','HOME','TMPDIR','PYTHONPATH') if k in os.environ}
    env['PYTHONDONTWRITEBYTECODE']='1'
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve().parent/'ics_public_science/server.py')],env=env)
    try:
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tool_list=await session.list_tools()
                evidence['tools']=[t.name for t in tool_list.tools]
                assert set(evidence['tools'])=={'literature_search','literature_get','literature_links','literature_read'}
                async def call(name,**args):
                    await asyncio.sleep(1.0)
                    result=await session.call_tool(name,args)
                    value=result.structuredContent
                    if value is None:
                        value=json.loads(next(c.text for c in result.content if c.type=='text'))
                    evidence['calls'].append({'tool':name,'arguments':args,'isError':result.isError,'result':value})
                    save()
                    assert not result.isError,(name,'MCP error')
                    return value
                def success(r):assert r['state'] in {'success','partial_success'},r.get('failures')
                r=await call('literature_search',source='pubmed',query='asthma',limit=2)
                success(r);assert r['query']['mode']=='controlled' and r['records']
                r=await call('literature_search',source='pubmed',query='asthma',query_mode='provider',target='provider',limit=2)
                success(r);assert r['query']['fidelity']=='provider_interpreted' and 'MeSH Terms' in r['query']['observation']['translated_query']
                r=await call('literature_search',source='pubmed',query='"Asthma"[MeSH Terms] AND "Review"[Publication Type] AND 2020:2025[Date - Publication]',query_mode='provider',target='provider',limit=2)
                success(r);assert r['records']
                r=await call('literature_get',source='pubmed',identifier='DOI:10.1093/nar/gks1195',metadata=['indexing','funding','publication_types','access'])
                success(r);assert r['record']['identity']['value']=='23193287'
                assert any(i.get('kind')=='doi' and i.get('value')=='10.1093/nar/gks1195' for i in r['record']['identifiers'])
                assert r['record']['metadata']['funding']['value']['represented_count']>=1
                for relation in ('references','similar','citing'):
                    r=await call('literature_links',source='pubmed',identifier='23193287',relation=relation,limit=2)
                    success(r);assert r['relations'] and all(e['relation']==relation for e in r['relations'])
                    if relation=='references':
                        first=r
                        r=await call('literature_links',source='pubmed',identifier='23193287',relation=relation,limit=2,continuation=r['continuation']['token'])
                        success(r);assert r['relations'][0]['target']['identity']!=first['relations'][0]['target']['identity']
                r=await call('literature_search',source='europe_pmc',query='TITLE_ABS:antibody',query_mode='provider',target='provider',expand_synonyms=True,limit=2)
                success(r);assert r['query']['observation']['synonym'] is True
                r=await call('literature_get',source='europe_pmc',identifier='DOI:10.1093/nar/gkad1085',metadata=['indexing','access','funding','dates'])
                success(r);assert r['record']['identity']['value']=='37994696'
                assert r['record']['metadata']['access']['value']['license']
                r=await call('literature_links',source='europe_pmc',identifier='MED:37994696',relation='citing',limit=2)
                success(r);assert r['relations']
                first=r
                r=await call('literature_links',source='europe_pmc',identifier='MED:37994696',relation='citing',limit=2,continuation=r['continuation']['token'])
                success(r);assert r['relations'][0]['target']['identity']!=first['relations'][0]['target']['identity']
                r=await call('literature_links',source='europe_pmc',identifier='MED:37994696',relation='references',limit=2)
                if r['state']=='failed' and all(f['code']=='provider_server_error' and not f['absence'] for f in r['failures']):
                    evidence['provider_limitations']=['Europe PMC references endpoint unavailable; positive live reference retrieval there is not verified. Failure handling verified.']
                else:success(r)
                outline=await call('literature_read',source='europe_pmc',identifier='PMC10767826')
                success(outline);assert outline['tables'] and outline['sections'] and outline['access']['licenses']
                r=await call('literature_read',source='europe_pmc',identifier='PMC10767826',view='section',locator=outline['sections'][0]['locator'],expected_content_hash=outline['content_hash'])
                success(r);assert r['content']['blocks']
                r=await call('literature_read',source='europe_pmc',identifier='PMC10767826',view='table',locator=outline['tables'][0]['locator'],expected_content_hash=outline['content_hash'])
                success(r);assert r['content']['rows'][0]['cells'][2]['colspan']==3 and 'preprint' in r['content']['caption']
                r=await call('literature_read',source='europe_pmc',identifier='PMC10767826',view='section',locator=outline['sections'][0]['locator'],expected_content_hash='0'*64)
                assert r['state']=='failed' and r['failures'][0]['code']=='stale_content_handle'
                r=await call('literature_search',source='pubmed',query='confidential mechanism',query_mode='provider',target='provider')
                assert r['state']=='blocked' and r['disclosure']['outbound_calls']==0
                evidence['passed']=True
    finally:save()
    print(json.dumps({'passed':evidence['passed'],'calls':len(evidence['calls']),'output':str(output),'limitations':evidence.get('provider_limitations',[])}))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();asyncio.run(main(args.output))
