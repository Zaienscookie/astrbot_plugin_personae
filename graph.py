"""GraphEngine —— 轻量知识图谱引擎（节点 + 关系 + RAG 检索）

供 Personae / Memoir 插件共用：将画像/记忆数据组织为图结构，
支持节点/边管理、JSON 持久化、基于关键词匹配的图检索（RAG）。
"""
import asyncio
import json
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path


def _now() -> int:
    return int(time.time())


def _nid() -> str:
    return f"n_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _eid() -> str:
    return f"e_{int(time.time())}_{uuid.uuid4().hex[:6]}"


# 默认节点类型配色（WebUI 图例用）
DEFAULT_NODE_TYPES = {
    "memory": {"label": "记忆", "color": "#6c8cff"},
    "tag": {"label": "标签", "color": "#3ddc97"},
    "author": {"label": "记录者", "color": "#ffb454"},
    "source": {"label": "来源", "color": "#9d6cff"},
    "user": {"label": "用户", "color": "#6c8cff"},
    "group": {"label": "群", "color": "#9d6cff"},
    "field": {"label": "字段", "color": "#3ddc97"},
    "value": {"label": "值", "color": "#ffb454"},
    "note": {"label": "备注", "color": "#ff7a7a"},
    "personality": {"label": "性格", "color": "#ff9f43"},
    "entity": {"label": "实体", "color": "#ff6b81"},
}


def extract_keywords(text: str, max_kw: int = 40) -> list[str]:
    """轻量关键词提取：中文 bigram + 英文/数字词。"""
    text = (text or "").strip()
    if not text:
        return []
    kws: set[str] = set()
    # 英文 / 数字词（长度 >= 2）
    for m in re.findall(r"[A-Za-z0-9_]{2,}", text.lower()):
        kws.add(m.lower())
    # 中文 bigram
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(cjk) - 1):
        kws.add(cjk[i] + cjk[i + 1])
    if len(cjk) == 1:
        kws.add(cjk[0])
    # 中文词（2-4 字连续片段，帮助匹配）
    for m in re.findall(r"[\u4e00-\u9fff]{2,4}", text):
        kws.add(m)
    return sorted(kws)[:max_kw]


