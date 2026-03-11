from __future__ import annotations

import json
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import (
    DEFAULT_SYSTEM_INSTRUCTIONS,
    ModerationError,
    SUPPORTED_RESOLUTIONS,
    create_client,
    extract_last_frame,
    finalize_session,
    generate_video,
    preflight_check,
    refine_prompt,
    unique_output_path,
)
from .pricing import PricingStore
from .storyboard import generate_storyboard_html


def generate_session_id() -> str:
    return secrets.token_hex(3)


def utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y"}


@dataclass
class SessionState:
    session_id: str
    mode: str
    output_dir: Path
    session_dir: Path | None = None
    title: str | None = None
    status: str = "queued"
    current_part: int = 0
    total_parts: int = 0
    pending_action: str | None = None
    allowed_actions: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    refined_prompts: list[str] = field(default_factory=list)
    accepted_prompts: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str | None = None
    playlists: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    parent_session_id: str | None = None
    parent_clip_index: int | None = None
    budget_cap: float | None = None
    costs: dict[str, Any] = field(
        default_factory=lambda: {
            "currency": "USD",
            "total": 0.0,
            "items": [],
            "violation_fee": 0.05,
        }
    )
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_action: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.session_dir is None:
            self.session_dir = self.output_dir / self.session_id

    def log(self, message: str) -> None:
        with self._lock:
            timestamped = f"[{utc_now()}] {message}"
            self.logs.append(timestamped)
            self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "title": self.title,
                "mode": self.mode,
                "status": self.status,
                "current_part": self.current_part,
                "total_parts": self.total_parts,
                "pending_action": self.pending_action,
                "allowed_actions": list(self.allowed_actions),
                "logs": list(self.logs),
                "outputs": list(self.outputs),
                "prompts": list(self.prompts),
                "refined_prompts": list(self.refined_prompts),
                "accepted_prompts": list(self.accepted_prompts),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "last_error": self.last_error,
                "playlists": dict(self.playlists),
                "settings": dict(self.settings),
                "parent_session_id": self.parent_session_id,
                "parent_clip_index": self.parent_clip_index,
                "budget_cap": self.budget_cap,
                "costs": dict(self.costs),
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any], output_dir: Path, session_dir: Path | None = None) -> SessionState:
        return cls(
            session_id=data["session_id"],
            title=data.get("title"),
            mode=data.get("mode", "unknown"),
            output_dir=output_dir,
            session_dir=session_dir,
            status=data.get("status", "queued"),
            current_part=data.get("current_part", 0),
            total_parts=data.get("total_parts", 0),
            pending_action=data.get("pending_action"),
            allowed_actions=data.get("allowed_actions", []),
            logs=data.get("logs", []),
            outputs=data.get("outputs", []),
            prompts=data.get("prompts", []),
            refined_prompts=data.get("refined_prompts", []),
            accepted_prompts=data.get("accepted_prompts", []),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            last_error=data.get("last_error"),
            playlists=data.get("playlists", {}),
            settings=data.get("settings", {}),
            parent_session_id=data.get("parent_session_id"),
            parent_clip_index=data.get("parent_clip_index"),
            budget_cap=data.get("budget_cap"),
            costs=data.get("costs", {
                "currency": "USD",
                "total": 0.0,
                "items": [],
                "violation_fee": 0.05,
            }),
        )

    def set_title(self, title: str) -> None:
        with self._lock:
            self.title = title
            self.updated_at = utc_now()

    def init_costs(self, currency: str, violation_fee: float) -> None:
        with self._lock:
            self.costs = {
                "currency": currency,
                "total": 0.0,
                "items": [],
                "violation_fee": violation_fee,
            }

    def add_cost(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.costs.setdefault("items", []).append(item)
            self.costs["total"] = round(float(self.costs.get("total", 0.0)) + item["total_cost"], 4)
            self.updated_at = utc_now()

    def update_status(self, status: str) -> None:
        with self._lock:
            self.status = status
            self.updated_at = utc_now()

    def set_pending_action(self, prompt: str, actions: list[str]) -> None:
        with self._lock:
            self.pending_action = prompt
            self.allowed_actions = actions
            self._last_action = None
            self.status = "waiting"
            self.updated_at = utc_now()
        self._event.clear()

    def wait_for_action(self) -> str:
        self._event.wait()
        with self._lock:
            action = self._last_action
            self.pending_action = None
            self.allowed_actions = []
            self.status = "running"
            self.updated_at = utc_now()
            return action or ""

    def submit_action(self, action: str) -> None:
        with self._lock:
            if self.allowed_actions and action not in self.allowed_actions:
                raise ValueError(f"Action '{action}' not allowed: {self.allowed_actions}")
            self._last_action = action
        self._event.set()

    def write_session_json(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_path = self.session_dir / f"{self.session_id}_session.json"
        session_path.write_text(json.dumps(self.to_dict(), indent=2))


@dataclass
class SingleRequest:
    image_path: Path
    prompt: str
    duration: int
    resolution: str
    api_key: str
    api_host: str
    preflight: bool
    system_instructions: str
    grounding_text: str
    refine_prompts: bool = False
    refine_auto_accept: bool = False


@dataclass
class MultiRequest:
    image_path: Path
    prompts: list[str]
    duration: int
    resolution: str
    api_key: str
    api_host: str
    preflight: bool
    system_instructions: str
    refine_prompts: bool
    refine_auto_accept: bool
    grounding_text: str


class SessionManager:
    def __init__(self, output_dir: Path, pricing_store: PricingStore) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self.pricing = pricing_store
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Load persisted sessions from the output directory and subdirectories."""
        # Load from root directory
        for path in self.output_dir.glob("*_session.json"):
            self._load_single_session(path)
        
        # Load from immediate subdirectories (completed sessions)
        for subdir in self.output_dir.iterdir():
            if subdir.is_dir():
                # Check for session file inside the subdirectory
                # The session file is named {session_id}_session.json
                # We can glob inside the subdir
                for path in subdir.glob("*_session.json"):
                    self._load_single_session(path)

    def _load_single_session(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text())
            # Use the directory containing the session file as the output_dir for that session
            # This ensures relative paths resolve correctly
            session_dir = path.parent
            session = SessionState.from_dict(data, self.output_dir, session_dir)
            # Ensure we don't load sessions that are in a transient state as 'running' if we crashed
            if session.status in ("running", "waiting", "queued"):
                session.status = "stopped"
            self._sessions[session.session_id] = session
        except Exception as exc:
            # Log error but don't crash
            print(f"Error loading session from {path}: {exc}")
            pass

    def open_folder(self, session_id: str) -> None:
        import subprocess
        import platform
        
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session: {session_id}")
            session = self._sessions[session_id]
            
        path = session.session_dir.resolve()
        
        system = platform.system()
        try:
            if system == "Darwin":  # macOS
                subprocess.Popen(["open", str(path)])
            elif system == "Windows":
                subprocess.Popen(["explorer", str(path)])
            else:  # Linux
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            raise RuntimeError(f"Failed to open folder: {exc}") from exc

    def _record_cost(
        self,
        session: SessionState,
        kind: str,
        duration: int,
        resolution: str,
        status: str,
        penalty: float = 0.0,
    ) -> dict[str, Any]:
        rate = self.pricing.get_video_rate(resolution)
        base_cost = round(rate * duration, 4)
        total_cost = round(base_cost + penalty, 4)
        item = {
            "kind": kind,
            "status": status,
            "duration": duration,
            "resolution": resolution,
            "rate_per_second": rate,
            "base_cost": base_cost,
            "penalty": penalty,
            "total_cost": total_cost,
            "currency": self.pricing.currency,
            "timestamp": utc_now(),
        }
        session.add_cost(item)

        # Budget Check
        if session.budget_cap is not None and session.costs["total"] >= session.budget_cap:
            session.log(f"Budget cap exceeded (${session.budget_cap:.2f}). Waiting for approval...")
            session.set_pending_action("budget_exceeded", ["raise_cap", "continue_anyway", "stop"])
            action = session.wait_for_action()
            
            if action == "stop":
                raise RuntimeError("Session stopped due to budget cap")
            # For 'continue_anyway' or 'raise_cap', we proceed.
            # Ideally 'raise_cap' logic (updating the cap) happens via an API call before the user clicks the button,
            # or the user updates the cap and then clicks "continue".
            # The simple "raise_cap" action here just signals "I have raised it, proceed".
            
        return item

    def create_session(self, mode: str, total_parts: int = 1, title: str | None = None) -> SessionState:
        while True:
            session_id = generate_session_id()
            with self._lock:
                if session_id not in self._sessions:
                    break
        
        session = SessionState(session_id=session_id, mode=mode, output_dir=self.output_dir, title=title)
        session.total_parts = total_parts
        session.update_status("created")
        session.write_session_json()
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> SessionState:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session: {session_id}")
            return self._sessions[session_id]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(),
                key=lambda s: s.updated_at,
                reverse=True,
            )
            return [session.to_dict() for session in sessions]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session: {session_id}")
            session = self._sessions.pop(session_id)
            
        # Delete directory
        # SAFETY CHECK: Ensure we don't delete the root output directory
        if session.session_dir.resolve() == self.output_dir.resolve():
            # If session is in root (legacy), try to delete just its known files?
            # For now, just avoid the catastrophic rmtree.
            raise ValueError(f"Cannot delete session {session_id} because it resides in the root directory.")

        if session.session_dir.exists():
            shutil.rmtree(session.session_dir)

    def create_archive(self, session_id: str) -> Path:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session: {session_id}")
            session = self._sessions[session_id]
            # Capture session state for storyboard generation
            session_data = session.to_dict()

        # Generate storyboard.html
        try:
            storyboard_html = generate_storyboard_html(session_data)
            storyboard_path = session.session_dir / "storyboard.html"
            storyboard_path.write_text(storyboard_html)
        except Exception as exc:
            # Don't fail archive creation if storyboard generation fails
            # But log it? We don't have a logger here. Just ignore.
            pass

        archive_base = self.output_dir / f"{session_id}_archive"
        archive_path = shutil.make_archive(
            str(archive_base),
            "zip",
            root_dir=session.session_dir
        )
        return Path(archive_path)

    def fork_session(self, parent_session_id: str, part_index: int, title: str | None = None) -> tuple[SessionState, str]:
        """Fork a new session from a specific part of an existing session."""
        with self._lock:
            if parent_session_id not in self._sessions:
                raise KeyError(f"Unknown session: {parent_session_id}")
            parent_session = self._sessions[parent_session_id]
            
            # Find the video output for the requested part
            target_output = None
            for output in parent_session.outputs:
                if output.get("type") == "video" and output.get("part") == part_index and output.get("status") != "aborted":
                    target_output = output
                    break
            
            if not target_output:
                raise ValueError(f"Part {part_index} not found or invalid in session {parent_session_id}")

        # Create new session
        new_session = self.create_session(
            mode="multi", # Forked sessions are inherently multi-part/extensions
            total_parts=0, # Will be set when prompts are added
            title=title or f"Fork of {parent_session.title or parent_session_id}"
        )
        
        # Record parentage
        new_session.parent_session_id = parent_session_id
        new_session.parent_clip_index = part_index
        
        # Copy settings and context
        # We deep copy settings to avoid reference issues, though they are simple dicts
        new_session.settings = dict(parent_session.settings)
        
        # Extract the frame to the new session directory
        # We need to resolve the path of the source video. 
        # The output path in 'outputs' is just the filename relative to the session dir.
        source_video_path = parent_session.session_dir / target_output["path"]
        
        if not source_video_path.exists():
             raise FileNotFoundError(f"Source video not found: {source_video_path}")

        frame_filename = f"{new_session.session_id}_input.png"
        frame_path = new_session.session_dir / frame_filename
        
        extract_last_frame(source_video_path, frame_path)
        
        # Copy specific prompt instructions if they exist in the parent's prompt for that part?
        # Actually, we probably just want the system instructions if they were global.
        # But 'prompt' in session.settings isn't really a thing, prompts are per-request.
        # The prompt for the *next* part is up to the user.
        # However, we might want to capture grounding text if it was part of the prompt construction?
        # For now, we rely on the user to re-enter or we could try to parse it, but that's brittle.
        # The plan said: "Copy settings, grounding_text (if stored), and system_instructions from parent"
        # We don't strictly store grounding_text separately in session state, it's baked into the prompt.
        # We DO store settings.
        
        new_session.write_session_json()
        
        return new_session, frame_filename

    def start_single(self, session: SessionState, request: SingleRequest) -> None:
        with self._lock:
            if session.status == "running":
                raise RuntimeError(f"Session {session.session_id} is already running")
            session.update_status("running")
            session.write_session_json()
        thread = threading.Thread(
            target=self._run_single,
            args=(session, request),
            daemon=True,
        )
        thread.start()

    def start_multi(self, session: SessionState, request: MultiRequest) -> None:
        # Update state synchronously to prevent races
        with self._lock:
            session.init_costs(self.pricing.currency, self.pricing.violation_fee)
            session.prompts = list(request.prompts)
            session.total_parts = len(request.prompts)
            session.update_status("running")
            session.write_session_json()

        thread = threading.Thread(
            target=self._run_multi,
            args=(session, request, 1),
            daemon=True,
        )
        thread.start()

    def append_multi(self, session: SessionState, request: MultiRequest) -> None:
        with self._lock:
            # We start after the last known part
            start_index = len(session.prompts) - len(request.prompts) + 1
            # Wait, main.py will now send ONLY new prompts. 
            # So start_index should be existing + 1.
            # But we are changing the contract. 
            # Let's assume main.py sends ONLY new prompts for append.
            # But currently main.py sends ALL prompts.
            # We need to change main.py first or handle it here.
            # The plan says "Update session.prompts synchronously".
            # So we assume request.prompts contains ONLY the new prompts to append.
            
            # Re-read prompts from session to be safe under lock
            current_len = len(session.prompts)
            start_index = current_len + 1
            
            session.prompts.extend(request.prompts)
            session.total_parts += len(request.prompts)
            session.update_status("running")
            session.write_session_json()

        thread = threading.Thread(
            target=self._run_multi,
            args=(session, request, start_index),
            daemon=True,
        )
        thread.start()

    def _run_single(self, session: SessionState, request: SingleRequest) -> None:
        client = create_client(api_key=request.api_key, api_host=request.api_host)
        if not session.costs.get("items"):
            session.init_costs(self.pricing.currency, self.pricing.violation_fee)
        
        part_index = len([o for o in session.outputs if o.get("type") == "video" and o.get("status") != "aborted"]) + 1
        session.current_part = part_index
        session.total_parts = part_index
        
        prompt = request.prompt

        # Step 1: Refine with Grok (if enabled)
        if request.refine_prompts and prompt:
            session.log("Refining prompt with Grok...")
            try:
                refined = refine_prompt(client, request.image_path, prompt)
                session.refined_prompts.append(refined)
                if request.refine_auto_accept:
                    prompt = refined
                    session.accepted_prompts.append(refined)
                else:
                    session.set_pending_action("use_refined", ["use_refined", "use_original"])
                    action = session.wait_for_action()
                    if action == "use_refined":
                        prompt = refined
                        session.accepted_prompts.append(refined)
                    else:
                        session.accepted_prompts.append(request.prompt)
            except Exception as exc:
                session.log(f"Prompt refinement failed: {exc}")
                session.accepted_prompts.append(request.prompt)
        else:
            session.accepted_prompts.append(request.prompt)

        # Step 2: Apply grounding + system instructions
        if request.grounding_text:
            prompt = f"GROUNDING CONTEXT:\n{request.grounding_text}\n\nPROMPT:\n{prompt}"

        if request.system_instructions:
            prompt = f"{prompt}\n\n{request.system_instructions}"
        else:
            prompt = f"{prompt}\n\n{DEFAULT_SYSTEM_INSTRUCTIONS}"

        session.settings = {
            "duration": request.duration,
            "resolution": request.resolution,
            "preflight": request.preflight,
            "api_host": request.api_host,
            "refine_prompts": request.refine_prompts,
        }
        session.prompts.append(request.prompt)
        session.write_session_json()

        base_output = session.session_dir / f"{session.session_id}_part{part_index}.mp4"
        preflight_done = False
        
        while True:
            video_path = unique_output_path(base_output)
            preflight_path = unique_output_path(session.session_dir / f"{session.session_id}_{part_index}_preflight.mp4")
            try:
                if request.preflight and not preflight_done:
                    session.log("Running preflight check...")
                    try:
                        preflight_url = preflight_check(
                            client,
                            request.image_path,
                            prompt,
                            preflight_path,
                        )
                    except ModerationError as exc:
                        # Pre-gen: $0.05 only. Post-gen: video cost + $0.05
                        duration_charged = 0 if exc.pre_generation else 1
                        self._record_cost(
                            session, "preflight", duration_charged, "480p", "failed",
                            self.pricing.violation_fee,
                        )
                        raise
                    except Exception:
                        self._record_cost(session, "preflight", 1, "480p", "failed")
                        raise
                    preflight_cost = self._record_cost(session, "preflight", 1, "480p", "success")
                    session.outputs.append({
                        "type": "preflight",
                        "part": part_index,
                        "path": str(preflight_path.name),
                        "url": preflight_url,
                        "cost": preflight_cost,
                        "prompt": prompt,
                    })
                    preflight_done = True
                    session.write_session_json()
                
                session.log("Generating video...")
                try:
                    video_url = generate_video(
                        client,
                        request.image_path,
                        prompt,
                        video_path,
                        request.duration,
                        request.resolution,
                    )
                except ModerationError as exc:
                    # Pre-gen: $0.05 only. Post-gen: video cost + $0.05
                    duration_charged = 0 if exc.pre_generation else request.duration
                    self._record_cost(
                        session, "video", duration_charged, request.resolution,
                        "failed", self.pricing.violation_fee,
                    )
                    raise
                except Exception:
                    self._record_cost(
                        session, "video", request.duration, request.resolution, "failed",
                    )
                    raise
                video_cost = self._record_cost(
                    session, "video", request.duration, request.resolution, "success",
                )
                session.outputs.append({
                    "type": "video",
                    "part": part_index,
                    "path": str(video_path.name),
                    "url": video_url,
                    "cost": video_cost,
                    "prompt": prompt,
                })
                session.write_session_json()
            except RuntimeError as exc:
                if str(exc) == "Session stopped due to budget cap":
                    session.log("Session stopped by user (budget cap).")
                    session.update_status("stopped")
                    session.write_session_json()
                    return
                session.last_error = str(exc)
                session.log(f"Error: {exc}")
                session.write_session_json()
                session.set_pending_action("retry", ["retry", "stop"])
                action = session.wait_for_action()
                if action != "retry":
                    session.update_status("failed")
                    session.write_session_json()
                    return
                continue
            except Exception as exc:
                session.last_error = str(exc)
                session.log(f"Error: {exc}")
                session.write_session_json()
                session.set_pending_action("retry", ["retry", "stop"])
                action = session.wait_for_action()
                if action != "retry":
                    session.update_status("failed")
                    session.write_session_json()
                    return
                continue

            # Generation succeeded — ask user what to do
            session.set_pending_action("clip_done", ["extend", "regenerate", "end"])
            action = session.wait_for_action()
            
            if action == "regenerate":
                # Mark the video output as aborted in JSON (no file rename)
                for out in session.outputs:
                    if out.get("path") == str(video_path.name) and out.get("status") != "aborted":
                        out["status"] = "aborted"
                session.log("Clip rejected — awaiting new prompt.")
                session.update_status("completed")
                session.write_session_json()
                return

            if action == "extend":
                # Extract last frame for the next clip
                frame_path = session.session_dir / f"{session.session_id}_part{part_index}_lastframe.png"
                try:
                    extract_last_frame(video_path, frame_path)
                    session.log(f"Last frame extracted for extension.")
                except Exception as exc:
                    session.log(f"Frame extraction failed: {exc}")
                self._finalize_playlists(session)
                session.update_status("completed")
                session.write_session_json()
                return

            # action == "end"
            self._finalize_playlists(session)
            session.update_status("completed")
            session.write_session_json()
            return

    def _finalize_playlists(self, session: SessionState) -> None:
        accepted_preflights = [
            session.session_dir / out["path"]
            for out in session.outputs
            if out["type"] == "preflight" and out.get("status") != "aborted"
        ]
        accepted_videos = [
            session.session_dir / out["path"]
            for out in session.outputs
            if out["type"] == "video" and out.get("status") != "aborted"
        ]
        preflight_playlist, final_playlist = finalize_session(
            session.session_id,
            accepted_preflights,
            accepted_videos,
            session.session_dir,
        )
        session.playlists = {
            "preflight": preflight_playlist.name,
            "final": final_playlist.name,
        }

    def _run_multi(self, session: SessionState, request: MultiRequest, start_index: int = 1) -> None:
        masked_key = f"{request.api_key[:4]}...{request.api_key[-4:]}" if request.api_key else "None"
        session.log(f"Starting session with host={request.api_host}, key={masked_key}")
        client = create_client(api_key=request.api_key, api_host=request.api_host)
        
        # Only init costs if this is a fresh start
        if start_index == 1:
            session.init_costs(self.pricing.currency, self.pricing.violation_fee)
            session.prompts = list(request.prompts)
            session.total_parts = len(request.prompts)
        else:
            # We are appending
            session.prompts.extend(request.prompts)
            session.total_parts += len(request.prompts)

        session.settings = {
            "duration": request.duration,
            "resolution": request.resolution,
            "preflight": request.preflight,
            "api_host": request.api_host,
            "refine_prompts": request.refine_prompts,
        }

        current_image = request.image_path

        # Reconstruct accepted lists from existing outputs when appending
        if start_index > 1:
            accepted_preflights = [
                session.session_dir / out["path"]
                for out in session.outputs
                if out["type"] == "preflight" and out.get("status") != "aborted"
            ]
            accepted_videos = [
                session.session_dir / out["path"]
                for out in session.outputs
                if out["type"] == "video" and out.get("status") != "aborted"
            ]
        else:
            accepted_preflights = []
            accepted_videos = []

        for idx, raw_prompt in enumerate(request.prompts, start=start_index):
            session.current_part = idx
            session.write_session_json()
            prompt = raw_prompt

            if request.refine_prompts and raw_prompt:
                session.log("Refining prompt with Grok...")
                try:
                    refined = refine_prompt(client, current_image, raw_prompt)
                    session.refined_prompts.append(refined)
                    if request.refine_auto_accept:
                        prompt = refined
                        session.accepted_prompts.append(refined)
                    else:
                        session.set_pending_action("use_refined", ["use_refined", "use_original"])
                        action = session.wait_for_action()
                        if action == "use_refined":
                            prompt = refined
                            session.accepted_prompts.append(refined)
                        else:
                            session.accepted_prompts.append(raw_prompt)
                except Exception as exc:
                    session.log(f"Prompt refinement failed: {exc}")
                    session.accepted_prompts.append(raw_prompt)
            else:
                session.accepted_prompts.append(raw_prompt)

            if request.grounding_text:
                prompt = f"GROUNDING CONTEXT:\n{request.grounding_text}\n\nPROMPT:\n{prompt}"

            if request.system_instructions:
                prompt = f"{prompt}\n\n{request.system_instructions}"
            else:
                prompt = f"{prompt}\n\n{DEFAULT_SYSTEM_INSTRUCTIONS}"

            base_image = current_image

            while True:
                preflight_path = unique_output_path(session.session_dir / f"{session.session_id}_{idx}_preflight.mp4")
                video_path = unique_output_path(session.session_dir / f"{session.session_id}_part{idx}.mp4")
                frame_path = unique_output_path(session.session_dir / f"{session.session_id}_part{idx}_finalframe.png")

                if request.preflight:
                    while True:
                        session.log("Running preflight check...")
                        try:
                            preflight_url = preflight_check(
                                client,
                                base_image,
                                prompt,
                                preflight_path,
                            )
                            break
                        except ModerationError as exc:
                            # Pre-gen: $0.05 only. Post-gen: video cost + $0.05
                            duration_charged = 0 if exc.pre_generation else 1
                            self._record_cost(
                                session,
                                "preflight",
                                duration_charged,
                                "480p",
                                "failed",
                                self.pricing.violation_fee,
                            )
                            session.last_error = str(exc)
                            session.log(f"Preflight error: {exc}")
                            session.write_session_json()
                            session.set_pending_action("retry_preflight", ["retry", "stop"])
                            action = session.wait_for_action()
                            if action != "retry":
                                session.update_status("failed")
                                session.write_session_json()
                                return
                        except Exception as exc:
                            self._record_cost(session, "preflight", 1, "480p", "failed")
                            session.last_error = str(exc)
                            session.log(f"Preflight error: {exc}")
                            session.write_session_json()
                            session.set_pending_action("retry_preflight", ["retry", "stop"])
                            action = session.wait_for_action()
                            if action != "retry":
                                session.update_status("failed")
                                session.write_session_json()
                                return
                    preflight_cost = self._record_cost(session, "preflight", 1, "480p", "success")
                    session.outputs.append(
                        {
                            "type": "preflight",
                            "part": idx,
                            "path": str(preflight_path.name),
                            "url": preflight_url,
                            "cost": preflight_cost,
                            "prompt": prompt,
                        }
                    )
                else:
                    preflight_url = ""

                while True:
                    session.log("Generating video...")
                    try:
                        video_url = generate_video(
                            client,
                            base_image,
                            prompt,
                            video_path,
                            request.duration,
                            request.resolution,
                        )
                        break
                    except ModerationError as exc:
                        # Pre-gen: $0.05 only. Post-gen: video cost + $0.05
                        duration_charged = 0 if exc.pre_generation else request.duration
                        self._record_cost(
                            session,
                            "video",
                            duration_charged,
                            request.resolution,
                            "failed",
                            self.pricing.violation_fee,
                        )
                        session.last_error = str(exc)
                        session.log(f"Generation error: {exc}")
                        session.write_session_json()
                        session.set_pending_action("retry_generation", ["retry", "stop"])
                        action = session.wait_for_action()
                        if action != "retry":
                            session.update_status("failed")
                            session.write_session_json()
                            return
                    except Exception as exc:
                        self._record_cost(
                            session,
                            "video",
                            request.duration,
                            request.resolution,
                            "failed",
                        )
                        session.last_error = str(exc)
                        session.log(f"Generation error: {exc}")
                        session.write_session_json()
                        session.set_pending_action("retry_generation", ["retry", "stop"])
                        action = session.wait_for_action()
                        if action != "retry":
                            session.update_status("failed")
                            session.write_session_json()
                            return

                video_cost = self._record_cost(
                    session,
                    "video",
                    request.duration,
                    request.resolution,
                    "success",
                )
                session.outputs.append(
                    {
                        "type": "video",
                        "part": idx,
                        "path": str(video_path.name),
                        "url": video_url,
                        "cost": video_cost,
                        "prompt": prompt,
                    }
                )
                session.write_session_json()

                is_final = idx == session.total_parts
                if is_final:
                    session.set_pending_action("end", ["end", "regenerate", "stop"])
                else:
                    session.set_pending_action("continue", ["continue", "regenerate", "stop"])
                action = session.wait_for_action()

                if action == "regenerate":
                    # Mark outputs as aborted in JSON (no file rename)
                    for p in [preflight_path, video_path, frame_path]:
                        for out in session.outputs:
                            if out.get("path") == str(p.name) and out.get("status") != "aborted":
                                out["status"] = "aborted"
                    session.write_session_json()
                    session.log("Regenerating part...")
                    continue

                if request.preflight:
                    accepted_preflights.append(preflight_path)
                accepted_videos.append(video_path)

                if action == "stop":
                    session.update_status("stopped")
                    session.write_session_json()
                    preflight_playlist, final_playlist = finalize_session(
                        session.session_id,
                        accepted_preflights,
                        accepted_videos,
                        session.session_dir,
                    )
                    session.playlists = {
                        "preflight": preflight_playlist.name,
                        "final": final_playlist.name,
                    }
                    session.write_session_json()
                    return

                if not is_final:
                    extract_last_frame(video_path, frame_path)
                    current_image = frame_path
                break

        preflight_playlist, final_playlist = finalize_session(
            session.session_id,
            accepted_preflights,
            accepted_videos,
            session.session_dir,
        )
        session.playlists = {
            "preflight": preflight_playlist.name,
            "final": final_playlist.name,
        }
        session.update_status("completed")
        session.write_session_json()


class SessionFileResolver:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def resolve(self, session_id: str, name: str) -> Path:
        safe_name = Path(name).name
        
        # Check session subdirectory first (new structure)
        session_dir = self.output_dir / session_id
        if session_dir.exists():
            candidate = session_dir / safe_name
            if candidate.exists():
                return candidate
        
        # Fallback to root directory (legacy structure)
        candidate = self.output_dir / safe_name
        if candidate.exists():
            return candidate
            
        # If the name itself contains the session_id (legacy playlist entries sometimes did this),
        # we might need to handle that, but safe_name strips directory components.
        
        raise FileNotFoundError(f"File not found: {safe_name}")


class SessionValidator:
    @staticmethod
    def validate_resolution(resolution: str) -> None:
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise ValueError(f"Resolution must be one of {SUPPORTED_RESOLUTIONS}")

    @staticmethod
    def ensure_duration(duration: int) -> None:
        if duration < 1 or duration > 15:
            raise ValueError("Duration must be between 1 and 15 seconds")
