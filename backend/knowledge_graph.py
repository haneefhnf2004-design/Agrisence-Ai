"""
Knowledge Graph engine for Nexora.
Uses NetworkX for in-memory graph storage with JSON persistence.
"""
import json
import os
import re
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False


_COMMON_WORDS = {
    'The', 'A', 'An', 'In', 'On', 'At', 'By', 'For', 'With', 'From',
    'To', 'Of', 'And', 'Or', 'Is', 'Are', 'Was', 'Were', 'Be', 'Been',
    'Has', 'Have', 'Had', 'Do', 'Does', 'Did', 'Will', 'Would', 'Could',
    'Should', 'May', 'Might', 'Can', 'This', 'That', 'These', 'Those',
    'It', 'Its', 'He', 'She', 'They', 'We', 'You', 'My', 'Your', 'Our',
    'Their', 'His', 'Her', 'As', 'So', 'But', 'Not', 'No', 'If', 'Up',
}

_RELATION_PATTERNS = [
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+is\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'is'),
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+uses\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'uses'),
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+contains\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'contains'),
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+has\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'has'),
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+includes\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'includes'),
    (r'\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\s+part of\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*)\b', 'part_of'),
]


class KnowledgeGraph:
    def __init__(self):
        self._graph_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'knowledge_graph.json')
        self._available = _NX_AVAILABLE
        if _NX_AVAILABLE:
            self.G = nx.DiGraph()
        else:
            self.G = None
            print("[KG] networkx not installed — knowledge graph disabled.")
            return
        if os.path.exists(self._graph_path):
            self._load()

    def _load(self):
        """Load graph from JSON file."""
        try:
            with open(self._graph_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for n in data.get('nodes', []):
                node_id = n['id']
                attrs = {k: v for k, v in n.items() if k != 'id'}
                self.G.add_node(node_id, **attrs)
            for e in data.get('edges', []):
                from_id = e['from']
                to_id = e['to']
                attrs = {k: v for k, v in e.items() if k not in ('from', 'to')}
                self.G.add_edge(from_id, to_id, **attrs)
            print(f"[KG] Loaded {len(self.G.nodes())} nodes, {len(self.G.edges())} edges.")
        except Exception as ex:
            print(f"[KG] _load error: {ex}")

    def _save(self):
        """Persist graph to JSON file."""
        if not self._available:
            return
        try:
            nodes = [{'id': n, **self.G.nodes[n]} for n in self.G.nodes()]
            edges = [{'from': u, 'to': v, **self.G.edges[u, v]} for u, v in self.G.edges()]
            with open(self._graph_path, 'w', encoding='utf-8') as f:
                json.dump({'nodes': nodes, 'edges': edges}, f, indent=2, ensure_ascii=False)
        except Exception as ex:
            print(f"[KG] _save error: {ex}")

    def add_entity(self, name: str, entity_type: str, properties: dict = None) -> str:
        """Add a named entity node and return its id."""
        if not self._available:
            return ''
        node_id = uuid.uuid4().hex
        props = properties or {}
        self.G.add_node(
            node_id,
            name=name,
            type=entity_type,
            properties=props,
            created_at=datetime.utcnow().isoformat(),
        )
        self._save()
        return node_id

    def add_relationship(self, from_id: str, to_id: str, relation_type: str, properties: dict = None) -> str:
        """Add a directed edge between two entity nodes and return an edge id."""
        if not self._available:
            return ''
        if from_id not in self.G or to_id not in self.G:
            return ''
        props = properties or {}
        self.G.add_edge(
            from_id,
            to_id,
            relation_type=relation_type,
            properties=props,
            created_at=datetime.utcnow().isoformat(),
        )
        self._save()
        return f"{from_id}-{to_id}"

    def extract_and_add(self, text: str, source: str):
        """
        Extract capitalised multi-word phrases from text as concept entities,
        then look for simple relational patterns (X is Y, X uses Y, etc.) and
        add edges between matching nodes.
        """
        if not self._available:
            return

        # --- entity extraction ---
        pattern = r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b'
        candidates = re.findall(pattern, text)

        # Deduplicate, filter by length and word count
        seen = set()
        new_ids: Dict[str, str] = {}  # phrase -> node_id
        for phrase in candidates:
            if phrase in seen:
                continue
            seen.add(phrase)
            words = phrase.split()
            if len(phrase) < 3 or len(words) > 5:
                continue
            if phrase in _COMMON_WORDS:
                continue
            # Skip pure single common words
            if len(words) == 1 and phrase in _COMMON_WORDS:
                continue
            # Check if entity with same name already exists
            existing = self._find_node_by_name(phrase)
            if existing:
                new_ids[phrase] = existing
            else:
                node_id = self.add_entity(phrase, 'concept', {'source': source})
                new_ids[phrase] = node_id

        # --- relationship extraction ---
        for rel_pattern, rel_type in _RELATION_PATTERNS:
            for match in re.finditer(rel_pattern, text):
                subj = match.group(1).strip()
                obj = match.group(2).strip()
                if subj in new_ids and obj in new_ids:
                    from_id = new_ids[subj]
                    to_id = new_ids[obj]
                    if from_id and to_id and from_id != to_id:
                        # Avoid duplicate edges
                        if not self.G.has_edge(from_id, to_id):
                            self.add_relationship(from_id, to_id, rel_type, {'source': source})

    def _find_node_by_name(self, name: str) -> Optional[str]:
        """Return the node_id for the first node with this name, or None."""
        if not self._available:
            return None
        for node_id, attrs in self.G.nodes(data=True):
            if attrs.get('name', '').lower() == name.lower():
                return node_id
        return None

    def search_graph(self, query: str) -> list:
        """Find nodes where query words appear in name or properties."""
        if not self._available:
            return []
        try:
            query_words = query.lower().split()
            results = []
            for node_id, attrs in self.G.nodes(data=True):
                name = str(attrs.get('name', '')).lower()
                props_str = json.dumps(attrs.get('properties', {})).lower()
                if any(w in name or w in props_str for w in query_words):
                    neighbors = list(self.G.successors(node_id)) + list(self.G.predecessors(node_id))
                    neighbor_data = []
                    for nb in neighbors[:3]:
                        nb_attrs = self.G.nodes[nb]
                        neighbor_data.append({'id': nb, 'name': nb_attrs.get('name', ''), 'type': nb_attrs.get('type', '')})
                    results.append({
                        'id': node_id,
                        'name': attrs.get('name', ''),
                        'type': attrs.get('type', ''),
                        'properties': attrs.get('properties', {}),
                        'created_at': attrs.get('created_at', ''),
                        'neighbors': neighbor_data,
                    })
            return results
        except Exception as ex:
            print(f"[KG] search_graph error: {ex}")
            return []

    def get_entity(self, node_id: str) -> Optional[dict]:
        """Return node data dict or None if the node doesn't exist."""
        if not self._available:
            return None
        if node_id not in self.G:
            return None
        attrs = self.G.nodes[node_id]
        return {'id': node_id, **attrs}

    def delete_entity(self, node_id: str):
        """Remove a node and all its edges from the graph, then save."""
        if not self._available:
            return
        if node_id in self.G:
            self.G.remove_node(node_id)
            self._save()

    def get_all_entities(self, limit: int = 50) -> list:
        """Return the first N nodes as dicts with id, name, type, and degree."""
        if not self._available:
            return []
        result = []
        for node_id in list(self.G.nodes())[:limit]:
            attrs = self.G.nodes[node_id]
            degree = len(list(self.G.neighbors(node_id)))
            result.append({
                'id': node_id,
                'name': attrs.get('name', ''),
                'type': attrs.get('type', ''),
                'degree': degree,
                'properties': attrs.get('properties', {}),
                'created_at': attrs.get('created_at', ''),
            })
        return result

    def get_relationships(self, node_id: str) -> list:
        """Return edge dicts for all edges touching this node (in and out)."""
        if not self._available:
            return []
        edges = []
        for u, v, data in self.G.out_edges(node_id, data=True):
            edges.append({'from': u, 'to': v, **data})
        for u, v, data in self.G.in_edges(node_id, data=True):
            if u != node_id:  # avoid duplicating self-loops
                edges.append({'from': u, 'to': v, **data})
        return edges

    def stats(self) -> dict:
        """Return basic graph statistics."""
        if not self._available:
            return {'node_count': 0, 'edge_count': 0, 'available': False}
        return {
            'node_count': len(self.G.nodes()),
            'edge_count': len(self.G.edges()),
            'available': True,
        }
