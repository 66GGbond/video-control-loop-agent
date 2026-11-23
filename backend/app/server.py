"""Zero-dependency server for the interview demo.

Run with `python3 run_demo.py`, then open http://127.0.0.1:8000.
"""

from __future__ import annotations

import json
import os
import base64
import binascii
import shutil
import uuid
import threading
import hashlib
from functools import wraps
from copy import copy
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib import request as urllib_request

from .sessions import SessionStore
from .conversation import record_exchange
from .agent.planner import ArkControlPlanner
from .agent.context import ContextEnrichmentAgent
from .agent.generation_spec import validate_brief_duration
from .agent.prompt_compiler import PromptCompiler
from .demo import create_empty_state, create_enriched_project_state, create_orchestrator, present
from .domain.models import ProjectState, VersionReview
from .tools.providers import create_video_generator


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
RUNTIME = Path(os.getenv("RUNTIME_DIR", str(ROOT / ".runtime")))
# Keep the local composition payload bounded independently of any provider.
# Adapters decide later which retained assets have a documented request path.
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_REPAIR_ITERATIONS = 5


def load_local_env(path: Path) -> None:
    """Load only simple KEY=VALUE entries without adding a dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {
            "ARK_API_KEY", "ARK_MODEL", "SEEDANCE_25_API_KEY", "SEEDANCE_20_API_KEY", "SEEDREAM_PRO_API_KEY", "SEEDREAM_FLASH_API_KEY",
            "ARK_VIDEO_TEXT_MODEL", "ARK_VIDEO_REFERENCE_MODEL", "VIDEO_PROVIDER",
            "MINIMAX_API_KEY", "MINIMAX_VIDEO_MODEL", "MINIMAX_VIDEO_RESOLUTION", "MINIMAX_API_BASE_URL",
            "PORT",
        } and value.strip():
            os.environ.setdefault(key, value.strip())


if os.getenv("VIDEO_AGENT_SKIP_LOCAL_ENV") != "1":
    load_local_env(ROOT / ".env")
SESSIONS = SessionStore(RUNTIME)
from .image_tasks import ImageTasks
from . import character_references as characters
from .generation_settings import configure as configure_generation
from .projects import Projects
VISITOR_LOCKS = [threading.RLock() for _ in range(128)]
ORCHESTRATOR = create_orchestrator()
CONTROL_PLANNER = ArkControlPlanner()
CONTEXT_AGENT = ContextEnrichmentAgent()
PROMPT_COMPILER = PromptCompiler()
VIDEO_GENERATOR = create_video_generator()


def start_user_directed_revision(original: ProjectState, instruction: str, language: str | None = None) -> ProjectState:
    """Replan after publication without overwriting the reviewed version chain.

    A published video is an immutable piece of evidence, not the end of the
    conversation.  When the user supplies a new creative direction, the Agent
    starts a fresh control-plan cycle for the *next* version.  The previous
    videos, reports and decision log remain attached to the same project so a
    reviewer can compare the before/after lineage.
    """
    target_market = next(
        (
            str(item.get("value", ""))
            for item in original.creative_spec.enriched_context.get("facts", [])
            if item.get("key") == "target_market"
        ),
        "",
    )
    revised_prompt = f"{original.creative_spec.source_prompt}\n\nAdditional user direction: {instruction}"
    validate_brief_duration(revised_prompt)
    revised = create_enriched_project_state(
        revised_prompt,
        characters.source_reference(original.creative_spec),
        CONTROL_PLANNER,
        CONTEXT_AGENT,
        target_market,
        characters.source_assets(original.creative_spec),
        ui_language=language or original.creative_spec.enriched_context.get("ui_language", "zh"),
    )

    # Keep the version chain immutable.  The fresh state only replaces the
    # active creative specification and its future repair budget.
    revised.project_id = original.project_id
    revised.current_version_id = original.current_version_id
    revised.current_video_asset = original.current_video_asset.copy() if original.current_video_asset else None
    revised.version_reviews = original.version_reviews
    revised.evaluation_history = original.evaluation_history
    revised.generation_jobs = original.generation_jobs
    revised.conversation = original.conversation
    revised.latest_report = original.latest_report
    revised.continuity_memory = original.continuity_memory
    # Evidence for an evaluated version stays on disk for its review.  Clear
    # the active working set so registering V(n+1) cannot delete V(n)'s frames.
    revised.evaluation_frame_records = []
    revised.latest_decision = None
    revised.latest_repair_task = None
    revised.observation_error = ""
    revised.pending_user_direction = ""
    revised.iterations_used = 0
    revised.execution_mode = "collaborative"
    revised.autopilot_paused = False
    revised.history = [
        *original.history,
        *revised.history,
        {
            "at": "local-demo",
            "phase": "human_approval",
            "event": "post_publication_control_plan_revised",
            "payload": {
                "message": (
                    f"User requested a creative revision after {original.current_version_id.upper()} passed the release gate. "
                    "Agent created a new control plan for the next version; prior video evidence remains immutable."
                ),
            },
        },
    ]
    return revised


def visitor_request(method):
    @wraps(method)
    def wrapped(self):
        if not urlparse(self.path).path.startswith("/api/"):
            return method(self)
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        self.visitor_id, self.visitor_cookie = SESSIONS.visitor(cookie["agent_session"].value if "agent_session" in cookie else cookie["trial_visitor"].value if "trial_visitor" in cookie else "")
        lock = VISITOR_LOCKS[int(hashlib.sha256(self.visitor_id.encode()).hexdigest(), 16) % len(VISITOR_LOCKS)]
        with lock:
            self.state = SESSIONS.load(self.visitor_id) or create_empty_state()
            if urlparse("http://" + self.headers.get("Host", "")).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return self._json(403, {"error": "This application is available locally only."})
            # Same-origin JSON requests only; reject cross-site form/fetch submissions.
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if (origin and urlparse(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    return self._json(403, {"error": "请求来源无效。"})
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self._json(415, {"error": "请使用 JSON 请求。"})
            expected = self.headers.get('X-Project-ID')
            canvas_path=urlparse(self.path).path in {'/api/canvas','/api/new-canvas','/api/recommend-image-model','/api/generate-image','/api/image-results'} or urlparse(self.path).path.startswith('/api/image-task/')
            expected_canvas=self.headers.get('X-Canvas-ID')
            if canvas_path and expected_canvas and expected_canvas!=SESSIONS.canvas_project(self.visitor_id):
                return self._json(HTTPStatus.CONFLICT,{'error':'画布项目已切换，请刷新后继续。'})
            if not canvas_path and expected and expected != self.state.project_id and urlparse(self.path).path not in {'/api/projects','/api/projects/open','/api/projects/rename','/api/project-titles'}:
                return self._json(HTTPStatus.CONFLICT, {'error': '当前项目已在其他页面切换，请刷新后继续。'})
            SESSIONS.save(self.visitor_id, self.state)
            try:
                return method(self)
            finally:
                SESSIONS.save(self.visitor_id, self.state)
    return wrapped


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    @visitor_request
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/projects':
            return self._json(HTTPStatus.OK, Projects(SESSIONS).list(self.visitor_id))
        if path.startswith('/api/project-preview/'):
            result = Projects(SESSIONS).image(self.visitor_id, path.rsplit('/',1)[-1])
            if not result:
                return self._json(HTTPStatus.NOT_FOUND, {'error':'Project preview unavailable'})
            self.send_response(HTTPStatus.OK);self.send_header('Content-Type',result[0]);self.send_header('Content-Length',str(len(result[1])));self.send_header('Cache-Control','private, no-cache');self.end_headers();self.wfile.write(result[1]);return
        if path == "/api/state":
            return self._json(HTTPStatus.OK, present(self.state))
        if path == "/api/character-references":
            characters.sync(self.state, SESSIONS, self.visitor_id)
            return self._json(HTTPStatus.OK, present(self.state))
        if path == "/api/canvas":
            return self._json(HTTPStatus.OK, SESSIONS.load_canvas(self.visitor_id))
        if path.startswith('/api/image-task/'):
            return self._json(HTTPStatus.OK, ImageTasks(SESSIONS,SESSIONS.canvas_project(self.visitor_id)).get(self.visitor_id,path.rsplit('/',1)[-1]))
        if path == '/api/image-results':
            with SESSIONS.connect() as db:
                tasks=db.execute('SELECT node_id,data FROM image_tasks WHERE visitor_id=?',(self.visitor_id,)).fetchall()
                ids={r[0] for r in db.execute('SELECT id FROM image_outputs WHERE visitor_id=?',(self.visitor_id,))}
            character_tasks={p.get('task_id') for p in self.state.creative_spec.character_reference_plan.get('characters',[])}
            results=[]
            for row in reversed(tasks):
                job=json.loads(row['data']);run=job.get('run_id')
                if (row['node_id'].startswith(SESSIONS.canvas_project(self.visitor_id)+':') or row['node_id'] in character_tasks) and run in ids and job.get('status')=='succeeded':
                    results.append({'run_id':run,'name':'Seedream '+run[:8]})
            return self._json(HTTPStatus.OK, {'results':results})
        if path.startswith('/api/image-output/'):
            result=ImageTasks(SESSIONS).output(self.visitor_id,path.rsplit('/',1)[-1])
            if not result:return self._json(HTTPStatus.NOT_FOUND,{'error':'Image unavailable'})
            self.send_response(HTTPStatus.OK);self.send_header('Content-Type',result[0]);self.send_header('Content-Length',str(len(result[1])));self.end_headers();self.wfile.write(result[1]);return
        if path.startswith("/api/generated-video/"):
            return self._serve_generated_video(path.rsplit("/", 1)[-1])
        return super().do_GET()

    @visitor_request
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/new-canvas':
                payload=self._read_json();name=payload.get('title','')
                if not isinstance(name,str) or len(name.strip())>80:raise ValueError('项目名称需要少于 80 个字符。')
                SESSIONS.clear_canvas(self.visitor_id)
                with SESSIONS.connect() as db:
                    db.execute('INSERT OR REPLACE INTO canvas_projects VALUES (?,?)',(self.visitor_id,uuid.uuid4().hex))
                if name.strip():
                    current=SESSIONS.load_canvas(self.visitor_id);current['document']['title']=name.strip()
                    SESSIONS.save_canvas(self.visitor_id,current['revision'],current['document'])
                return self._json(HTTPStatus.OK,{'project_id':SESSIONS.canvas_project(self.visitor_id),'workspace':'canvas'})
            if path == '/api/projects/workspace':
                Projects(SESSIONS).workspace(self.visitor_id,self.state.project_id,self._read_json().get('workspace'))
                return self._json(HTTPStatus.OK, {'saved':True})
            if path == '/api/project-titles':
                payload = self._read_json()
                return self._json(HTTPStatus.OK, Projects(SESSIONS).start_naming(self.visitor_id, payload.get('language','zh')))
            if path == '/api/projects/rename':
                payload = self._read_json()
                Projects(SESSIONS).rename(self.visitor_id,payload.get('project_id'),payload.get('title'))
                return self._json(HTTPStatus.OK, Projects(SESSIONS).list(self.visitor_id))
            if path == '/api/projects/open':
                payload = self._read_json()
                self.state, workspace = Projects(SESSIONS).open(self.visitor_id,payload.get('project_id'))
                return self._json(HTTPStatus.OK, {'project_id':payload.get('project_id'),'workspace':workspace})
            if path in {'/api/create-project', '/api/reset', '/api/revise-brief', '/api/confirm-context'} and characters.running(self.state, SESSIONS, self.visitor_id):
                return self._json(HTTPStatus.CONFLICT, {'error': '人物四视图正在生成，请等待结果后再修改或重置项目。'})
            if path == '/api/attach-character-references':
                characters.attach(self.state, SESSIONS, self.visitor_id, self._read_json(), self._normalise_multimodal_assets)
                return self._json(HTTPStatus.OK, present(self.state))
            if path in {'/api/plan-character-references', '/api/generate-character-references'}:
                payload = self._read_json()
                if path == '/api/plan-character-references':
                    characters.prepare(self.state, SESSIONS, self.visitor_id)
                else:
                    characters.start(self.state, SESSIONS, self.visitor_id, payload.get('plan_id'), payload.get('model_id'))
                return self._json(HTTPStatus.OK, present(self.state))
            if path == '/api/configure-generation':
                if os.getenv('VIDEO_PROVIDER') != 'auto':
                    raise ValueError('Multi-model routing is not enabled')
                configure_generation(self.state, self._read_json())
                return self._json(HTTPStatus.OK, present(self.state))
            if path in {'/api/recommend-image-model','/api/generate-image'}:
                payload=self._read_json();node=payload.get('node_id')
                if not isinstance(node,str):raise ValueError('Select an image task')
                document=SESSIONS.load_canvas(self.visitor_id)['document'];service=ImageTasks(SESSIONS,SESSIONS.canvas_project(self.visitor_id))
                if path=='/api/recommend-image-model':result=service.recommend(self.visitor_id,node,document)
                else:result=service.start(self.visitor_id,node,document,payload.get('choice_id'),payload.get('model_id'))
                return self._json(HTTPStatus.ACCEPTED if result.get('status')=='running' else HTTPStatus.OK,result)
            if path == "/api/canvas":
                from .canvas import validate_canvas
                payload = self._read_json()
                document = validate_canvas(payload.get("document"), self._normalise_multimodal_assets)
                revision = SESSIONS.save_canvas(self.visitor_id, payload.get("revision"), document)
                if revision is None:
                    return self._json(HTTPStatus.CONFLICT, {"error": "Canvas changed in another tab. Export your draft before reloading."})
                return self._json(HTTPStatus.OK, {"revision": revision})
            if path == "/api/canvas-reference":
                payload = self._read_json()
                assets = self.state.creative_spec.multimodal_assets
                index = payload.get("index")
                if payload.get("project_id") != self.state.project_id or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(assets):
                    raise ValueError("Project reference is no longer available")
                asset = assets[index]
                return self._json(HTTPStatus.OK, {"asset": self._normalise_multimodal_assets({"multimodal_assets": [{**asset, "data_url": asset.get("data_url") or asset.get("preview_data_url")}]})[0]})
            if path in {"/api/recommend-video-model", "/api/confirm-video-model"}:
                from .agent.model_router import recommend, confirm
                if os.getenv("VIDEO_PROVIDER") != "auto":
                    raise ValueError("Multi-model routing is not enabled")
                if self.state.status not in {"planning_ready", "awaiting_first_video", "awaiting_repair_upload"}:
                    raise ValueError("当前阶段不能更改视频生成模型。")
                if path == "/api/recommend-video-model":
                    recommend(self.state)
                else:
                    payload = self._read_json()
                    card = confirm(self.state, payload.get("model_id"), payload.get("fingerprint"))
                    PROMPT_COMPILER.compile(self.state, self.state.latest_repair_task, "v1" if self.state.current_version_id == "none" else self._next_version_id(self.state.current_version_id))
                    self.state.history.append({"at":"local-demo", "phase":"human_approval", "event":"video_model_confirmed", "payload":{"model_id":card["model_id"], "warnings":card["warnings"]}})
                return self._json(HTTPStatus.OK, present(self.state))
            if path in {"/api/create-project", "/api/reset"} and self.state.status == "generating_video":
                return self._json(HTTPStatus.CONFLICT, {"error": "A video is being generated. Please wait for the result."})
            if path == "/api/create-project":
                payload = self._read_json()
                prompt = str(payload.get("prompt", "")).strip()
                if not prompt:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "A creative prompt is required"})
                validate_brief_duration(prompt)
                assets = self._normalise_multimodal_assets(payload)
                reference_asset = next((asset for asset in assets if asset["media_type"] == "image"), None)
                previous = self.state
                self.state = create_enriched_project_state(
                    prompt, reference_asset, CONTROL_PLANNER, CONTEXT_AGENT,
                    multimodal_assets=assets,
                    ui_language=payload.get("language", "zh"),
                )
                if previous.status == 'ready_for_brief':
                    self.state.project_id = previous.project_id
                    self.state.project_name = previous.project_name
                record_exchange(self.state, prompt)
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/confirm-context":
                if self.state.status != "awaiting_context_confirmation":
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "There is no pending context question for this project"})
                payload = self._read_json()
                target_market = str(payload.get("target_market", "")).strip()
                if not target_market:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "A target market or region is required for this seasonal request"})
                original = self.state
                self.state = create_enriched_project_state(
                    self.state.creative_spec.source_prompt,
                    characters.source_reference(self.state.creative_spec),
                    CONTROL_PLANNER,
                    CONTEXT_AGENT,
                    target_market,
                    characters.source_assets(self.state.creative_spec),
                    ui_language=payload.get("language", original.creative_spec.enriched_context.get("ui_language", "zh")),
                )
                self.state.project_id = original.project_id
                record_exchange(self.state, target_market, original)
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "human_approval",
                    "event": "context_confirmed",
                    "payload": {"message": "User provided the target market required for seasonal context."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/approve-plan":
                if self.state.status != "planning_ready":
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Create a control plan before approving it"})
                payload = self._read_json()
                budget = int(payload.get("max_iterations", self.state.creative_spec.recommended_iterations))
                if budget < 0 or budget > MAX_REPAIR_ITERATIONS:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": f"Repair budget must be between 0 and {MAX_REPAIR_ITERATIONS}"})
                self.state.creative_spec.max_iterations = budget
                self.state.status = "awaiting_first_video"
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/autopilot":
                payload = self._read_json()
                action = str(payload.get("action", "start")).strip()
                if action == "pause":
                    if self.state.execution_mode != "autopilot":
                        return self._json(HTTPStatus.BAD_REQUEST, {"error": "Autopilot is not running for this project"})
                    self.state.autopilot_paused = True
                    self.state.history.append({
                        "at": "local-demo",
                        "phase": "human_approval",
                        "event": "autopilot_pause_requested",
                        "payload": {"message": "User requested a pause after the current provider action completes."},
                    })
                    return self._json(HTTPStatus.OK, present(self.state))
                allowed = {"planning_ready", "awaiting_first_video", "ready_for_evaluation", "repair_uploaded", "awaiting_repair_upload", "generating_video", "generated_video_ready", "stopped_budget_exhausted"}
                if self.state.status not in allowed:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Autopilot can start only while a plan or an unfinished version is available"})
                if self.state.status == "planning_ready":
                    self.state.status = "awaiting_first_video"
                self.state.execution_mode = "autopilot"
                self.state.autopilot_paused = False
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "human_approval",
                    "event": "autopilot_authorized",
                    "payload": {"message": f"User authorized the Agent to run within its recommended limit of {self.state.creative_spec.max_iterations} repair iteration(s)."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/revise-brief":
                payload = self._read_json()
                instruction = str(payload.get("instruction", "")).strip()
                if not instruction:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Enter a revision before sending it to the Agent"})
                if self.state.creative_spec.character_reference_plan.get('status')=='awaiting_clarification':
                    original=self.state
                    self.state=start_user_directed_revision(original,instruction,payload.get('language'))
                    self.state.creative_spec.character_reference_plan=original.creative_spec.character_reference_plan
                    self.state.creative_spec.enriched_context['creative_answers']=list(original.creative_spec.enriched_context.get('creative_answers',[]))
                    characters.answer(self.state,instruction,SESSIONS,self.visitor_id)
                    return self._json(HTTPStatus.OK,present(self.state))
                previous = copy(self.state)
                if self.state.status in {"planning_ready", "awaiting_first_video", "awaiting_context_confirmation"}:
                    original = self.state
                    target_market = next(
                        (str(item.get("value", "")) for item in original.creative_spec.enriched_context.get("facts", []) if item.get("key") == "target_market"),
                        "",
                    )
                    revised_prompt = f"{original.creative_spec.source_prompt}\n\nAdditional user direction: {instruction}"
                    validate_brief_duration(revised_prompt)
                    self.state = create_enriched_project_state(
                        revised_prompt,
                        characters.source_reference(original.creative_spec),
                        CONTROL_PLANNER,
                        CONTEXT_AGENT,
                        target_market,
                        characters.source_assets(original.creative_spec),
                        ui_language=payload.get("language", original.creative_spec.enriched_context.get("ui_language", "zh")),
                    )
                    self.state.project_id = original.project_id
                    record_exchange(self.state, instruction, previous)
                    self.state.history = [*original.history, {
                        "at": "local-demo",
                        "phase": "human_approval",
                        "event": "control_plan_revised",
                        "payload": {"message": "User added a direction before first-version generation; Agent recompiled the control plan."},
                    }]
                    return self._json(HTTPStatus.OK, present(self.state))
                if self.state.status == "published":
                    self.state = start_user_directed_revision(self.state, instruction, payload.get("language"))
                    record_exchange(self.state, instruction, previous)
                    return self._json(HTTPStatus.OK, present(self.state))
                if self.state.status == "awaiting_repair_upload" and self.state.latest_repair_task:
                    self.state.latest_repair_task.prompt += f" User-directed refinement: {instruction}"
                    self.state.latest_repair_task.continuity_constraints.append(f"User-directed refinement: {instruction}")
                    PROMPT_COMPILER.compile(self.state, self.state.latest_repair_task, self._next_version_id(self.state.current_version_id))
                else:
                    self.state.pending_user_direction = instruction
                record_exchange(self.state, instruction, previous, reply="Direction saved for the next eligible version.")
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "human_approval",
                    "event": "next_version_direction_saved",
                    "payload": {"message": "User direction was retained for the next eligible version; existing evaluated evidence remains immutable."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/generate-video":
                characters.sync(self.state, SESSIONS, self.visitor_id)
                characters.ensure_ready(self.state)
                if (self.state.creative_spec.production_strategy or {}).get("mode") == "needs_segmentation":
                    return self._json(HTTPStatus.BAD_REQUEST, {
                        "error": "This brief requires a multi-segment production plan. The single-clip generator is intentionally blocked rather than compressing the story."
                    })
                if self.state.status not in {"awaiting_first_video", "awaiting_repair_upload"}:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Approve a control plan or wait for a repair decision before generating a video"})
                repairing = self.state.status == "awaiting_repair_upload"
                if repairing and not self.state.latest_repair_task:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "The Agent must create a repair task before generating the next version"})
                version_id = "v1" if self.state.current_version_id == "none" else self._next_version_id(self.state.current_version_id)
                PROMPT_COMPILER.compile(self.state, self.state.latest_repair_task if repairing else None, version_id)
                if not VIDEO_GENERATOR.available():
                    raise ValueError("Video generation is not configured. Check your local model settings.")
                selection = VIDEO_GENERATOR.select_tool(self.state)
                if selection.get("request_mode") == "unavailable":
                    raise ValueError(selection["reason"])
                try:
                    job = VIDEO_GENERATOR.submit(self.state, version_id, self.state.latest_repair_task if repairing else None)
                except (ValueError, OSError):
                    self.state.autopilot_paused = True
                    raise
                self.state.generation_jobs.append(job)
                review = next((item for item in self.state.version_reviews if item.version_id == version_id), None)
                if not review:
                    self.state.version_reviews.append(VersionReview(
                        version_id=version_id,
                        parent_version_id=None if version_id == "v1" else self.state.current_version_id,
                        status="generating",
                        compile_run_id=job.compile_run_id,
                        generation_contract_id=job.generation_contract_id,
                        evaluation_contract_id=job.evaluation_contract_id,
                    ))
                else:
                    # A retry of the same version must point to the contract
                    # that actually produced the retried provider job.
                    review.compile_run_id = job.compile_run_id
                    review.generation_contract_id = job.generation_contract_id
                    review.evaluation_contract_id = job.evaluation_contract_id
                self.state.status = "generating_video"
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "act",
                    "event": "generation_submitted",
                    "payload": {
                        "message": (
                            f"Agent submitted {version_id} with {job.tool_name} as "
                            f"{self.state.creative_spec.generation_spec.get('duration_seconds', 'planned')}s "
                            f"{self.state.creative_spec.generation_spec.get('aspect_ratio', 'planned')} "
                            f"at {self.state.creative_spec.generation_spec.get('quality_target', 'planned')} clarity."
                        ),
                        "tool": job.tool_name,
                        "provider": job.provider_id,
                        "model": job.model_id,
                        "request_mode": job.request_mode,
                        "capability_profile": job.capability_profile_id,
                        "capability_notice": job.capability_notice,
                        "generation_spec": self.state.creative_spec.generation_spec,
                        "repair_scope": (self.state.latest_repair_task.execution_scope if repairing and self.state.latest_repair_task else "initial_generation"),
                        "repair_scope_notice": (self.state.latest_repair_task.scope_notice if repairing and self.state.latest_repair_task else "Initial complete-clip generation."),
                        "compile_run_id": job.compile_run_id,
                        "generation_contract_id": job.generation_contract_id,
                        "evaluation_contract_id": job.evaluation_contract_id,
                        "reason": job.selection_reason,
                        "rejected": job.rejected_option,
                    },
                })
                return self._json(HTTPStatus.ACCEPTED, present(self.state))
            if path == "/api/refresh-generation":
                if self.state.status != "generating_video" or not self.state.generation_jobs:
                    return self._json(HTTPStatus.OK, present(self.state))
                job = self.state.generation_jobs[-1]
                try:
                    job = VIDEO_GENERATOR.refresh(job)
                except ValueError as exc:
                    # Keep the provider task and its version intact: a polling
                    # outage does not prove the remote render failed.  Pause
                    # autonomy so it cannot submit a duplicate paid job; the
                    # browser will continue its harmless status polling.
                    job.error = str(exc)
                    if job.status not in {"queued", "running"}:
                        job.status = "queued"
                    review = next((item for item in self.state.version_reviews if item.version_id == job.version_id), None)
                    if review:
                        review.status = "generating"
                        review.observation_error = job.error
                    self._pause_autopilot_for_failure(
                        "autopilot_paused_for_generation_status_error",
                        "Agent autonomy paused because the provider task status could not be verified. The existing task is retained; no duplicate generation will be submitted until the user resumes it.",
                    )
                    self.state.history.append({
                        "at": "local-demo",
                        "phase": "act",
                        "event": "generation_status_unavailable",
                        "payload": {"message": job.error},
                    })
                    return self._json(HTTPStatus.OK, present(self.state))
                review = next((item for item in self.state.version_reviews if item.version_id == job.version_id), None)
                if job.status in {"queued", "running"}:
                    if review:
                        review.status = "generating"
                    return self._json(HTTPStatus.OK, present(self.state))
                if job.status == "succeeded":
                    if not job.output_video_url:
                        job.status = "failed"
                        job.error = "The video provider completed the task without a downloadable video URL"
                        return self._generation_failed(job, review)
                    try:
                        local_path, size = self._download_generated_video(job.output_video_url, job.version_id)
                    except ValueError as exc:
                        job.status = "failed"
                        job.error = str(exc)
                        return self._generation_failed(job, review)
                    self.state.current_version_id = job.version_id
                    self.state.current_video_asset = {
                        "name": f"Agent generated {job.version_id.upper()}",
                        "mime_type": "video/mp4",
                        "size": str(size),
                        "source": "agent_generated",
                        "local_video_path": str(local_path),
                        "local_video_url": f"/api/generated-video/{job.version_id}",
                        "frame_count": "0",
                    }
                    if review:
                        review.asset = self.state.current_video_asset.copy()
                        review.status = "sampling_evidence"
                    self.state.status = "generated_video_ready"
                    self.state.history.append({
                        "at": "local-demo",
                        "phase": "act",
                        "event": "generation_completed",
                        "payload": {"message": f"The video provider completed {job.version_id}; Agent is preparing visual evidence."},
                    })
                else:
                    return self._generation_failed(job, review)
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/register-generated-video":
                if self.state.status != "generated_video_ready" or not self.state.current_video_asset:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "A completed generated video is required before preparing evidence"})
                payload = self._read_json()
                frame_records = self._store_evaluation_frames(payload.get("frames"))
                self._discard_evaluation_frames(self.state.evaluation_frame_records)
                self.state.evaluation_frame_records = frame_records
                self.state.current_video_asset["frame_count"] = str(len(frame_records))
                self.state.current_video_asset["poster_data_url"] = self._poster_data_url(frame_records[0])
                review = next((item for item in self.state.version_reviews if item.version_id == self.state.current_version_id), None)
                if review:
                    review.asset = self.state.current_video_asset.copy()
                    review.status = "awaiting_evaluation"
                self.state.status = "repair_uploaded" if self.state.current_version_id != "v1" else "ready_for_evaluation"
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "observe",
                    "event": "generated_video_evidence_ready",
                    "payload": {"message": f"Agent sampled {len(frame_records)} risk-aware evidence frames for {self.state.current_version_id}."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/update-budget":
                editable_statuses = {"planning_ready", "awaiting_first_video", "generating_video", "generated_video_ready", "ready_for_evaluation", "awaiting_repair_upload", "repair_uploaded", "stopped_budget_exhausted", "published", "generation_failed"}
                if self.state.status not in editable_statuses:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "The repair budget can no longer be changed for this project"})
                payload = self._read_json()
                budget = int(payload.get("max_iterations", self.state.creative_spec.max_iterations))
                if budget < self.state.iterations_used or budget > MAX_REPAIR_ITERATIONS:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": f"The repair budget must be between {self.state.iterations_used} and {MAX_REPAIR_ITERATIONS} because {self.state.iterations_used} iteration(s) have already been used"})
                previous_budget = self.state.creative_spec.max_iterations
                self.state.creative_spec.max_iterations = budget
                if budget != previous_budget:
                    self.state.history.append({
                        "at": "local-demo",
                        "phase": "human_approval",
                        "event": "repair_budget_updated",
                        "payload": {"message": f"User changed the repair budget from {previous_budget} to {budget} iteration(s)."},
                    })
                    if self.state.execution_mode == "autopilot" and not self.state.autopilot_paused:
                        self.state.autopilot_paused = True
                        self.state.history.append({
                            "at": "local-demo",
                            "phase": "human_approval",
                            "event": "autopilot_paused_for_budget_change",
                            "payload": {"message": "Agent autonomy paused after the user changed the repair budget. Resume explicitly to continue from the current step."},
                        })
                if self.state.status == "stopped_budget_exhausted" and budget > self.state.iterations_used:
                    ORCHESTRATOR.resume_after_human_approval(self.state)
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/continue-optimization":
                ORCHESTRATOR.prepare_optional_iteration(self.state)
                return self._json(HTTPStatus.OK, present(self.state))
            if path in {"/api/upload-first-video", "/api/replace-first-video"}:
                replacing = path == "/api/replace-first-video"
                expected_status = "ready_for_evaluation" if replacing else "awaiting_first_video"
                if self.state.status != expected_status:
                    message = "Only a V1 awaiting evaluation can be replaced" if replacing else "Approve a control plan before adding the first video"
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": message})
                payload = self._read_json()
                name, mime_type, size, frame_records = self._prepare_video_upload(payload)
                if replacing:
                    self._discard_evaluation_frames(self.state.evaluation_frame_records)
                self.state.current_video_asset = {
                    "name": name,
                    "mime_type": mime_type,
                    "size": str(size),
                    "source": "user_upload",
                    "frame_count": str(len(frame_records)),
                    "poster_data_url": self._poster_data_url(frame_records[0]),
                }
                self.state.evaluation_frame_records = frame_records
                self.state.current_version_id = "v1"
                existing_review = next((item for item in self.state.version_reviews if item.version_id == "v1"), None)
                if existing_review:
                    existing_review.asset = self.state.current_video_asset.copy()
                    existing_review.status = "awaiting_evaluation"
                    existing_review.observation_error = ""
                else:
                    self.state.version_reviews.append(VersionReview(
                        version_id="v1",
                        parent_version_id=None,
                        asset=self.state.current_video_asset.copy(),
                    ))
                self.state.status = "ready_for_evaluation"
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "act",
                    "event": "first_video_replaced" if replacing else "first_video_added",
                    "payload": {"message": f"User {'replaced' if replacing else 'added'} {name} as version v1 for quality evaluation."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/remove-first-video":
                if self.state.status != "ready_for_evaluation":
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "A V1 can be removed only before quality evaluation starts"})
                self._discard_evaluation_frames(self.state.evaluation_frame_records)
                removed_name = (self.state.current_video_asset or {}).get("name", "V1")
                self.state.current_video_asset = None
                self.state.evaluation_frame_records = []
                self.state.current_version_id = "none"
                self.state.version_reviews = [item for item in self.state.version_reviews if item.version_id != "v1"]
                self.state.status = "awaiting_first_video"
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "act",
                    "event": "first_video_removed",
                    "payload": {"message": f"User removed {removed_name} before quality evaluation."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/upload-repaired-video":
                if self.state.status != "awaiting_repair_upload" or not self.state.latest_repair_task:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "A repaired video can be added only after the Agent creates a repair task"})
                payload = self._read_json()
                name, mime_type, size, frame_records = self._prepare_video_upload(payload)
                self._discard_evaluation_frames(self.state.evaluation_frame_records)
                parent_version = self.state.current_version_id
                next_version = self._next_version_id(parent_version)
                self.state.current_video_asset = {
                    "name": name,
                    "mime_type": mime_type,
                    "size": str(size),
                    "source": "user_upload",
                    "frame_count": str(len(frame_records)),
                    "poster_data_url": self._poster_data_url(frame_records[0]),
                }
                self.state.evaluation_frame_records = frame_records
                self.state.current_version_id = next_version
                self.state.version_reviews.append(VersionReview(
                    version_id=next_version,
                    parent_version_id=parent_version,
                    asset=self.state.current_video_asset.copy(),
                ))
                self.state.status = "repair_uploaded"
                self.state.history.append({
                    "at": "local-demo",
                    "phase": "act",
                    "event": "repair_video_added",
                    "payload": {"message": f"User added {name} as {next_version} for verification."},
                })
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/run-loop":
                if self.state.status not in {"ready_for_evaluation", "repair_uploaded"}:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Upload or generate a first video version before evaluation"})
                ORCHESTRATOR.run_once(self.state)
                return self._json(HTTPStatus.OK, present(self.state))
            if path == "/api/reset":
                payload=self._read_json();name=payload.get("title","")
                if not isinstance(name,str) or len(name.strip())>80:raise ValueError("项目名称需要少于 80 个字符。")
                self.state = create_empty_state()
                self.state.project_name=name.strip()
                return self._json(HTTPStatus.OK, present(self.state))
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
        except ValueError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if hasattr(self, "visitor_cookie"):
            self.send_header("Set-Cookie", f"agent_session={self.visitor_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=315360000")
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        # Keep terminal output compact for a live demo.
        return

    def _json(self, status: HTTPStatus, data: dict):
        if hasattr(self, "visitor_id"):
            SESSIONS.save(self.visitor_id, self.state)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_generated_video(self, version_id: str):
        review = next((item for item in self.state.version_reviews if item.version_id == version_id), None)
        asset = review.asset if review else None
        raw_path = (asset or {}).get("local_video_path")
        if not isinstance(raw_path, str):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Generated video is unavailable"})
        path = Path(raw_path)
        root = (RUNTIME / "generated").resolve()
        try:
            path.resolve().relative_to(root)
        except (ValueError, OSError):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid generated video path"})
        if not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Generated video file is unavailable"})
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("This request contains too much reference media. Please use smaller images or shorter audio files.")
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("JSON body must be an object")
            return result
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request body") from exc

    def _pause_autopilot_for_failure(self, event: str, message: str) -> None:
        if self.state.execution_mode != "autopilot" or self.state.autopilot_paused:
            return
        self.state.autopilot_paused = True
        self.state.history.append({
            "at": "local-demo",
            "phase": "human_approval",
            "event": event,
            "payload": {"message": message},
        })

    def _generation_failed(self, job, review):
        """End a failed local handoff without silently resubmitting a paid job."""
        self.state.status = "awaiting_repair_upload" if job.version_id != "v1" else "awaiting_first_video"
        error = job.error or "Video generation failed or was canceled."
        if review:
            review.status = "generation_failed"
            review.observation_error = error
        self._pause_autopilot_for_failure(
            "autopilot_paused_for_generation_failure",
            "Agent autonomy paused because the video provider did not complete the current task. No replacement generation will be submitted until the user explicitly resumes it.",
        )
        self.state.history.append({
            "at": "local-demo",
            "phase": "act",
            "event": "generation_failed",
            "payload": {"message": error},
        })
        return self._json(HTTPStatus.OK, present(self.state))

    @staticmethod
    def _normalise_multimodal_assets(payload: dict) -> list[dict[str, str]]:
        """Validate retained multimodal assets before they enter Agent memory."""
        raw_assets = payload.get("multimodal_assets")
        if raw_assets is None:
            legacy = payload.get("reference_asset")
            raw_assets = [legacy] if legacy else []
        if not isinstance(raw_assets, list):
            raise ValueError("Reference assets must be a list")
        if len(raw_assets) > 50:
            raise ValueError("Use at most 30 images, 10 audio files and 10 video URLs")
        images = 0
        audio = 0
        videos = 0
        normalised: list[dict[str, str]] = []
        seen_names: set[str] = set()
        for input_order, raw in enumerate(raw_assets, start=1):
            if not isinstance(raw, dict):
                raise ValueError("Every reference asset must be an object")
            mime_type = str(raw.get("mime_type", "")).lower().strip()
            data_url = raw.get("data_url")
            name = str(raw.get("name", "reference asset")).strip()[:120]
            name_key = name.casefold()
            if name_key in seen_names:
                raise ValueError("Reference filenames must be unique for @ reference binding")
            seen_names.add(name_key)
            role = str(raw.get("role", "reference")).strip()[:80]
            raw_mentions = raw.get("prompt_mentions", [])
            if not isinstance(raw_mentions, list):
                raw_mentions = []
            prompt_mentions = [str(item).strip()[:140] for item in raw_mentions if str(item).strip()][:3]
            if mime_type.startswith("image/") and isinstance(data_url, str) and data_url.startswith("data:image/"):
                images += 1
                if images > 30:
                    raise ValueError("Use at most 30 reference images in this local demo")
                if len(data_url) > 2_000_000:
                    raise ValueError("Each reference image must be under 1.5 MB after browser compression")
                normalised.append({
                    "name": name or "reference image", "mime_type": mime_type,
                    "size": str(raw.get("size", 0)), "role": role or "Visual reference",
                    "media_type": "image", "preview_data_url": data_url,
                    "prompt_mentions": prompt_mentions, "input_order": input_order,
                })
            elif mime_type in {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"} and isinstance(data_url, str) and data_url.startswith("data:audio/"):
                audio += 1
                if audio > 10:
                    raise ValueError("Use at most 10 reference audio files in this local demo")
                try:
                    raw_size = int(raw.get("size", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Audio reference size is invalid") from exc
                if raw_size <= 0 or raw_size > 9 * 1024 * 1024:
                    raise ValueError("Each reference audio file must be under 9 MB for this local demo")
                # Base64 adds roughly one third to a browser file upload, so
                # this guard must be larger than the original 9 MB file limit.
                if len(data_url) > 13 * 1024 * 1024:
                    raise ValueError("Each reference audio file must be under 9 MB for this local demo")
                raw_duration = raw.get("duration_seconds")
                duration_seconds = None
                if raw_duration is not None:
                    try:
                        duration_seconds = round(float(raw_duration), 2)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Audio reference duration is invalid") from exc
                    if not 2 <= duration_seconds <= 30:
                        raise ValueError("Audio references must be 2–30 seconds; compatible models are filtered later")
                normalised.append({
                    "name": name or "reference audio", "mime_type": mime_type,
                    "size": str(raw.get("size", 0)), "role": role or "Audio reference",
                    "media_type": "audio", "data_url": data_url,
                    "prompt_mentions": prompt_mentions, "input_order": input_order,
                    **({"duration_seconds": duration_seconds} if duration_seconds is not None else {}),
                })
            elif mime_type in {"video/mp4", "video/quicktime"}:
                videos += 1
                url = str(raw.get("url", ""))
                parsed = urlparse(url)
                duration = float(raw.get("duration_seconds", 0))
                if videos > 10 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not 2 <= duration <= 30:
                    raise ValueError("参考视频需提供有效 HTTPS 链接和 2–30 秒时长；最多 10 个。")
                normalised.append({"name":name, "mime_type":mime_type, "media_type":"video", "url":url,
                                   "duration_seconds":duration, "size":"0", "role":"reference_video", "prompt_mentions":prompt_mentions, "input_order":input_order})
            else:
                raise ValueError("Reference assets must be images, MP3/WAV audio or public MP4/MOV URLs")
        return normalised

    def _prepare_video_upload(self, payload: dict) -> tuple[str, str, int, list[dict[str, object]]]:
        name = str(payload.get("name", "")).strip()
        mime_type = str(payload.get("mime_type", "")).strip()
        size = int(payload.get("size", 0))
        if not name or not mime_type.startswith("video/"):
            raise ValueError("Choose an MP4, WebM, or MOV video file")
        if size <= 0 or size > 500 * 1024 * 1024:
            raise ValueError("Video files must be between 1 byte and 500 MB")
        return name, mime_type, size, self._store_evaluation_frames(payload.get("frames"))

    @staticmethod
    def _download_generated_video(video_url: str, version_id: str) -> tuple[Path, int]:
        parsed = urlparse(video_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("The video provider returned an invalid video URL")
        target_dir = RUNTIME / "generated"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{version_id}-{uuid.uuid4().hex}.mp4"
        request = urllib_request.Request(video_url, headers={"User-Agent": "VideoControlLoopAgent/1.0"})
        try:
            with urllib_request.urlopen(request, timeout=90) as response, target.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 120 * 1024 * 1024:
                        raise ValueError("Generated video exceeds the 120 MB local-demo limit")
                    output.write(chunk)
        except ValueError:
            target.unlink(missing_ok=True)
            raise
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise ValueError("The completed video could not be downloaded for local evaluation") from exc
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise ValueError("The video provider returned an empty video file")
        return target, target.stat().st_size

    @staticmethod
    def _next_version_id(version_id: str) -> str:
        if version_id.startswith("v") and version_id[1:].isdigit():
            return f"v{int(version_id[1:]) + 1}"
        raise ValueError("The current video version is invalid")

    @staticmethod
    def _store_evaluation_frames(raw_frames) -> list[dict[str, object]]:
        if not isinstance(raw_frames, list) or not 3 <= len(raw_frames) <= 6:
            raise ValueError("The browser must provide 3 to 6 sampled keyframes for visual evaluation")
        decoded: list[tuple[str, float, bytes, str, str]] = []
        for index, frame in enumerate(raw_frames, start=1):
            if not isinstance(frame, dict):
                raise ValueError("Invalid sampled keyframe")
            data_url = frame.get("data_url")
            timestamp = frame.get("timestamp")
            expected_shot_id = frame.get("expected_shot_id")
            sampling_reason = frame.get("sampling_reason")
            prefix = "data:image/jpeg;base64,"
            if not isinstance(data_url, str) or not data_url.startswith(prefix):
                raise ValueError("Sampled keyframes must be JPEG images")
            if not isinstance(timestamp, (int, float)) or timestamp < 0:
                raise ValueError("Invalid keyframe timestamp")
            planned_shot_ids = {str(shot.get("shot_id")) for shot in self.state.creative_spec.shots if isinstance(shot, dict)}
            if expected_shot_id not in planned_shot_ids:
                raise ValueError("Each evidence frame must identify its planned shot")
            if not isinstance(sampling_reason, str) or not sampling_reason.strip() or len(sampling_reason.strip()) > 240:
                raise ValueError("Each evidence frame must include a concise sampling reason")
            try:
                image = base64.b64decode(data_url[len(prefix):], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("A sampled keyframe could not be decoded") from exc
            if not image or len(image) > 1024 * 1024:
                raise ValueError("Each sampled keyframe must be below 1 MB")
            decoded.append((f"F{index}", float(timestamp), image, expected_shot_id, sampling_reason.strip()))
        directory = RUNTIME / "frames" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        records = []
        for frame_id, timestamp, image, expected_shot_id, sampling_reason in decoded:
            path = directory / f"{frame_id.lower()}.jpg"
            path.write_bytes(image)
            records.append({
                "frame_id": frame_id,
                "timestamp": timestamp,
                "path": str(path),
                "expected_shot_id": expected_shot_id,
                "sampling_reason": sampling_reason,
            })
        return records

    @staticmethod
    def _poster_data_url(record: dict[str, str | float]) -> str:
        image = Path(str(record["path"])).read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")

    @staticmethod
    def _discard_evaluation_frames(records: list[dict[str, str | float]]) -> None:
        """Remove only the per-upload temporary frame directory owned by this request."""
        if not records:
            return
        directory = Path(str(records[0].get("path", ""))).parent
        runtime_frames = (RUNTIME / "frames").resolve()
        try:
            directory.resolve().relative_to(runtime_frames)
        except (ValueError, OSError):
            return
        if directory.is_dir():
            shutil.rmtree(directory)


def run(port: int | None = None):
    port = port or int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DemoHandler)
    ImageTasks(SESSIONS).recover()
    with SESSIONS.connect() as db:
        db.execute("UPDATE projects SET title_source='pending' WHERE title_source='naming'")
    print(f"Video Control Loop Agent running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
