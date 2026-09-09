"""Selective JATS XML reading with representation-bound locators and intact tables."""
from __future__ import annotations
import hashlib
import json
import re
from xml.etree import ElementTree as ET

from contracts import normalize_doi
from discovery import DiscoveryError, PMCID, fetch, xml_body

MAX_TEXT = 24_000
MAX_TABLE_CHARS = 40_000

def text(node):
    """Readable text without concatenating exponents, blocks or MathML operands."""
    if node is None:
        return ""
    def render(element):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "math":
            # Retain the mathematical representation, rather than invent a plain
            # text interpretation for fractions, matrices and presentation markup.
            return "[MathML: " + ET.tostring(element, encoding="unicode") + "]"
        value = element.text or ""
        for child in element:
            value += render(child) + (child.tail or "")
        if tag == "sup": return "^(" + value + ")"
        if tag == "sub": return "_(" + value + ")"
        if tag == "tex-math": return "[TeX: " + value + "]"
        if tag in {"p", "title", "label", "fn", "list-item", "break", "tr", "td", "th"}:
            return " " + value + " "
        return value
    return " ".join(render(node).split())

def article_index(root):
    sections, tables = [], []
    elements = {}
    def walk(node, parent=None, in_table=False, in_front=False):
        tag = node.tag
        if tag == "table-wrap":
            loc = f"table:{len(tables)+1}"
            tables.append({"locator":loc, "source_id":node.get("id"),
                           "label":text(node.find("label"))[:200],
                           "caption":text(node.find("caption"))[:2000],
                           "caption_truncated":len(text(node.find("caption")))>2000,
                           "section":parent})
            elements[loc] = node
            in_table=True
        if not in_table and tag in {"sec", "abstract", "ack", "ref-list"}:
            loc=f"section:{len(sections)+1}"
            title=text(node.find("title")) or {"abstract":"Abstract","ack":"Acknowledgments","ref-list":"References"}.get(tag,"Untitled section")
            sections.append({"locator":loc,"source_id":node.get("id"),"kind":tag,
                             "title":title[:1000],"title_truncated":len(title)>1000,"parent":parent})
            elements[loc]=node
            parent=loc
        for child in node: walk(child,parent,in_table,in_front or tag=="front")
    # Only article front/body/back are selected; not sub-articles or responses.
    for name in ("front", "body", "back"):
        node=root.find(name)
        if node is not None: walk(node)
    if len(sections)>400 or len(tables)>200:
        raise DiscoveryError("parser_limit_exceeded","Article outline exceeds the section/table count limit")
    return sections,tables,elements

def table_value(wrapper):
    tables=wrapper.findall("table")
    if len(tables)!=1:
        raise DiscoveryError("unsupported_representation","Table is not represented as one supported XML table")
    table=tables[0]
    rows=[]; cells_total=0
    for group in table:
        if group.tag in {"thead","tbody","tfoot"}: source_rows=list(group); group_name=group.tag
        elif group.tag=="tr": source_rows=[group]; group_name="table"
        elif group.tag in {"colgroup", "col"}: continue
        else: raise DiscoveryError("unsupported_representation","Table contains an unsupported structural element")
        for row in source_rows:
            if row.tag!="tr": raise DiscoveryError("unsupported_representation","Unsupported table row structure")
            cells=[]
            for cell in row:
                if cell.tag not in {"th","td"} or cell.find(".//table") is not None:
                    raise DiscoveryError("unsupported_representation","Unsupported or nested table cell")
                spans={}
                for name in ("rowspan","colspan"):
                    raw=cell.get(name,"1")
                    if not re.fullmatch(r"[0-9]{1,3}",raw) or not 1<=int(raw)<=200:
                        raise DiscoveryError("unsupported_representation","Invalid table span")
                    spans[name]=int(raw)
                cells.append({"kind":cell.tag,"text":text(cell),
                              "inline_xml":ET.tostring(cell,encoding="unicode"), **spans,
                              **{k:cell.get(k) for k in ("id","headers","scope","align") if cell.get(k) is not None}})
                cells_total+=1
            rows.append({"group":group_name,"cells":cells})
    value={"source_id":wrapper.get("id"),"label":text(wrapper.find("label")),
           "caption":text(wrapper.find("caption")),"rows":rows,
           "footnotes":text(wrapper.find("table-wrap-foot")),
           "footnotes_xml":ET.tostring(wrapper.find("table-wrap-foot"),encoding="unicode") if wrapper.find("table-wrap-foot") is not None else None,
           "caption_xml":ET.tostring(wrapper.find("caption"),encoding="unicode") if wrapper.find("caption") is not None else None,
           "truncated":False,
           "layout":"Rows retain source order, header/body/footer groups and cell spans; span cells are not duplicated."}
    if len(rows)>200 or cells_total>4000 or len(json.dumps(value,ensure_ascii=False))>MAX_TABLE_CHARS:
        raise DiscoveryError("projection_incomplete","Whole table exceeds the reading limit; no partial table is represented")
    return value

def section_value(node, elements):
    locators={id(el):loc for loc,el in elements.items()}
    blocks=[]; used=0; truncated=False
    def append(kind,value,**extra):
        nonlocal used,truncated
        room=MAX_TEXT-used
        if len(blocks)>=200 or room<=0:
            truncated=True;return
        truncated |= len(value)>room
        blocks.append({"kind":kind,"text":value[:room],**extra})
        used+=len(value[:room])
    def walk(el, top=False):
        if el.tag=="table-wrap":
            append("table_reference",text(el.find("label")),locator=locators.get(id(el)))
        elif el.tag in {"p","title","label","ref","list-item","disp-formula"}:
            # A paragraph can itself contain tables: avoid flattening those cells.
            if el.find(".//table-wrap") is not None:
                if el.text and el.text.strip(): append("text",el.text.strip())
                for child in el:
                    walk(child)
                    if child.tail and child.tail.strip(): append("text",child.tail.strip())
            else: append(el.tag,text(el))
        elif el.tag=="fig":
            append("figure_caption",text(el.find("caption")),source_id=el.get("id"))
        else:
            if el.text and el.text.strip(): append("text",el.text.strip())
            for child in el:
                walk(child)
                if child.tail and child.tail.strip(): append("text",child.tail.strip())
    walk(node,True)
    return {"blocks":blocks,"truncated":truncated,"character_limit":MAX_TEXT,
            "scope":"Selected section including descendant text; tables require separate table reads. Figure images are not retrieved."}

async def read_article(http, *, identifier, view, locator=None, expected_content_hash=None):
    pmcid=identifier.strip().upper()
    if not PMCID.fullmatch(pmcid):
        raise DiscoveryError("invalid_identifier","Article reading requires a PMCID from literature_get",stage="validation")
    result=await fetch(http,"europe_pmc",pmcid+"/fullTextXML",xml=True,max_bytes=4*1024*1024)
    if "xml" not in result.content_type.lower():
        raise DiscoveryError("unsupported_representation","Full text was not returned as XML")
    root=xml_body(result.body,"article")
    article_meta=root.find("front/article-meta")
    if article_meta is None:
        raise DiscoveryError("provider_schema_drift","Article XML omitted article metadata")
    identities=[]
    for node in article_meta.findall("article-id"):
        kind=node.get("pub-id-type"); value=text(node)
        if kind in {"pmcid","pmid","doi"}:
            if kind=="doi":
                try: value=normalize_doi(value)
                except ValueError: raise DiscoveryError("identity_mismatch","Article has a malformed DOI") from None
            identities.append({"kind":kind,"value":value})
    represented={i['value'].upper() for i in identities if i['kind']=='pmcid'}
    # Older JATS uses pub-id-type=pmc with a numeric accession.
    for n in article_meta.findall("article-id"):
        if n.get("pub-id-type")=="pmc" and re.fullmatch(r"[0-9]{1,12}",text(n)):
            represented.add("PMC"+text(n))
    if represented != {pmcid}:
        raise DiscoveryError("identity_mismatch","Article XML did not identify exactly the requested PMCID",stage="identity_or_query_fidelity")
    digest=hashlib.sha256(result.body).hexdigest()
    if expected_content_hash is not None and digest!=expected_content_hash:
        raise DiscoveryError("stale_content_handle","Article XML changed; retrieve a new outline before using a locator",stage="currentness")
    licenses=[]
    for n in article_meta.findall("permissions/license"):
        links=[v for el in n.iter() for k,v in el.attrib.items() if k.endswith("}href") or k=="href"]
        statement=text(n)
        licenses.append({"text":statement[:4000],"truncated":len(statement)>4000,"links":links[:20]})
    if not licenses:
        raise DiscoveryError("license_unknown","Article XML does not represent a license; selective text reading is not qualified",stage="projection_or_selection")
    sections,tables,elements=article_index(root)
    output={"identity":{"kind":"pmcid","value":pmcid},"identifiers":identities,
            "title":text(article_meta.find("title-group/article-title"))[:2000],
            "content_hash":digest,"hash_basis":"SHA-256 of retrieved XML bytes; not an immutable publication version",
            "access":{"state":"available","representation":"Europe PMC fullTextXML","licenses":licenses,
                      "reuse_scope":"Apply the represented license to your intended use; accessibility alone is not permission."},
            "record_url":f"https://europepmc.org/articles/{pmcid}","view":view}
    if view=="outline": output.update(sections=sections,tables=tables)
    else:
        node=elements.get(locator)
        expected_prefix="section:" if view=="section" else "table:"
        if node is None or not locator.startswith(expected_prefix):
            raise DiscoveryError("selection_not_represented","Locator is not present for this view and representation",stage="projection_or_selection")
        output["locator"]=locator
        output["content"]=section_value(node,elements) if view=="section" else table_value(node)
    return output
