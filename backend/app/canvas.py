"""Validate the local canvas independently of the Agent's generation contracts."""
import json
import math
import re


def empty_canvas():
    return {"schema": 1, "title": "Untitled canvas", "nodes": [], "edges": [], "viewport": {"x": 160, "y": 140, "zoom": 1}}


def validate_canvas(raw, normalise_assets):
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ValueError("Unsupported canvas file")
    if len(json.dumps(raw).encode()) > 32 * 1024 * 1024:
        raise ValueError("Canvas exceeds 32 MB")
    nodes, edges = raw.get("nodes"), raw.get("edges")
    if not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(edges, list) or len(edges) > 250:
        raise ValueError("Canvas supports up to 100 nodes and 250 connections")

    def text(value, limit):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("Invalid canvas text")
        return value

    def number(value, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid canvas coordinates")
        return value

    clean, ids = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Invalid canvas node")
        ident = node.get("id")
        kind = node.get("kind")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident) or ident in ids or kind not in {"text", "note", "image", "image_task", "audio", "video", "task", "output"}:
            raise ValueError("Invalid canvas node identity")
        ids.add(ident)
        item = {"id": ident, "kind": kind, "x": number(node.get("x"), -100000, 100000), "y": number(node.get("y"), -100000, 100000),
                "title": text(node.get("title", ""), 120), "text": text(node.get("text", ""), 12000)}
        for key in ('image_run_id','image_result_id'):
            if key in node:
                if not isinstance(node[key],str) or not re.fullmatch(r'[a-f0-9]{32}',node[key]):raise ValueError('Invalid image result identity')
                item[key]=node[key]
        if kind in {"image", "audio", "video"}:
            raw_asset = node.get("asset")
            if not isinstance(raw_asset, dict):
                raise ValueError("Canvas media is missing")
            asset = normalise_assets({"multimodal_assets": [{**raw_asset, "data_url": raw_asset.get("data_url") or raw_asset.get("preview_data_url")}]})[0]
            if asset["media_type"] != kind:
                raise ValueError("Canvas media type does not match its node")
            item["asset"] = asset
        elif kind == "output":
            version = node.get("version_id", "")
            if not isinstance(version, str) or not re.fullmatch(r"v[1-9][0-9]*", version):
                raise ValueError("Invalid video version")
            item.update(project_id=text(node.get("project_id", ""), 120), version_id=version)
        clean.append(item)
    connections, pairs = [], set()
    adjacency = {ident: [] for ident in ids}
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("Invalid canvas connection")
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or source not in ids or target not in ids or source == target or (source, target) in pairs:
            raise ValueError("Invalid canvas connection")
        pairs.add((source, target))
        adjacency[source].append(target)
        connections.append({"source": source, "target": target})
    visiting, visited = set(), set()

    def visit(ident):
        if ident in visiting:
            raise ValueError("Canvas connections cannot form a cycle")
        if ident in visited:
            return
        visiting.add(ident)
        for child in adjacency[ident]:
            visit(child)
        visiting.remove(ident)
        visited.add(ident)
    for ident in ids:
        visit(ident)
    viewport = raw.get("viewport", {})
    if not isinstance(viewport, dict):
        raise ValueError("Invalid canvas viewport")
    return {"schema": 1, "title": text(raw.get("title", "Untitled canvas"), 120), "nodes": clean, "edges": connections,
            "viewport": {"x": number(viewport.get("x"), -200000, 200000), "y": number(viewport.get("y"), -200000, 200000), "zoom": number(viewport.get("zoom"), .2, 2)}}
