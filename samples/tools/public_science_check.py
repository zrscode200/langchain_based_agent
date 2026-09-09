#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1", "httpx>=0.27", "pydantic>=2"]
# ///
"""Offline MCP contract regressions using captured public responses and edge fixtures."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'ics_public_science'))
import server
from http_client import HttpResult
from registry import RuntimeRegistry
from discovery import xml_body, DiscoveryError
from mcp.server.fastmcp.exceptions import ToolError

CAPTURE=HERE/'fixtures/public_science/captured'
MANIFEST={e['name']:e for e in json.loads((CAPTURE/'manifest.json').read_text())}

def captured(name):
    m=MANIFEST[name];body=(CAPTURE/m['file']).read_bytes()
    assert hashlib.sha256(body).hexdigest()==m['sha256'],name
    return HttpResult(m['status'],body,m['content_type'],{},len(body))

def response(body,status=200,ctype='application/json'):
    if not isinstance(body,bytes):body=json.dumps(body).encode()
    return HttpResult(status,body,ctype,{},len(body))

class Replay:
    def __init__(self,*results):self.results=list(results);self.calls=[]
    async def get(self,source,path,**kwargs):
        before=kwargs.pop('before_attempt');before()
        self.calls.append((source,path,kwargs))
        if not self.results:raise AssertionError('Unexpected outbound call')
        return self.results.pop(0)

def call(name,**kwargs):
    # Real registered MCP argument validation and dispatch, not a function stub.
    return asyncio.run(server.mcp._tool_manager.get_tool(name).run(kwargs))

class PublicScienceTests(unittest.TestCase):
    def setUp(self):
        self.old_runtime=server._RUNTIME
        self.policy=patch.dict(os.environ,{},clear=False);self.policy.start()
        for k in ('ICS_DISCLOSURE_DENY_FILE','ICS_DISCLOSURE_ALLOW_TERMS'):os.environ.pop(k,None)
    def tearDown(self):server._RUNTIME=self.old_runtime;self.policy.stop()
    def replay(self,*results):
        r=Replay(*results);server._RUNTIME=RuntimeRegistry(http=r);return r
    def failure(self,r,code):
        self.assertIn(r['state'],{'failed','rejected','blocked','inconsistent'})
        self.assertTrue(any(f['code']==code and not f['absence'] for f in r['failures']),r)
    def test_capture_integrity(self):
        for name in MANIFEST:
            with self.subTest(name=name):captured(name)
    def test_tool_arguments(self):
        r=self.replay()
        for name,kwargs in [
            ('literature_links',dict(source='pubmed',identifier='23193287',relation='references',limit=True)),
            ('literature_links',dict(source='pubmed',identifier='23193287',relation='citing',unknown=1)),
            ('literature_read',dict(source='europe_pmc',identifier='PMC10767826',view='table',expected_content_hash=3)),
            ('literature_get',dict(source='pubmed',identifier='23193287',metadata=['made_up'])),
            ('literature_search',dict(source='pubmed',query='asthma',expand_synonyms=1))]:
            with self.subTest(name=name,kwargs=kwargs):
                with self.assertRaises(ToolError):call(name,**kwargs)
        self.assertEqual(r.calls,[])
    def test_provider_mode_is_explicit(self):
        r=self.replay()
        for kwargs in [dict(query='asthma',query_mode='provider'),
                       dict(query='asthma',target='provider'),
                       dict(query='asthma',expand_synonyms=True),
                       dict(query='asthma)[Title] OR (all',query_mode='provider',target='provider')]:
            self.failure(call('literature_search',source='pubmed',**kwargs),'invalid_request')
        self.assertEqual(r.calls,[])
    def test_native_pubmed_translation(self):
        payload=json.loads(captured('pubmed_atm').body)
        ids=payload['esearchresult']['idlist']
        summary={'result':{'uids':ids,**{i:{'uid':i,'title':'Captured-ID fixture summary','authors':[],'pubdate':'2026','pubtype':['Journal Article']} for i in ids}}}
        # ESearch body is captured; ESummary is explicitly synthetic surrounding data.
        r=self.replay(captured('pubmed_atm'),response(summary))
        result=call('literature_search',source='pubmed',query='asthma',query_mode='provider',target='provider',limit=2)
        self.assertEqual(result['state'],'success',result)
        self.assertEqual(result['query']['fidelity'],'provider_interpreted')
        self.assertIn('MeSH Terms',result['query']['observation']['translated_query'])
        self.assertIsNone(result['query']['observation']['translation_scope_preserved'])
        self.assertEqual(r.calls[0][2]['params']['term'],'asthma')
        self.assertEqual(result['disclosure']['outbound_calls'],2)
        self.assertEqual([x['identity']['value'] for x in result['records']],ids)
    def test_controlled_still_rejects_scope_broadening(self):
        self.replay(captured('pubmed_atm'))
        result=call('literature_search',source='pubmed',query='asthma',limit=2)
        self.failure(result,'query_repaired');self.assertFalse(result['records'])
    def test_europe_synonyms_and_binding(self):
        r=self.replay(captured('epmc_expanded'))
        args=dict(source='europe_pmc',query='TITLE_ABS:antibody',query_mode='provider',target='provider',expand_synonyms=True,limit=2)
        result=call('literature_search',**args)
        self.assertEqual(result['state'],'success',result)
        self.assertTrue(result['query']['observation']['synonym'])
        self.assertEqual(r.calls[0][2]['params']['synonym'],'true')
        self.failure(call('literature_search',**{**args,'expand_synonyms':False,'continuation':result['continuation']['token']}),'invalid_request')
        self.assertEqual(len(r.calls),1)
    def test_europe_synonym_conflict(self):
        self.replay(captured('epmc_expanded'))
        result=call('literature_search',source='europe_pmc',query='TITLE_ABS:antibody',query_mode='provider',target='provider',limit=2)
        self.failure(result,'query_fidelity_unknown')
    def test_pubmed_doi_and_metadata(self):
        r=self.replay(captured('pubmed_doi'),captured('pubmed_metadata'))
        result=call('literature_get',source='pubmed',identifier='https://doi.org/10.1093/nar/gks1195',metadata=['indexing','funding','publication_types','relationships','access','dates'])
        self.assertEqual(result['state'],'success',result)
        rec=result['record'];self.assertEqual(rec['identity']['value'],'23193287')
        self.assertEqual(rec['metadata']['funding']['value']['represented_count'],1)
        self.assertIn('mesh_heading',json.dumps(rec['metadata']['indexing']))
        self.assertTrue(rec['metadata']['access']['full_text_routes'])
        self.assertEqual(result['disclosure']['outbound_calls'],2)
        self.assertEqual(len(r.calls),2)
    def test_doi_mismatch_and_ambiguity(self):
        search=json.loads(captured('pubmed_doi').body)
        self.replay(captured('pubmed_doi'),captured('pubmed_metadata'))
        self.failure(call('literature_get',source='pubmed',identifier='DOI:10.1234/mismatch'),'identity_mismatch')
        search['esearchresult'].update(count='2',retmax='2',idlist=['23193287','1234'])
        r=self.replay(response(search))
        self.failure(call('literature_get',source='pubmed',identifier='DOI:10.1093/nar/gks1195'),'ambiguous_identity')
        self.assertEqual(len(r.calls),1)
    def test_europe_doi_and_metadata(self):
        self.replay(captured('epmc_doi'))
        result=call('literature_get',source='europe_pmc',identifier='DOI:10.1093/nar/gkad1085',metadata=['indexing','access','dates','funding'])
        self.assertEqual(result['state'],'success',result)
        rec=result['record'];self.assertEqual(rec['identity'],{'kind':'europe_pmc','collection':'MED','value':'37994696'})
        self.assertEqual(rec['metadata']['access']['value']['license'],'cc by')
        self.assertIn('firstIndexDate',rec['metadata']['dates']['value'])
    def test_metadata_defaults_and_nonrequested_abstract(self):
        self.replay(captured('pubmed_metadata'))
        r=call('literature_get',source='pubmed',identifier='23193287',include_abstract=False)
        self.assertNotIn('metadata',r['record']);self.assertEqual(r['record']['abstract']['state'],'not_requested')
        self.replay(captured('epmc_metadata'))
        r=call('literature_get',source='europe_pmc',identifier='PMC10767826',include_abstract=False,metadata=['relationships'])
        self.assertEqual(r['record']['abstract']['state'],'not_requested')
        self.assertEqual(r['record']['metadata']['relationships']['state'],'not_represented')
    def test_disclosure_all_new_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=Path(tmp)/'deny';policy.write_text('asthma\n23193287\nPMC10767826\n10\\.1093\n')
            os.environ['ICS_DISCLOSURE_DENY_FILE']=str(policy)
            r=self.replay()
            cases=[('literature_search',dict(source='pubmed',query='asthma[MeSH Terms]',query_mode='provider',target='provider')),
                ('literature_get',dict(source='pubmed',identifier='DOI:10.1093/nar/gks1195')),
                ('literature_links',dict(source='pubmed',identifier='23193287',relation='similar')),
                ('literature_read',dict(source='europe_pmc',identifier='PMC10767826'))]
            for name,args in cases:
                out=call(name,**args);self.failure(out,'disclosure_blocked');self.assertEqual(out['disclosure']['outbound_calls'],0)
            self.assertFalse(r.calls)
    def test_pubmed_link_directions_and_pagination(self):
        for relation,fixture in [('similar','pubmed_pubmed'),('references','pubmed_pubmed_refs'),('citing','pubmed_pubmed_citedin')]:
            with self.subTest(relation=relation):
                self.replay(captured(fixture),captured(fixture))
                args=dict(source='pubmed',identifier='23193287',relation=relation,limit=2)
                r=call('literature_links',**args);self.assertEqual(r['state'],'success',r)
                self.assertEqual(len(r['relations']),2);self.assertFalse(r['coverage']['exhaustive'])
                self.assertEqual(r['provenance']['link_name'],fixture)
                r2=call('literature_links',**args,continuation=r['continuation']['token'])
                self.assertNotEqual(r['relations'][0]['target']['identity'],r2['relations'][0]['target']['identity'])
                self.assertEqual(r2['relations'][0]['provider_position'],2)
    def test_links_reject_stale_and_changed_bindings(self):
        body=captured('pubmed_pubmed_refs')
        r=self.replay(body,response(body.body.replace(b'23193264',b'99999999'),ctype='text/xml'))
        args=dict(source='pubmed',identifier='23193287',relation='references',limit=2)
        first=call('literature_links',**args)
        token=first['continuation']['token']
        self.failure(call('literature_links',**{**args,'relation':'citing'},continuation=token),'invalid_handle')
        self.assertEqual(len(r.calls),1)
        self.failure(call('literature_links',**args,continuation=token),'snapshot_drift')
    def test_europe_citations_and_maintenance(self):
        self.replay(captured('epmc_37994696_citations'))
        r=call('literature_links',source='europe_pmc',identifier='MED:37994696',relation='citing',limit=2)
        self.assertEqual(r['state'],'success',r);self.assertEqual(r['relations'][0]['target']['identity']['value'],'42363751')
        self.replay(captured('epmc_37994696_references'))
        r=call('literature_links',source='europe_pmc',identifier='MED:37994696',relation='references',limit=2)
        self.failure(r,'provider_server_error');self.assertTrue(r['failures'][0]['retryable'])
    def test_europe_relation_echo(self):
        payload=json.loads(captured('epmc_37994696_citations').body);payload['request']['offSet']=2
        self.replay(response(payload))
        self.failure(call('literature_links',source='europe_pmc',identifier='37994696',relation='citing',limit=2),'query_fidelity_unknown')
    def test_europe_citation_later_pages_from_live_capture(self):
        replay=self.replay(captured('epmc_37994696_citations'),
            captured('epmc_37994696_citations_page2'),captured('epmc_37994696_citations_page3'))
        args=dict(source='europe_pmc',identifier='MED:37994696',relation='citing',limit=2)
        result=call('literature_links',**args)
        identities=[]
        for page in (1,2,3):
            if page>1:result=call('literature_links',**args,continuation=result['continuation']['token'])
            self.assertEqual(result['state'],'success',result)
            self.assertEqual(result['relations'][0]['provider_position'],(page-1)*2)
            self.assertEqual(replay.calls[-1][2]['params']['page'],page)
            identities.extend(x['target']['identity']['value'] for x in result['relations'])
        self.assertEqual(len(set(identities)),6)
        self.assertEqual(identities[2:4],['PPR1214362','PPR1214837'])
        self.assertEqual(identities[4:6],['41868252','41695260'])
    def test_article_outline_section_table(self):
        self.replay(*[captured('epmc_article') for _ in range(3)])
        args=dict(source='europe_pmc',identifier='PMC10767826')
        outline=call('literature_read',**args)
        self.assertEqual(outline['state'],'success',outline)
        self.assertEqual(len(outline['tables']),4)
        self.assertIn('Creative Commons',outline['access']['licenses'][0]['text'])
        section=call('literature_read',**args,view='section',locator=outline['sections'][0]['locator'],expected_content_hash=outline['content_hash'])
        self.assertTrue(section['content']['blocks'])
        table=call('literature_read',**args,view='table',locator='table:1',expected_content_hash=outline['content_hash'])
        self.assertEqual(table['content']['rows'][0]['cells'][2]['colspan'],3)
        self.assertIn('preprint',table['content']['caption'])
        self.assertEqual(table['content']['rows'][0]['group'],'thead')
        self.assertFalse(table['content']['truncated'])
    def test_article_hash_identity_and_access(self):
        args=dict(source='europe_pmc',identifier='PMC10767826')
        self.replay(captured('epmc_article'))
        self.failure(call('literature_read',**args,view='section',locator='section:1',expected_content_hash='0'*64),'stale_content_handle')
        self.replay(captured('epmc_article'))
        self.failure(call('literature_read',source='europe_pmc',identifier='PMC1234'),'identity_mismatch')
        self.replay(response(b'not available',404,'text/plain'))
        r=call('literature_read',**args);self.failure(r,'provider_not_found');self.assertEqual(r['access']['state'],'unavailable')
        root=ET.fromstring(captured('epmc_article').body);m=root.find('front/article-meta');m.remove(m.find('permissions'))
        self.replay(response(ET.tostring(root),ctype='application/xml'))
        self.failure(call('literature_read',**args),'license_unknown')
    def test_xml_safety_and_limits(self):
        bodies=[b'<!DOCTYPE article [<!ENTITY x "xx">]><article>&x;</article>',b'<article>\0</article>',b'<article>'+b'<p>'*100+b'</p>'*100+b'</article>',b'<article><p></article>']
        for body in bodies:
            with self.subTest(body=body[:30]):
                with self.assertRaises(DiscoveryError):xml_body(body,'article')
    def test_article_table_and_text_boundaries(self):
        from article import table_value,section_value
        table=ET.fromstring('<table-wrap><caption><p>Units: mg</p></caption><table><thead><tr><th rowspan="2">Dose</th><th>Result</th></tr></thead><tbody><tr><td>3</td><td>4*</td></tr></tbody></table><table-wrap-foot><fn><p>* dry basis</p></fn></table-wrap-foot></table-wrap>')
        out=table_value(table);self.assertEqual(out['rows'][0]['cells'][0]['rowspan'],2);self.assertIn('dry basis',out['footnotes']);self.assertEqual(out['caption'],'Units: mg')
        table.find('table/tbody/tr/td').text='x'*40001
        with self.assertRaises(DiscoveryError):table_value(table)
        section=ET.fromstring('<sec><title>Method</title><p>'+'x'*25000+'</p></sec>')
        out=section_value(section,{});self.assertTrue(out['truncated']);self.assertLessEqual(sum(len(b['text']) for b in out['blocks']),24000)
    def test_xml_encoding_failure_keeps_mcp_envelope(self):
        for encoding in ('not-a-real-encoding','shift_jis'):
            with self.subTest(encoding=encoding):
                body=f'<?xml version="1.0" encoding="{encoding}"?><article/>'.encode()
                replay=self.replay(response(body,ctype='application/xml'))
                result=call('literature_read',source='europe_pmc',identifier='PMC10767826')
                self.failure(result,'provider_schema_drift')
                self.assertEqual(result['disclosure']['outbound_calls'],1)
                self.assertEqual(len(replay.calls),1)
    def test_section_notation_is_preserved(self):
        from article import section_value
        node=ET.fromstring('<sec><p>Dose 10<sup>3</sup>; fraction 10<sup>-3</sup>; H<sub>2</sub>O; <inline-formula><math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mi>a</mi><mi>b</mi></mfrac></math></inline-formula>.</p></sec>')
        out=section_value(node,{})
        value=out['blocks'][0]['text']
        self.assertIn('10^(3)',value);self.assertIn('10^(-3)',value);self.assertIn('H_(2)O',value)
        self.assertIn('mfrac',value);self.assertFalse(out['truncated'])
    def test_metadata_bounds_cover_keys_state_and_numbers(self):
        from metadata import europe_metadata
        for value in [{'x'*50000:1},{'state':'x'*50000},{'number':10**3000}, {'items':['\u0001'*2000]*100}]:
            out=europe_metadata({'grantsList':value},['funding'])
            self.assertLess(len(json.dumps(out)),12500)
            self.assertIn(out['funding']['state'],{'represented','not_represented','unknown','not_requested'})
        self.assertTrue(europe_metadata({'grantsList':{'x'*50000:1}},['funding'])['funding']['truncated'])
    def test_deep_and_wide_metadata_charge_every_emitted_value(self):
        from metadata import europe_metadata
        deep=[['x']*100 for _ in range(100)]
        for _ in range(11):deep=[deep]
        wide=[0]*100
        for _ in range(3):wide=[wide]*10
        for value in (deep,wide):
            result=europe_metadata({'grantsList':value},['funding'])['funding']
            self.assertTrue(result['truncated'])
            self.assertLessEqual(len(json.dumps(result['value'],ensure_ascii=True)),12000)
            self.assertLess(len(json.dumps(result)),12500)
    def test_short_relation_page_keeps_rows_without_invalid_token(self):
        payload=json.loads(captured('epmc_37994696_citations').body)
        payload['hitCount']=3;payload['citationList']['citation']=payload['citationList']['citation'][:1]
        self.replay(response(payload))
        r=call('literature_links',source='europe_pmc',identifier='37994696',relation='citing',limit=2)
        self.assertEqual(r['state'],'partial_success',r)
        self.assertEqual(len(r['relations']),1);self.assertFalse(r['continuation']['available'])
        self.assertFalse(r['coverage']['represented_set_exhausted'])
        payload['hitCount']=1;self.replay(response(payload))
        r=call('literature_links',source='europe_pmc',identifier='37994696',relation='citing',limit=2)
        self.assertEqual(r['state'],'success');self.assertTrue(r['coverage']['represented_set_exhausted'])
    def test_malformed_relation_identity(self):
        payload=json.loads(captured('epmc_37994696_citations').body)
        payload['citationList']['citation'][0]['id']='not-a-pmid'
        self.replay(response(payload))
        self.failure(call('literature_links',source='europe_pmc',identifier='37994696',relation='citing',limit=2),'identity_mismatch')
    def test_unsupported_and_bad_selection_no_call(self):
        r=self.replay()
        self.failure(call('literature_links',source='europe_pmc',identifier='37994696',relation='similar'),'unsupported_combination')
        self.failure(call('literature_read',source='pubmed',identifier='23193287'),'unsupported_combination')
        self.failure(call('literature_read',source='europe_pmc',identifier='PMC10767826',view='table',locator='table:1'),'invalid_request')
        self.assertFalse(r.calls)

if __name__=='__main__':unittest.main(verbosity=2)
