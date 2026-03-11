from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .core import analyze_image, create_client, extract_last_frame, unique_output_path
from .pricing import PricingStore
from .session import (
    MultiRequest,
    SessionFileResolver,
    SessionManager,
    SessionValidator,
    SingleRequest,
    parse_bool,
)

OUTPUT_DIR = Path("sessions")
PRICING_PATH = Path("pricing.json")

app = FastAPI(title="Grok Video Generator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

pricing_store = PricingStore(PRICING_PATH)
pricing_store.refresh()
manager = SessionManager(OUTPUT_DIR, pricing_store)
resolver = SessionFileResolver(OUTPUT_DIR)


def _save_upload(upload: UploadFile, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


def _parse_prompts(raw_prompts: str) -> list[str]:
    if not raw_prompts.strip():
        raise ValueError("Prompts cannot be empty")
    try:
        parsed = json.loads(raw_prompts)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    lines = [line.strip() for line in raw_prompts.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Prompts cannot be empty")
    return lines


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    return manager.list_sessions()


@app.get("/api/pricing")
def get_pricing() -> dict[str, Any]:
    return pricing_store.to_dict()


@app.post("/api/pricing/refresh")
def refresh_pricing() -> dict[str, Any]:
    pricing_store.refresh()
    return pricing_store.to_dict()


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    try:
        session = manager.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/api/analyze-image")
def analyze_image_endpoint(
    image: UploadFile = File(...),
    api_key: str = Form(...),
    api_host: str = Form("api.x.ai"),
) -> dict[str, str]:
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    temp_path = OUTPUT_DIR / f"temp_analyze_{image.filename}"
    _save_upload(image, temp_path)
    try:
        client = create_client(api_key=api_key, api_host=api_host)
        description = analyze_image(client, temp_path)
        return {"description": description}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.post("/api/single")
def create_single(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    duration: int = Form(15),
    resolution: str = Form("720p"),
    preflight: str = Form("true"),
    api_host: str = Form("api.x.ai"),
    api_key: str = Form(...),
    system_instructions: str = Form(""),
    grounding_text: str = Form(""),
    refine_prompts: str = Form("false"),
    refine_auto_accept: str = Form("false"),
    title: str = Form(None),
    budget_cap: str = Form(None),
    session_id: str = Form(None),
) -> dict[str, str]:
    print(f"DEBUG: create_single received api_key length={len(api_key)}, prefix={api_key[:4] if api_key else 'None'}, suffix={api_key[-4:] if api_key else 'None'}")
    print(f"DEBUG: api_host={api_host}, session_id={session_id}")

    SessionValidator.validate_resolution(resolution)
    SessionValidator.ensure_duration(duration)

    if session_id:
        try:
            session = manager.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found") from exc
    else:
        session = manager.create_session("single", total_parts=1, title=title)
        if budget_cap:
            try:
                session.budget_cap = float(budget_cap)
            except ValueError:
                pass

    suffix = Path(image.filename or "").suffix or ".png"
    if session_id:
        base_name = f"{session.session_id}_input"
        image_path = unique_output_path(session.session_dir / f"{base_name}{suffix}")
    else:
        image_path = session.session_dir / f"{session.session_id}_input{suffix}"
    _save_upload(image, image_path)

    request = SingleRequest(
        image_path=image_path,
        prompt=prompt.strip(),
        duration=duration,
        resolution=resolution,
        api_key=api_key.strip(),
        api_host=api_host.strip() or "api.x.ai",
        preflight=parse_bool(preflight, True),
        system_instructions=system_instructions.strip(),
        grounding_text=grounding_text.strip(),
        refine_prompts=parse_bool(refine_prompts, False),
        refine_auto_accept=parse_bool(refine_auto_accept, False),
    )
    if not request.prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if not request.api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    try:
        manager.start_single(session, request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_id": session.session_id}


@app.post("/api/multi")
def create_multi(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    duration: int = Form(15),
    resolution: str = Form("720p"),
    preflight: str = Form("true"),
    api_host: str = Form("api.x.ai"),
    api_key: str = Form(...),
    system_instructions: str = Form(""),
    refine_prompts: str = Form("true"),
    refine_auto_accept: str = Form("false"),
    grounding_text: str = Form(""),
    title: str = Form(None),
    session_id: str = Form(None),
    budget_cap: str = Form(None),
) -> dict[str, str]:
    print(f"DEBUG: create_multi received api_key length={len(api_key)}, prefix={api_key[:4] if api_key else 'None'}, suffix={api_key[-4:] if api_key else 'None'}")
    print(f"DEBUG: api_host={api_host}, session_id={session_id}")
    
    SessionValidator.validate_resolution(resolution)
    SessionValidator.ensure_duration(duration)

    prompt_text = prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Prompt is required")

    prompt_list = [prompt_text]

    if session_id:
        try:
            session = manager.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found") from exc
    else:
        session = manager.create_session("multi", total_parts=len(prompt_list), title=title)
        if budget_cap:
            try:
                session.budget_cap = float(budget_cap)
            except ValueError:
                pass  # Ignore invalid budget caps

    suffix = Path(image.filename or "").suffix or ".png"
    if session_id:
        # Ensure unique input filename when appending
        base_name = f"{session.session_id}_input"
        image_path = unique_output_path(session.session_dir / f"{base_name}{suffix}")
    else:
        image_path = session.session_dir / f"{session.session_id}_input{suffix}"
        
    _save_upload(image, image_path)

    request = MultiRequest(
        image_path=image_path,
        prompts=prompt_list,
        duration=duration,
        resolution=resolution,
        api_key=api_key.strip(),
        api_host=api_host.strip() or "api.x.ai",
        preflight=parse_bool(preflight, True),
        system_instructions=system_instructions.strip(),
        refine_prompts=parse_bool(refine_prompts, True),
        refine_auto_accept=parse_bool(refine_auto_accept, False),
        grounding_text=grounding_text.strip(),
    )
    if not request.api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    if session_id:
        manager.append_multi(session, request)
    else:
        manager.start_multi(session, request)
        
    return {"session_id": session.session_id}


@app.post("/api/sessions/{session_id}/title")
def update_session_title(session_id: str, title: str = Form(...)) -> dict[str, str]:
    try:
        session = manager.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    
    session.set_title(title.strip())
    session.write_session_json()
    return {"session_id": session.session_id, "title": session.title}


@app.post("/api/sessions/{session_id}/open-folder")
def open_session_folder(session_id: str) -> dict[str, str]:
    try:
        manager.open_folder(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok"}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, str]:
    try:
        manager.delete_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/api/sessions/{session_id}/archive")
def download_session_archive(session_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    try:
        archive_path = manager.create_archive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    
    # Schedule cleanup of the archive file after response is sent
    background_tasks.add_task(lambda p: p.unlink(missing_ok=True), archive_path)
    
    return FileResponse(
        archive_path, 
        filename=f"{session_id}_archive.zip", 
        media_type="application/zip"
    )


@app.post("/api/sessions/{session_id}/action")
def submit_action(session_id: str, action: str = Form(...)) -> dict[str, str]:
    try:
        session = manager.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        session.submit_action(action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/extend-frame")
def extend_from_last_frame(session_id: str) -> dict[str, str]:
    try:
        session = manager.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    videos = [output for output in session.outputs if output.get("type") == "video"]
    if not videos:
        raise HTTPException(status_code=400, detail="No video outputs available to extend.")

    last_video = videos[-1]
    try:
        source_path = resolver.resolve(session_id, last_video["path"])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    frame_path = unique_output_path(OUTPUT_DIR / f"{session_id}_extend_frame.png")
    try:
        extract_last_frame(source_path, frame_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"frame": frame_path.name}


@app.post("/api/sessions/{session_id}/fork")
def fork_session(
    session_id: str,
    part_index: int = Form(...),
    title: str = Form(None),
) -> dict[str, str]:
    try:
        new_session, frame_filename = manager.fork_session(session_id, part_index, title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    
    return {"session_id": new_session.session_id, "frame": frame_filename}


@app.get("/api/sessions/{session_id}/files/{name}")
def get_file(session_id: str, name: str) -> FileResponse:
    try:
        path = resolver.resolve(session_id, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )
