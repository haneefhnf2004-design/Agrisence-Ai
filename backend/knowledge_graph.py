"""
Knowledge Graph for Nexora (NetworkX MultiDiGraph, persisted to knowledge_graph.json).

It does NOT store a second copy of memories or documents. Nodes and edges keep
source references into the existing stores:
    memory:<id>              -> nexora_memory.db  (memory.py)
    doc:<filename>#<chunk>   -> nexora_knowledge.db (rag_engine.py)
plus a short evidence sentence on each edge so every fact can be explained.

Extraction is rule-based and explainable (no model call):
  entities  - capitalised words / phrases, CamelCase and acronyms (SQLite, RAG,
              Nexora V2); a capitalised word that only appears at the start of a
              sentence is ignored (that is just English capitalisation).
  relations - two entities in the same sentence joined by a known verb phrase,
              e.g. "Nexora uses SQLite" -> (Nexora) -uses-> (SQLite).
Accuracy of this extractor is measured by the evaluation suite (evaluation/), not assumed.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Optional, List, Dict

try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False

_STOP = {
    'the', 'a', 'an', 'in', 'on', 'at', 'by', 'for', 'with', 'from', 'to', 'of', 'and', 'or', 'is', 'are', 'was',
    'were', 'be', 'been', 'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may',
    'might', 'can', 'this', 'that', 'these', 'those', 'it', 'its', 'he', 'she', 'they', 'we', 'you', 'i', 'my',
    'your', 'our', 'their', 'his', 'her', 'as', 'so', 'but', 'not', 'no', 'if', 'up', 'then', 'there', 'here',
    'when', 'what', 'which', 'who', 'how', 'why', 'also', 'yes', 'ok', 'please', 'thanks', 'hello', 'hi', 'dear',
    'today', 'tomorrow', 'yesterday', 'user', 'remember', 'however', 'after', 'before', 'note', 'meanwhile',
    'first', 'next', 'finally', 'every', 'each', 'some', 'many', 'all', 'just', 'now', 'later', 'maybe', 'sure',
}

# verb phrase between two entities (lower-case, articles removed) -> relation type
RELATION_VERBS = {
    'is a': 'is_a', 'is an': 'is_a', 'is': 'is', 'are': 'is', 'was': 'is',
    'uses': 'uses', 'used': 'uses', 'is using': 'uses', 'relies on': 'depends_on', 'depends on': 'depends_on',
    'requires': 'depends_on', 'contains': 'contains', 'has': 'has', 'includes': 'includes',
    'is part of': 'part_of', 'part of': 'part_of', 'belongs to': 'part_of',
    'works at': 'works_at', 'works for': 'works_at', 'is employed by': 'works_at', 'works on': 'works_on',
    'created': 'created', 'built': 'created', 'developed': 'created', 'wrote': 'created', 'made': 'created',
    'created by': 'created_by', 'built by': 'created_by', 'developed by': 'created_by', 'written by': 'created_by',
    'made by': 'created_by', 'is created by': 'created_by', 'is built by': 'created_by', 'is developed by': 'created_by',
    'located in': 'located_in', 'is located in': 'located_in', 'is in': 'located_in', 'lives in': 'located_in',
    'is based in': 'located_in', 'based in': 'located_in',
    'manages': 'manages', 'leads': 'manages', 'reports to': 'reports_to', 'owns': 'owns', 'supports': 'supports',
    'replaced': 'replaced', 'replaces': 'replaced', 'studies at': 'studies_at', 'teaches': 'teaches',
    'connects to': 'connects_to', 'talks to': 'connects_to', 'sends': 'sends', 'stores': 'stores',
}

_ENTITY = re.compile(r"\b(?:[A-Z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)?)(?:\s+(?:[A-Z][A-Za-z0-9]*|V?\d+(?:\.\d+)?))*\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def extract_entities(sentence: str, mid_sentence_caps: set = None, lower_words: set = None) -> List[dict]:
    """Entity mentions with character spans, in order."""
    out = []
    for m in _ENTITY.finditer(sentence):
        phrase = m.group(0).strip()
        words = phrase.split()
        # trim leading stop-words captured because they were capitalised ("The Nexora App" -> "Nexora App")
        start = m.start()
        while words and words[0].lower() in _STOP:
            start += len(words[0]) + 1
            words = words[1:]
        if not words or len(words) > 6:
            continue
        phrase = " ".join(words)
        if len(phrase) < 2 or phrase.lower() in _STOP:
            continue
        single = len(words) == 1
        special = single and (sum(c.isupper() for c in phrase) > 1 or any(c.isdigit() for c in phrase))
        if single and start == 0 and not special and phrase not in (mid_sentence_caps or set())                 and phrase.lower() in (lower_words or set()):
            continue  # sentence-initial capital of a word the text also uses in lower case
        out.append({'name': phrase, 'start': start, 'end': start + len(phrase)})
    return out


def extract(text: str) -> Dict[str, list]:
    """Pure extraction: {'entities': [names], 'relations': [(subj, rel, obj, sentence)]}."""
    sents = split_sentences(text)
    mid_caps = set()
    for s in sents:
        for m in _ENTITY.finditer(s):
            if m.start() > 0:
                mid_caps.update(m.group(0).split())
    lower_words = set(re.findall(r"[a-z][a-z']+", text or ""))
    ents, rels = [], []
    for s in sents:
        found = extract_entities(s, mid_caps, lower_words)
        for e in found:
            if e['name'] not in ents:
                ents.append(e['name'])
        for a, b in zip(found, found[1:]):
            between = re.sub(r"\b(a|an|the)\s*$", "", s[a['end']:b['start']].strip().lower()).strip(" ,")
            between = re.sub(r"\s+", " ", between)
            rel = RELATION_VERBS.get(between)
            if rel is None and between.endswith((' a', ' an', ' the')):
                rel = RELATION_VERBS.get(between.rsplit(' ', 1)[0])
            if rel and a['name'] != b['name']:
                rels.append((a['name'], rel, b['name'], s[:200]))
    return {'entities': ents, 'relations': rels}


class KnowledgeGraph:
    def __init__(self, path: str = None):
        self._graph_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'knowledge_graph.json')
        self._available = _NX_AVAILABLE
        self._lock = threading.RLock()
        self.G = nx.MultiDiGraph() if _NX_AVAILABLE else None
        if not _NX_AVAILABLE:
            print("[KG] networkx not installed - knowledge graph disabled.")
            return
        if os.path.exists(self._graph_path):
            self._load()

    # ------------------------------------------------------------ storage ----
    def _load(self):
        try:
            with open(self._graph_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n in data.get('nodes', []):
                attrs = {k: v for k, v in n.items() if k != 'id'}
                src = (attrs.get('properties') or {}).get('source')
                attrs.setdefault('sources', [src] if src else [])
                self.G.add_node(n['id'], **attrs)
            for e in data.get('edges', []):
                attrs = {k: v for k, v in e.items() if k not in ('from', 'to')}
                src = (attrs.get('properties') or {}).get('source')
                attrs.setdefault('sources', [src] if src else [])
                attrs.setdefault('relation_type', 'related_to')
                if e['from'] in self.G and e['to'] in self.G:
                    self.G.add_edge(e['from'], e['to'], key=attrs['relation_type'], **attrs)
            print(f"[KG] Loaded {self.G.number_of_nodes()} nodes, {self.G.number_of_edges()} edges.")
        except Exception as ex:
            print(f"[KG] _load error: {ex}")

    def _save(self):
        if not self._available:
            return
        with self._lock:
            nodes = [{'id': n, **self.G.nodes[n]} for n in self.G.nodes()]
            edges = [{'from': u, 'to': v, **d} for u, v, d in self.G.edges(data=True)]
            tmp = self._graph_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': 2, 'nodes': nodes, 'edges': edges}, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self._graph_path)  # atomic: never leaves a half-written file

    # ------------------------------------------------------------ writes -----
    def _find_node_by_name(self, name: str) -> Optional[str]:
        key = (name or '').strip().lower()
        for node_id, attrs in self.G.nodes(data=True):
            if str(attrs.get('name', '')).lower() == key:
                return node_id
        return None

    def add_entity(self, name: str, entity_type: str = 'concept', properties: dict = None,
                   source: str = None, save: bool = True) -> str:
        """Add (or merge into) the entity with this name. Returns its id."""
        if not self._available:
            return ''
        with self._lock:
            nid = self._find_node_by_name(name)
            if nid:
                n = self.G.nodes[nid]
                if source and source not in n['sources']:
                    n['sources'].append(source)
                n['mentions'] = n.get('mentions', 1) + 1
                if properties:
                    n.setdefault('properties', {}).update(properties)
                if entity_type and entity_type != 'concept' and n.get('type') == 'concept':
                    n['type'] = entity_type
            else:
                nid = uuid.uuid4().hex
                self.G.add_node(nid, name=name.strip(), type=entity_type or 'concept', properties=properties or {},
                                sources=[source] if source else [], mentions=1, manual=source is None,
                                created_at=datetime.now().isoformat(timespec='seconds'))
            if save:
                self._save()
            return nid

    def add_relationship(self, from_id: str, to_id: str, relation_type: str, properties: dict = None,
                         source: str = None, evidence: str = None, save: bool = True) -> str:
        if not self._available or from_id not in self.G or to_id not in self.G or from_id == to_id:
            return ''
        with self._lock:
            if self.G.has_edge(from_id, to_id, key=relation_type):
                d = self.G.edges[from_id, to_id, relation_type]
                if source and source not in d['sources']:
                    d['sources'].append(source)
                if evidence and not d.get('evidence'):
                    d['evidence'] = evidence
            else:
                self.G.add_edge(from_id, to_id, key=relation_type, relation_type=relation_type,
                                properties=properties or {}, sources=[source] if source else [],
                                evidence=evidence or '', created_at=datetime.now().isoformat(timespec='seconds'))
            if save:
                self._save()
            return f"{from_id}-{relation_type}-{to_id}"

    def extract_and_add(self, text: str, source: str) -> dict:
        """Extract entities + relations from text and add them with `source` as reference."""
        if not self._available:
            return {'entities': [], 'relations': []}
        ex = extract(text)
        with self._lock:
            ids = {name: self.add_entity(name, 'concept', source=source, save=False) for name in ex['entities']}
            for subj, rel, obj, sent in ex['relations']:
                self.add_relationship(ids[subj], ids[obj], rel, source=source, evidence=sent, save=False)
            self._save()
        return {'entities': ex['entities'], 'relations': [(s, r, o) for s, r, o, _ in ex['relations']]}

    def remove_source(self, source: str, prefix: bool = False) -> dict:
        """Drop a source everywhere; auto-extracted items left with no source are removed.
        'doc:a.pdf' also removes 'doc:a.pdf#3'. prefix=True removes everything starting with `source`."""
        if not self._available:
            return {'nodes_removed': 0, 'edges_removed': 0}

        def gone(s):
            s = str(s)
            return s.startswith(source) if prefix else (s == source or s.startswith(source + '#'))
        with self._lock:
            edges_rm, nodes_rm = 0, 0
            for u, v, k, d in list(self.G.edges(keys=True, data=True)):
                before = d.get('sources', [])
                d['sources'] = [x for x in before if not gone(x)]
                if before and not d['sources']:
                    self.G.remove_edge(u, v, key=k)
                    edges_rm += 1
            for nid, d in list(self.G.nodes(data=True)):
                before = d.get('sources', [])
                d['sources'] = [x for x in before if not gone(x)]
                if before and not d['sources'] and not d.get('manual') and self.G.degree(nid) == 0:
                    self.G.remove_node(nid)
                    nodes_rm += 1
            self._save()
        return {'nodes_removed': nodes_rm, 'edges_removed': edges_rm}

    def delete_entity(self, node_id: str):
        if self._available and node_id in self.G:
            with self._lock:
                self.G.remove_node(node_id)
                self._save()

    # ------------------------------------------------------------ reads ------
    def _node_dict(self, nid):
        a = self.G.nodes[nid]
        return {'id': nid, 'name': a.get('name', ''), 'type': a.get('type', ''), 'properties': a.get('properties', {}),
                'sources': a.get('sources', []), 'created_at': a.get('created_at', ''), 'degree': self.G.degree(nid)}

    def get_entity(self, node_id: str) -> Optional[dict]:
        if not self._available or node_id not in self.G:
            return None
        return self._node_dict(node_id)

    def search_graph(self, query: str) -> list:
        """Entities whose name/properties contain any query word, with up to 5 neighbours."""
        if not self._available:
            return []
        words = [w for w in re.findall(r"\w+", (query or '').lower()) if w not in _STOP]
        results = []
        with self._lock:
            for nid, a in self.G.nodes(data=True):
                hay = (str(a.get('name', '')) + ' ' + json.dumps(a.get('properties', {}))).lower()
                score = sum(1 for w in words if w in hay)
                if score:
                    nbs = list(dict.fromkeys(list(self.G.successors(nid)) + list(self.G.predecessors(nid))))[:5]
                    results.append({**self._node_dict(nid), 'score': score,
                                    'neighbors': [{'id': n, 'name': self.G.nodes[n].get('name', ''),
                                                   'type': self.G.nodes[n].get('type', '')} for n in nbs]})
        return sorted(results, key=lambda r: (-r['score'], -r['degree']))

    def relations(self, name: str, relation_type: str = None) -> list:
        """All facts touching an entity: [{'subject','relation','object','sources','evidence'}]."""
        if not self._available:
            return []
        nid = self._find_node_by_name(name)
        if not nid:
            return []
        out = []
        with self._lock:
            for u, v, d in list(self.G.out_edges(nid, data=True)) + list(self.G.in_edges(nid, data=True)):
                if relation_type and d.get('relation_type') != relation_type:
                    continue
                out.append({'subject': self.G.nodes[u]['name'], 'relation': d.get('relation_type'),
                            'object': self.G.nodes[v]['name'], 'sources': d.get('sources', []),
                            'evidence': d.get('evidence', '')})
        return out

    def neighbors(self, name: str, depth: int = 1) -> list:
        if not self._available:
            return []
        nid = self._find_node_by_name(name)
        if not nid:
            return []
        und = self.G.to_undirected(as_view=True)
        lengths = nx.single_source_shortest_path_length(und, nid, cutoff=max(1, int(depth)))
        return [{**self._node_dict(n), 'distance': d} for n, d in sorted(lengths.items(), key=lambda x: x[1]) if n != nid]

    def find_path(self, a: str, b: str) -> dict:
        """Shortest connection between two entities (ignoring direction), with the relation on each hop."""
        if not self._available:
            return {'found': False, 'reason': 'graph unavailable'}
        na, nb = self._find_node_by_name(a), self._find_node_by_name(b)
        if not na or not nb:
            return {'found': False, 'reason': f"unknown entity: {a if not na else b}"}
        try:
            path = nx.shortest_path(self.G.to_undirected(as_view=True), na, nb)
        except nx.NetworkXNoPath:
            return {'found': False, 'reason': 'no connection'}
        hops = []
        for u, v in zip(path, path[1:]):
            if self.G.has_edge(u, v):
                rel, s, o = next(iter(self.G.get_edge_data(u, v).values())), u, v
            else:
                rel, s, o = next(iter(self.G.get_edge_data(v, u).values())), v, u
            hops.append({'subject': self.G.nodes[s]['name'], 'relation': rel['relation_type'],
                         'object': self.G.nodes[o]['name'], 'sources': rel.get('sources', [])})
        return {'found': True, 'path': [self.G.nodes[n]['name'] for n in path], 'hops': hops}

    def retrieve_context(self, query: str, limit: int = 10) -> dict:
        """Graph retrieval for the assistant: matching entities and their facts, each with source references."""
        hits = self.search_graph(query)[:5]
        facts, seen = [], set()
        for h in hits:
            for f in self.relations(h['name']):
                key = (f['subject'], f['relation'], f['object'])
                if key not in seen:
                    seen.add(key)
                    facts.append(f)
        return {'entities': [{'name': h['name'], 'type': h['type'], 'sources': h['sources']} for h in hits],
                'facts': facts[:limit],
                'text': "\n".join(f"{f['subject']} --{f['relation']}--> {f['object']} (source: {', '.join(f['sources']) or 'manual'})"
                                  for f in facts[:limit]) or "No graph facts found."}

    def get_all_entities(self, limit: int = 50) -> list:
        if not self._available:
            return []
        with self._lock:
            ids = sorted(self.G.nodes(), key=lambda n: -self.G.degree(n))[:limit]
            return [self._node_dict(n) for n in ids]

    def get_relationships(self, node_id: str) -> list:
        if not self._available or node_id not in self.G:
            return []
        return [{'from': u, 'to': v, **d} for u, v, d in
                list(self.G.out_edges(node_id, data=True)) + list(self.G.in_edges(node_id, data=True))]

    def stats(self) -> dict:
        if not self._available:
            return {'node_count': 0, 'edge_count': 0, 'available': False}
        rel_counts = {}
        for _, _, d in self.G.edges(data=True):
            rel_counts[d.get('relation_type')] = rel_counts.get(d.get('relation_type'), 0) + 1
        return {'node_count': self.G.number_of_nodes(), 'edge_count': self.G.number_of_edges(),
                'relation_types': rel_counts, 'available': True}


_singleton = None
_singleton_lock = threading.Lock()


def get_graph() -> KnowledgeGraph:
    """The one graph instance shared by memory.py, rag_engine.py, the server and the tools."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = KnowledgeGraph()
        return _singleton