class GraphEngine:
    """知识图谱引擎：节点 / 边 / 检索 / 持久化。"""

    def __init__(self, path: Path | str, node_types: dict | None = None):
        self.path = Path(path)
        self.node_types = {**DEFAULT_NODE_TYPES, **(node_types or {})}
        self._lock = asyncio.Lock()
        self.nodes: dict[str, dict] = {}          # nid -> node
        self.edges: list[dict] = []               # edge list
        self.edge_key: set[tuple] = set()         # (source, target, relation) 去重
        self.adj: dict[str, set[str]] = defaultdict(set)
        self._load()

    # ---------- 持久化 ----------

    def _load(self):
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.nodes = data.get("nodes", {})
                self.edges = data.get("edges", [])
                self._reindex()
        except Exception as e:
            import logging
            logging.getLogger("graph").error(f"[graph] 读取失败: {e}")

    def _reindex(self):
        self.edge_key = set()
        self.adj = defaultdict(set)
        for e in self.edges:
            s, t = e.get("source"), e.get("target")
            if s and t:
                self.edge_key.add((s, t, e.get("relation", "")))
                self.adj[s].add(t)
                self.adj[t].add(s)

    async def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"nodes": self.nodes, "edges": self.edges,
                           "meta": {"updated_at": _now()}}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            import logging
            logging.getLogger("graph").error(f"[graph] 保存失败: {e}")

    # ---------- 节点 ----------

    async def add_node(self, ntype: str, label: str, props: dict | None = None,
                       weight: int = 1) -> dict:
        """按 (type, label) 去重；已存在则合并 props 并增加 weight。"""
        async with self._lock:
            label = (label or "").strip()[:200]
            ntype = (ntype or "").strip()[:30] or "node"
            for nid, n in self.nodes.items():
                if n.get("type") == ntype and n.get("label") == label:
                    n["weight"] = n.get("weight", 1) + max(1, int(weight))
                    n["props"].update(props or {})
                    n["updated_at"] = _now()
                    return json.loads(json.dumps(n))
            nid = _nid()
            node = {
                "id": nid, "type": ntype, "label": label,
                "props": dict(props or {}), "weight": max(1, int(weight)),
                "created_at": _now(), "updated_at": _now(),
            }
            self.nodes[nid] = node
            return json.loads(json.dumps(node))

    async def remove_node(self, nid: str) -> bool:
        async with self._lock:
            if nid not in self.nodes:
                return False
            del self.nodes[nid]
            self.edges = [e for e in self.edges if e.get("source") != nid and e.get("target") != nid]
            self._reindex()
            return True

    async def get_node(self, nid: str) -> dict | None:
        async with self._lock:
            n = self.nodes.get(nid)
            return json.loads(json.dumps(n)) if n else None

    # ---------- 边 ----------

    async def add_edge(self, source: str, target: str, relation: str,
                       weight: int = 1) -> dict | None:
        async with self._lock:
            if source not in self.nodes or target not in self.nodes:
                return None
            key = (source, target, relation)
            if key in self.edge_key:
                for e in self.edges:
                    if (e.get("source"), e.get("target"), e.get("relation")) == key:
                        e["weight"] = e.get("weight", 1) + max(1, int(weight))
                        return json.loads(json.dumps(e))
            e = {"id": _eid(), "source": source, "target": target,
                 "relation": (relation or "")[:50], "weight": max(1, int(weight))}
            self.edges.append(e)
            self.edge_key.add(key)
            self.adj[source].add(target)
            self.adj[target].add(source)
            return json.loads(json.dumps(e))

    async def remove_edge(self, eid: str) -> bool:
        async with self._lock:
            before = len(self.edges)
            self.edges = [e for e in self.edges if e.get("id") != eid]
            if len(self.edges) != before:
                self._reindex()
                return True
            return False

    async def neighbors(self, nid: str, depth: int = 1) -> list[dict]:
        """返回 nid 的 depth 跳邻居节点（含自身）。"""
        async with self._lock:
            seen: set[str] = {nid}
            frontier = {nid}
            for _ in range(max(1, int(depth))):
                nxt: set[str] = set()
                for f in frontier:
                    nxt.update(self.adj.get(f, ()))
                frontier = nxt - seen
                seen |= frontier
            return [json.loads(json.dumps(self.nodes[i])) for i in seen if i in self.nodes]

    # ---------- 检索（RAG） ----------

    async def search(self, query: str, depth: int = 1, limit: int = 40) -> dict:
        """关键词匹配种子节点，扩展邻居，返回子图与知识文本。"""
        kws = extract_keywords(query)
        async with self._lock:
            seed: set[str] = set()
            for nid, n in self.nodes.items():
                text = (n.get("label", "") + " " + json.dumps(n.get("props", {}), ensure_ascii=False)).lower()
                hit = sum(1 for k in kws if k in text)
                if hit:
                    seed.add(nid)
            # 扩展子图（逐跳累积，保留所有层邻居）
            sub_nodes: dict[str, dict] = {}
            sub_edges: list[dict] = []
            collected = set(seed)
            frontier = set(seed)
            for _ in range(max(1, int(depth))):
                nxt: set[str] = set()
                for f in frontier:
                    nxt.update(self.adj.get(f, ()))
                frontier = nxt - collected
                collected |= frontier
            picked = collected
            for nid in picked:
                if nid in self.nodes:
                    sub_nodes[nid] = self.nodes[nid]
            for e in self.edges:
                if e.get("source") in sub_nodes and e.get("target") in sub_nodes:
                    sub_edges.append(e)
            # 生成知识文本
            lines: list[str] = []
            seen_pairs: set[tuple] = set()
            for e in sub_edges:
                s = sub_nodes.get(e.get("source"))
                t = sub_nodes.get(e.get("target"))
                if not s or not t:
                    continue
                pair = (e.get("source"), e.get("target"))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                rel = e.get("relation", "")
                lines.append(f"- {s.get('label')} {rel} {t.get('label')}")
            text = "\n".join(lines[:limit])
            return {
                "query": query,
                "keywords": kws,
                "matched": sorted({self.nodes[i].get("label") for i in seed if i in self.nodes}),
                "text": text,
                "nodes": [json.loads(json.dumps(n)) for n in sub_nodes.values()][:limit],
                "edges": [json.loads(json.dumps(e)) for e in sub_edges][:limit * 2],
            }

    # ---------- 全图 ----------

    async def to_json(self) -> dict:
        async with self._lock:
            by_type: dict[str, int] = defaultdict(int)
            for n in self.nodes.values():
                by_type[n.get("type", "node")] += 1
            return {
                "stats": {"nodes": len(self.nodes), "edges": len(self.edges),
                          "types": dict(by_type)},
                "nodes": [json.loads(json.dumps(n)) for n in self.nodes.values()],
                "edges": [json.loads(json.dumps(e)) for e in self.edges],
            }

    async def clear(self):
        async with self._lock:
            self.nodes.clear()
            self.edges.clear()
            self._reindex()
