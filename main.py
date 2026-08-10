import asyncio
import json
import os
import time
from pathlib import Path

from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.star.filter.permission import PermissionType

try:
    from aiohttp import web
    HAS_AIOHTTP = True
except Exception:
    HAS_AIOHTTP = False

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph import GraphEngine
from ai_client import AIClient

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
DATA_FILE = DATA_DIR / "user_profiles.json"
GRAPH_FILE = DATA_DIR / "graph.json"
WEBUI_FILE = PLUGIN_DIR / "webui" / "index.html"

COMMAND_PREFIXES = ("/", "／")


def now_ts() -> int:
    return int(time.time())


def fmt_ts(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "-"


class UserProfileStore:
    """用户画像存储：JSON 持久化，协程安全。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict = {"meta": {"version": 1, "created_at": now_ts()}, "users": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
        except Exception as e:
            logger.error(f"[personae] 读取数据文件失败: {e}")
        self._data.setdefault("meta", {})
        self._data.setdefault("users", {})

    async def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"[personae] 保存数据失败: {e}")

    async def touch_user(self, key, platform, user_id, nickname,
                         group_id="", group_name="") -> dict | None:
        """记录一次用户发言。"""
        async with self._lock:
            users = self._data["users"]
            ts = now_ts()
            u = users.get(key)
            if u is None:
                u = {
                    "key": key,
                    "platform": platform,
                    "user_id": str(user_id),
                    "nickname": nickname or "",
                    "groups": {},
                    "fields": {},
                    "message_count": 0,
                    "first_seen": ts,
                    "last_active": ts,
                    "last_message": "",
                    "notes": [],
                }
                users[key] = u
            if nickname and nickname != u.get("nickname"):
                u["nickname"] = nickname
            if group_id:
                g = u["groups"].setdefault(str(group_id), {"name": "", "last_active": ts})
                if group_name:
                    g["name"] = group_name
                g["last_active"] = ts
            u["message_count"] = u.get("message_count", 0) + 1
            u["last_active"] = ts
            await self._save()
            return u

    async def set_last_message(self, key, text):
        async with self._lock:
            u = self._data["users"].get(key)
            if u:
                u["last_message"] = (text or "")[:200]
                await self._save()

    async def set_ai_profile(self, key, ai_profile: dict):
        """保存 AI 分析出的性格画像摘要。"""
        async with self._lock:
            u = self._data["users"].get(key)
            if u:
                u["ai_profile"] = ai_profile
                await self._save()

    async def get_user(self, key):
        async with self._lock:
            u = self._data["users"].get(key)
            return json.loads(json.dumps(u)) if u else None

    async def list_users(self):
        async with self._lock:
            users = list(self._data["users"].values())
        users.sort(key=lambda x: x.get("last_active", 0), reverse=True)
        return users

    async def update_fields(self, key, fields: dict):
        async with self._lock:
            u = self._data["users"].get(key)
            if u is None:
                return None
            u.setdefault("fields", {})
            for k, v in (fields or {}).items():
                if v is None or str(v).strip() == "":
                    u["fields"].pop(k, None)
                else:
                    u["fields"][k] = str(v)[:200]
            await self._save()
            return json.loads(json.dumps(u))

    async def add_note(self, key, text, author=""):
        async with self._lock:
            u = self._data["users"].get(key)
            if u is None:
                return None
            u.setdefault("notes", [])
            u["notes"].append({"time": now_ts(), "text": (text or "")[:500], "author": author})
            u["notes"] = u["notes"][-50:]
            await self._save()
            return json.loads(json.dumps(u))

    async def delete_note(self, key, idx: int):
        async with self._lock:
            u = self._data["users"].get(key)
            if u is None:
                return False
            notes = u.get("notes", [])
            if 0 <= idx < len(notes):
                notes.pop(idx)
                await self._save()
                return True
            return False

    async def delete_user(self, key):
        async with self._lock:
            u = self._data["users"].pop(key, None)
            if u is not None:
                await self._save()
            return u is not None

    async def create_user(self, key, platform, user_id, nickname="", fields=None):
        async with self._lock:
            users = self._data["users"]
            if key in users:
                return users[key]
            ts = now_ts()
            u = {
                "key": key,
                "platform": platform,
                "user_id": str(user_id),
                "nickname": nickname or "",
                "groups": {},
                "fields": fields or {},
                "message_count": 0,
                "first_seen": ts,
                "last_active": ts,
                "last_message": "",
                "notes": [],
            }
            users[key] = u
            await self._save()
            return u

    async def stats(self):
        async with self._lock:
            users = self._data["users"]
            total_msgs = sum(u.get("message_count", 0) for u in users.values())
            groups = set()
            for u in users.values():
                groups.update(u.get("groups", {}).keys())
            return {
                "users": len(users),
                "messages": total_msgs,
                "groups": len(groups),
            }


@register(
    "astrbot_plugin_personae",
    "zaiens",
    "Personae 众生相：用户画像管理，独立端口 WebUI 管理面板",
    "v1.0.0",
)
class PersonaePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = UserProfileStore(DATA_FILE)
        self.graph = GraphEngine(GRAPH_FILE)
        self.webui_port = int(config.get("webui_port", 8799))
        self.webui_host = str(config.get("webui_host", "0.0.0.0"))
        self.enable_record = bool(config.get("enable_record", True))
        self.admins_id = set(str(x) for x in config.get("admins_id", []))
        global_admins = context.get_config().get("admins_id", [])
        self.admins_id.update(str(x) for x in global_admins)
        self._runner = None
        # ---- AI 性格分析 ----
        self.ai_enabled = bool(config.get("ai_enabled", True))
        self.ai = AIClient(
            base_url=str(config.get("ai_base_url", "") or ""),
            api_key=str(config.get("ai_api_key", "") or ""),
            model=str(config.get("ai_model", "") or ""),
            timeout=int(config.get("ai_timeout", 60) or 60),
            cooldown=int(config.get("ai_cooldown", 1800) or 1800),
            max_fails=int(config.get("ai_max_fails", 3) or 3),
        )
        self.ai_min_messages = int(config.get("ai_min_messages", 5) or 5)
        self.ai_interval = int(config.get("ai_interval", 18000) or 18000)
        self._msg_buf: dict[str, list[str]] = {}   # key -> 最近消息
        self._ai_lock = asyncio.Lock()
        self._ai_task: asyncio.Task | None = None
        self._ai_status: dict = {"last_run": 0, "analyzed": 0, "error": ""}

        # ---------- Dashboard 插件页面 API ----------
        from astrbot.api.web import request as _web_req

        async def _dash_stats():
            return await self.store.stats()

        async def _dash_users():
            return await self.store.list_users()

        async def _dash_user():
            u = await self.store.get_user(_web_req.path_params.get('key'))
            if u is None:
                return {'error': 'not found'}
            return u

        async def _dash_create():
            body = await _web_req.json() or {}
            platform = str(body.get('platform') or 'manual')
            user_id = str(body.get('user_id') or '').strip()
            if not user_id:
                return {'error': 'user_id required'}
            key = body.get('key') or f'{platform}:{user_id}'
            u = await self.store.create_user(
                key, platform, user_id,
                nickname=str(body.get('nickname') or ''),
                fields=body.get('fields') if isinstance(body.get('fields'), dict) else None,
            )
            await self.rebuild_graph()
            return u

        async def _dash_update():
            key = _web_req.path_params.get('key')
            u = await self.store.get_user(key)
            if u is None:
                return {'error': 'not found'}
            body = await _web_req.json() or {}
            fields = body.get('fields')
            if isinstance(fields, dict):
                await self.store.update_fields(u['key'], fields)
            u2 = await self.store.get_user(u['key'])
            await self.rebuild_graph()
            return u2

        async def _dash_add_note():
            key = _web_req.path_params.get('key')
            body = await _web_req.json() or {}
            text = str(body.get('text') or '').strip()
            if not text:
                return {'error': 'text required'}
            u = await self.store.add_note(key, text, author=str(body.get('author') or ''))
            if u is None:
                return {'error': 'not found'}
            await self.rebuild_graph()
            return u

        async def _dash_delete():
            ok = await self.store.delete_user(_web_req.path_params.get('key'))
            if ok:
                await self.rebuild_graph()
            return {'deleted': ok}

        async def _dash_graph():
            return await self.graph.to_json()

        async def _dash_graph_rebuild():
            await self.rebuild_graph()
            return {'ok': True}

        self.context.register_web_api('personae/api/stats', _dash_stats, ['GET'], '画像统计')
        self.context.register_web_api('personae/api/users', _dash_users, ['GET'], '用户列表')
        self.context.register_web_api('personae/api/user/<key>', _dash_user, ['GET'], '用户详情')
        self.context.register_web_api('personae/api/user', _dash_create, ['POST'], '新建用户')
        self.context.register_web_api('personae/api/user/<key>/update', _dash_update, ['POST'], '更新画像')
        self.context.register_web_api('personae/api/user/<key>/note', _dash_add_note, ['POST'], '添加备注')
        self.context.register_web_api('personae/api/user/<key>/delete', _dash_delete, ['POST'], '删除用户')
        self.context.register_web_api('personae/api/graph', _dash_graph, ['GET'], '知识图谱')
        self.context.register_web_api('personae/api/graph/rebuild', _dash_graph_rebuild, ['POST'], '重建图谱')

        async def _dash_delete_note():
            key = _web_req.path_params.get('key')
            idx = int(_web_req.path_params.get('idx') or -1)
            ok = await self.store.delete_note(key, idx)
            if not ok:
                return {'error': 'not found'}
            await self.rebuild_graph()
            return {'deleted': True}

        self.context.register_web_api('personae/api/user/<key>/note/<idx>/delete', _dash_delete_note, ['POST'], '删除备注')

    # ---------- 生命周期 ----------

    @filter.on_astrbot_loaded()
    async def _on_loaded(self, event=None):
        try:
            data = await self.graph.to_json()
            if not data["stats"]["nodes"]:
                await self.rebuild_graph()
        except Exception as e:
            logger.error(f"[personae] 初始构建图谱失败: {e}")
        if False:  # webui 已禁用
            pass  # webui 已禁用
        # 启动 AI 性格分析后台任务
        if self.ai_enabled and self.ai.configured:
            self._ai_task = asyncio.create_task(self._ai_worker())
            logger.info(f"[personae] AI 性格分析已启动: {self.ai.base_url} / {self.ai.model}")

    async def terminate(self):
        if self._ai_task:
            self._ai_task.cancel()
            try:
                await self._ai_task
            except Exception:
                pass
            self._ai_task = None
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.error(f"[personae] WebUI 关闭失败: {e}")
            self._runner = None

    async def _start_webui(self):
        if not WEBUI_FILE.exists():
            logger.error(f"[personae] WebUI 页面文件不存在: {WEBUI_FILE}")
            return
        app = web.Application()
        app.router.add_get("/", self._web_index)
        app.router.add_get("/api/stats", self._web_stats)
        app.router.add_get("/api/users", self._web_users)
        app.router.add_get("/api/user/{key}", self._web_user)
        app.router.add_put("/api/user/{key}", self._web_update)
        app.router.add_post("/api/user", self._web_create)
        app.router.add_delete("/api/user/{key}", self._web_delete)
        app.router.add_post("/api/user/{key}/note", self._web_add_note)
        app.router.add_delete("/api/user/{key}/note/{idx}", self._web_delete_note)
        # 知识图谱 + RAG
        app.router.add_get("/api/graph", self._web_graph)
        app.router.add_post("/api/graph/rebuild", self._web_graph_rebuild)
        app.router.add_get("/api/graph/nodes", self._web_graph_nodes)
        app.router.add_delete("/api/graph/node/{nid}", self._web_graph_node_delete)
        app.router.add_post("/api/rag/search", self._web_rag_search)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.webui_host, self.webui_port)
            await site.start()
            self._runner = runner
            logger.info(f"[personae] WebUI 已启动: http://<服务器IP>:{self.webui_port}/")
        except Exception as e:
            logger.error(f"[personae] WebUI 启动失败: {e}")

    # ---------- WebUI 处理 ----------

    async def _web_index(self, request):
        try:
            with open(WEBUI_FILE, "r", encoding="utf-8") as f:
                html = f.read()
            return web.Response(text=html, content_type="text/html", charset="utf-8")
        except Exception as e:
            return web.Response(text=f"页面加载失败: {e}", status=500)

    async def _web_stats(self, request):
        return web.json_response(await self.store.stats())

    async def _web_users(self, request):
        return web.json_response(await self.store.list_users())

    async def _web_user(self, request):
        u = await self.store.get_user(request.match_info["key"])
        if u is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(u)

    async def _web_update(self, request):
        u = await self.store.get_user(request.match_info["key"])
        if u is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        fields = body.get("fields")
        if isinstance(fields, dict):
            await self.store.update_fields(u["key"], fields)
        u2 = await self.store.get_user(u["key"])
        await self.rebuild_graph()
        return web.json_response(u2)

    async def _web_create(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        platform = str(body.get("platform") or "manual")
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            return web.json_response({"error": "user_id required"}, status=400)
        key = body.get("key") or f"{platform}:{user_id}"
        u = await self.store.create_user(
            key, platform, user_id,
            nickname=str(body.get("nickname") or ""),
            fields=body.get("fields") if isinstance(body.get("fields"), dict) else None,
        )
        await self.rebuild_graph()
        return web.json_response(u)

    async def _web_delete(self, request):
        ok = await self.store.delete_user(request.match_info["key"])
        if ok:
            await self.rebuild_graph()
        return web.json_response({"deleted": ok})

    async def _web_add_note(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        u = await self.store.add_note(request.match_info["key"], text, author=body.get("author") or "")
        if u is None:
            return web.json_response({"error": "not found"}, status=404)
        await self.rebuild_graph()
        return web.json_response(u)

    async def _web_delete_note(self, request):
        ok = await self.store.delete_note(request.match_info["key"], int(request.match_info["idx"]))
        if ok:
            await self.rebuild_graph()
        return web.json_response({"deleted": ok})

    # ---------- 图谱构建 ----------

    async def rebuild_graph(self):
        """从用户画像重建知识图谱：user / group / field / value / note / personality 节点 + 关系边。"""
        await self.graph.clear()
        users = await self.store.list_users()
        for u in users:
            key = u.get("key")
            nick = u.get("nickname") or u.get("user_id") or key
            unode = await self.graph.add_node(
                "user", str(nick),
                {"key": key, "platform": u.get("platform"), "user_id": u.get("user_id"),
                 "message_count": u.get("message_count", 0), "last_active": u.get("last_active", 0)},
                weight=1 + min(9, u.get("message_count", 0) // 10),
            )
            # 群
            for gid, g in (u.get("groups") or {}).items():
                gnode = await self.graph.add_node("group", str(g.get("name") or gid), {"id": gid})
                await self.graph.add_edge(unode["id"], gnode["id"], "在群")
            # 画像字段
            for fname, fval in (u.get("fields") or {}).items():
                fnode = await self.graph.add_node("field", str(fname), {})
                vnode = await self.graph.add_node("value", str(fval)[:120], {"field": fname})
                await self.graph.add_edge(unode["id"], vnode["id"], "有")
                await self.graph.add_edge(vnode["id"], fnode["id"], "属于字段")
            # 备注
            for note in (u.get("notes") or []):
                text = str(note.get("text") or "").strip()
                if not text:
                    continue
                nnode = await self.graph.add_node(
                    "note", text[:30] + ("…" if len(text) > 30 else ""), {"text": text[:300]})
                await self.graph.add_edge(unode["id"], nnode["id"], "有备注")
            # AI 性格画像
            ai_profile = u.get("ai_profile") or {}
            summary = str(ai_profile.get("summary") or "").strip()
            if summary:
                pnode = await self.graph.add_node("personality", summary[:40], {"text": summary[:200]})
                await self.graph.add_edge(unode["id"], pnode["id"], "性格")
            fields = u.get("fields") or {}
            if fields.get("性格"):
                pnode = await self.graph.add_node("personality", str(fields["性格"])[:40], {"text": str(fields["性格"])[:200]})
                await self.graph.add_edge(unode["id"], pnode["id"], "性格")
        await self.graph.save()
        return await self.graph.to_json()

    # ---------- WebUI: 知识图谱 / RAG ----------

    async def _web_graph(self, request):
        return web.json_response(await self.graph.to_json())

    async def _web_graph_rebuild(self, request):
        data = await self.rebuild_graph()
        return web.json_response({"rebuilt": True, **data})

    async def _web_graph_nodes(self, request):
        ntype = request.query.get("type", "").strip()
        data = await self.graph.to_json()
        nodes = data["nodes"]
        if ntype:
            nodes = [n for n in nodes if n.get("type") == ntype]
        return web.json_response(nodes)

    async def _web_graph_node_delete(self, request):
        ok = await self.graph.remove_node(request.match_info["nid"])
        if ok:
            await self.graph.save()
        return web.json_response({"deleted": ok})

    async def _web_rag_search(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        query = str(body.get("query", "")).strip()
        if not query:
            return web.json_response({"error": "query required"}, status=400)
        depth = int(body.get("depth", 1) or 1)
        result = await self.graph.search(query, depth=depth)
        return web.json_response(result)

    # ---------- 消息记录 ----------

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(r".+")
    async def on_any_message(self, event: AstrMessageEvent):
        """自动记录所有发言用户。"""
        if not self.enable_record:
            return
        try:
            uid = event.get_sender_id()
            if not uid or uid == event.get_self_id():
                return
            platform = event.get_platform_id() or "unknown"
            key = f"{platform}:{uid}"
            await self.store.touch_user(
                key, platform, uid,
                event.get_sender_name() or "",
                event.get_group_id() or "",
            )
            text = event.get_message_str()
            if text and not text.startswith(COMMAND_PREFIXES):
                await self.store.set_last_message(key, text)
                # AI 分析缓冲（仅记录非指令消息）
                if self.ai_enabled:
                    async with self._ai_lock:
                        buf = self._msg_buf.setdefault(key, [])
                        buf.append(f"{event.get_sender_name() or 'TA'}：{text[:200]}")
                        if len(buf) > 50:
                            del buf[: len(buf) - 50]
        except Exception as e:
            logger.error(f"[personae] 自动记录失败: {e}")
        return

    # ---------- AI 性格分析 ----------

    async def _ai_worker(self):
        """后台循环：定时对活跃用户做 AI 性格分析。"""
        while True:
            try:
                await asyncio.sleep(self.ai_interval)
                if not self.ai.healthy:
                    if self.ai.configured and self.ai.in_cooldown:
                        logger.warning(f"[personae] AI 处于冷却期，跳过性格分析（{int(self.ai.cooldown_until - time.time())}s 后恢复）")
                    continue
                await self._analyze_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[personae] AI 分析任务异常: {e}")
                await asyncio.sleep(30)

    async def _analyze_batch(self):
        """对消息数达标的用户做 AI 性格分析，更新画像字段。"""
        async with self._ai_lock:
            buf = {k: list(v) for k, v in self._msg_buf.items() if len(v) >= self.ai_min_messages}
            if not buf:
                return
        analyzed = 0
        for key, msgs in buf.items():
            try:
                ok = await self._analyze_user(key, msgs)
                if ok:
                    analyzed += 1
                    async with self._ai_lock:
                        del self._msg_buf[key]
            except Exception as e:
                logger.error(f"[personae] 分析用户 {key} 失败: {e}")
        if analyzed:
            self._ai_status.update(last_run=now_ts(), analyzed=analyzed, error="")
            try:
                await self.rebuild_graph()
            except Exception as e:
                logger.error(f"[personae] 重建图谱失败: {e}")

    async def _analyze_user(self, key: str, msgs: list[str]) -> bool:
        user = await self.store.get_user(key)
        if not user:
            return False
        nick = user.get("nickname") or user.get("user_id") or key
        dialog = "\n".join(msgs[-30:])
        system = (
            "你是一名资深用户画像分析师。根据对话内容分析这个人的性格与特点，"
            "输出 JSON，不要输出其他内容。字段：\n"
            '{"性格": "2-4个词概括", "说话风格": "简短描述", '
            '"爱好兴趣": "根据对话推断，没有就写未知", '
            '"行为习惯": "简短描述", "性格摘要": "一句话（不超过40字）描述这个人是什么性子"}'
        )
        user_prompt = f"分析对象：{nick}\n最近对话：\n{dialog}"
        data = await self.ai.chat_json(system, user_prompt, max_tokens=500)
        if not data or not isinstance(data, dict):
            return False
        fields = dict(user.get("fields") or {})
        updated = False
        for k, v in data.items():
            if k == "性格摘要":
                continue
            if v and str(v).strip() and str(v).strip() != "未知":
                s = str(v).strip()
                if fields.get(k) != s:
                    fields[k] = s
                    updated = True
        summary = str(data.get("性格摘要", "")).strip()
        ai_profile = {"summary": summary, "updated_at": now_ts()}
        if user.get("ai_profile") != ai_profile:
            updated = True
        if updated:
            await self.store.update_fields(key, fields)
            await self.store.set_ai_profile(key, ai_profile)
        return True

    @filter.on_llm_request()
    async def inject_profile(self, event: AstrMessageEvent, req):
        """LLM 请求前注入当前用户画像，让 bot 真正用上画像。"""
        try:
            uid = event.get_sender_id()
            if not uid or uid == event.get_self_id():
                return
            platform = event.get_platform_id() or "unknown"
            key = f"{platform}:{uid}"
            user = await self.store.get_user(key)
            if not user:
                return
            lines = [
                f"[用户画像] 当前对话用户：{user.get('nickname') or uid}（ID {uid}）"
            ]
            fields = user.get("fields") or {}
            fstr = "；".join(f"{k}：{v}" for k, v in fields.items() if v)
            if fstr:
                lines.append(f"已知信息：{fstr}")
            notes = user.get("notes") or []
            nstr = "；".join(str(n.get("text", "")) for n in notes[-3:] if n.get("text"))
            if nstr:
                lines.append(f"备注：{nstr}")
            ai_profile = user.get("ai_profile") or {}
            asum = str(ai_profile.get("summary") or "").strip()
            if asum:
                lines.append(f"性格（AI分析）：{asum}")
            last = (user.get("last_message") or "").strip()
            if last:
                lines.append(f"TA最近说：{last[:80]}")
            req.system_prompt += "\n\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"[personae] 画像注入失败: {e}")

    # ---------- 指令 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        uid = event.get_sender_id()
        return uid in self.admins_id

    def _render_user(self, u: dict, me=False) -> str:
        lines = [
            f"—— 用户画像 ——",
            f"昵称：{u.get('nickname') or '未知'}",
            f"平台：{u.get('platform')} | ID：{u.get('user_id')}",
        ]
        groups = u.get("groups") or {}
        if groups:
            names = [g.get("name") or gid for gid, g in groups.items()]
            lines.append(f"所在群({len(groups)})：{'、'.join(names[:5])}")
        fields = u.get("fields") or {}
        if fields:
            parts = [f"{k}={v}" for k, v in fields.items()]
            lines.append("字段：" + "，".join(parts))
        lines.append(f"消息数：{u.get('message_count', 0)}")
        lines.append(f"首次活跃：{fmt_ts(u.get('first_seen', 0))}")
        lines.append(f"最近活跃：{fmt_ts(u.get('last_active', 0))}")
        notes = u.get("notes") or []
        if notes:
            last = notes[-1]
            lines.append(f"最近备注：{last.get('text')[:50]}")
        if me:
            lines.append("")
            lines.append("可用：/画像设置 字段 值 · /画像备注 文本")
        return "\n".join(lines)

    @filter.command("画像")
    @filter.event_message_type(EventMessageType.ALL)
    async def profile_view(self, event: AstrMessageEvent):
        """查看自己或@对象的画像。"""
        target = None
        for comp in event.get_messages():
            if isinstance(comp, At):
                target = str(comp.qq)
                break
        if target is None:
            target = event.get_sender_id()
        platform = event.get_platform_id() or "unknown"
        key = f"{platform}:{target}"
        u = await self.store.get_user(key)
        if u is None:
            # 尝试直接查询一下（某些平台昵称缺失时）
            u = await self.store.touch_user(key, platform, target, event.get_sender_name() or "")
        if u is None:
            yield event.plain_result("该用户暂无画像记录。")
            return
        yield event.plain_result(self._render_user(u, me=(target == event.get_sender_id())))

    @filter.command("画像设置")
    @filter.event_message_type(EventMessageType.ALL)
    async def profile_set(self, event: AstrMessageEvent, field: str, value: str):
        """设置自己的画像字段。管理员可 @ 修改他人。"""
        key = f"{event.get_platform_id() or 'unknown'}:{event.get_sender_id()}"
        target = None
        for comp in event.get_messages():
            if isinstance(comp, At):
                target = str(comp.qq)
                break
        if target is not None:
            if not self._is_admin(event):
                yield event.plain_result("只有管理员能修改他人画像。")
                return
            key = f"{event.get_platform_id() or 'unknown'}:{target}"
        u = await self.store.get_user(key)
        if u is None:
            u = await self.store.create_user(key, event.get_platform_id() or "unknown",
                                            event.get_sender_id(), event.get_sender_name() or "")
        await self.store.update_fields(key, {field: value})
        yield event.plain_result(f"✅ 已设置：{field} = {value}")

    @filter.command("画像备注")
    @filter.event_message_type(EventMessageType.ALL)
    async def profile_note(self, event: AstrMessageEvent, text: str):
        """给自己加一条备注。"""
        uid = event.get_sender_id()
        key = f"{event.get_platform_id() or 'unknown'}:{uid}"
        u = await self.store.get_user(key)
        if u is None:
            u = await self.store.create_user(key, event.get_platform_id() or "unknown",
                                            uid, event.get_sender_name() or "")
        await self.store.add_note(key, text, author=uid)
        yield event.plain_result("✅ 已添加备注。")

    @filter.command("画像列表")
    @filter.event_message_type(EventMessageType.ALL)
    async def profile_list(self, event: AstrMessageEvent):
        """查看本群用户活跃排行。"""
        group_id = event.get_group_id()
        users = await self.store.list_users()
        if group_id:
            users = [u for u in users if group_id in u.get("groups", {})]
        if not users:
            yield event.plain_result("暂无用户记录。")
            return
        lines = ["—— 本群用户排行 ——"]
        for i, u in enumerate(users[:15], 1):
            lines.append(f"{i}. {u.get('nickname') or u.get('user_id')} · {u.get('message_count', 0)}条")
        yield event.plain_result("\n".join(lines))
