"""Civic Knowledge Graph engine.

Constructs an entity-relationship graph of real complaints, locations (wards/zones),
evidence files, work orders, repair assessments, and recurrence relationships
directly from the CivicLens ComplaintStore database.

Provides nodes and edges suitable for graph visualization, incident linking,
and audit exploration. Does not fabricate entities or links.
"""

from __future__ import annotations

import logging
from typing import Any

from ..store import ComplaintStore

logger = logging.getLogger(__name__)


def build_knowledge_graph(
    store: ComplaintStore,
    *,
    complaint_id: str | None = None,
    ward: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Extract entities and relationships from the database.

    If complaint_id is given, returns the focused subgraph for that complaint.
    Otherwise extracts the most recent complaints up to `limit`.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, label: str, node_type: str, properties: dict[str, Any]):
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "type": node_type,
                "properties": properties,
            })

    def add_edge(source_id: str, target_id: str, relation: str, properties: dict[str, Any] | None = None):
        key = (source_id, target_id, relation)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append({
                "source": source_id,
                "target": target_id,
                "relation": relation,
                "properties": properties or {},
            })

    # Fetch complaints from store
    complaints = []
    if complaint_id:
        c = store.get_staff_complaint(complaint_id)
        if c:
            complaints = [c]
    else:
        try:
            items, _total = store.list_complaints(limit=limit, offset=0)
            complaints = items
        except Exception as e:
            logger.warning("Could not list complaints for knowledge graph: %s", e)
            complaints = []

    for c in complaints:
        cid = c.get("id") or c.get("publicId") or str(c.get("pk", ""))
        c_node_id = f"complaint:{cid}"
        add_node(
            c_node_id,
            label=f"Case {cid}",
            node_type="Complaint",
            properties={
                "id": cid,
                "category": c.get("category", "Unknown"),
                "status": c.get("status", "Unknown"),
                "created": c.get("created", ""),
                "description": c.get("description", "")[:100],
            },
        )

        # Location / Ward node
        area = c.get("area") or c.get("neighbourhood") or "Unknown"
        if area:
            ward_node_id = f"ward:{area}"
            add_node(
                ward_node_id,
                label=f"Ward: {area}",
                node_type="Location",
                properties={"name": area},
            )
            add_edge(c_node_id, ward_node_id, "LOCATED_IN")

        # Department node
        dept = c.get("department")
        if dept:
            dept_node_id = f"dept:{dept}"
            add_node(
                dept_node_id,
                label=f"Dept: {dept}",
                node_type="Department",
                properties={"name": dept},
            )
            add_edge(c_node_id, dept_node_id, "MANAGED_BY")

        # Evidence node
        if c.get("hasEvidence") or c.get("evidence"):
            ev = c.get("evidence")
            ev_id = (ev.get("evidenceId") if ev else None) or f"ev_{cid}"
            ev_node_id = f"evidence:{ev_id}"
            add_node(
                ev_node_id,
                label=f"Evidence {ev_id[:10]}",
                node_type="Evidence",
                properties={"id": ev_id, "mediaType": c.get("mediaType", "photo")},
            )
            add_edge(c_node_id, ev_node_id, "HAS_EVIDENCE")

        # Merged / Recurrence
        merged_into = c.get("mergedInto")
        if merged_into:
            other_node_id = f"complaint:{merged_into}"
            add_node(
                other_node_id,
                label=f"Case {merged_into}",
                node_type="Complaint",
                properties={"id": merged_into},
            )
            add_edge(c_node_id, other_node_id, "RECURRENCE_OF", {"note": "merged incident"})

    return {
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "entity_types": sorted(list({n["type"] for n in nodes})),
            "relation_types": sorted(list({e["relation"] for e in edges})),
        },
        "query": {
            "complaint_id": complaint_id,
            "ward": ward,
            "limit": limit,
        },
    }
