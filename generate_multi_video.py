#!/usr/bin/env python3
"""Generate a chain of videos from sequential prompts using xAI's Grok Imagine Video API."""

import argparse
import base64
import json
import os
import secrets
import subprocess
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from xai_sdk import Client
from xai_sdk.chat import image, system, user

# 16MB limit for gRPC messages (default is 4MB, which fails for large base64 images)
GRPC_MAX_MESSAGE_LENGTH = 16 * 1024 * 1024


def create_client(api_key: str, api_host: str = "api.x.ai") -> Client:
    """Create an xAI SDK Client with increased gRPC message size limits."""
    return Client(
        api_key=api_key,
        api_host=api_host,
        channel_options=[
            ("grpc.max_send_message_length", GRPC_MAX_MESSAGE_LENGTH),
            ("grpc.max_receive_message_length", GRPC_MAX_MESSAGE_LENGTH),
        ],
    )


def generate_session_id() -> str:
    """Generate a unique 6-character session ID."""
    return secrets.token_hex(3)


def load_image_as_data_url(image_path: Path) -> str:
    """Load an image file and return it as a base64 data URL."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    suffix = image_path.suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    mime_type = mime_types.get(suffix, "image/jpeg")

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    base64_string = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{base64_string}"


PROMPT_AUDIO_INSTRUCTION = (
    "IMPORTANT: DO NOT INCLUDE BACKGROUND AUDIO OR GENERATE VOICE OVERS DESCRIBING THE SCENE. \
    Maintain accurate human anatomy and realistic movements throughout—no extra limbs, distortions, \
    morphing, impossible poses, or unnatural behaviors; ensure all elements remain consistent, \
    physically plausible, and grounded in reality. Continue seamlessly from the provided image."
)


def download_video(url: str, output_path: Path) -> None:
    """Download a video from a URL to a local file."""
    print(f"  Downloading video to: {output_path}")
    urllib.request.urlretrieve(url, output_path)


def unique_output_path(path: Path, avoid_paths: set[Path] | None = None) -> Path:
    """Return a non-existing path by appending a counter when needed."""
    avoid_paths = avoid_paths or set()
    resolved_avoid = {p.resolve() for p in avoid_paths}
    if not path.exists() and path.resolve() not in resolved_avoid:
        return path

    counter = 1
    while True:
        candidate = path.with_stem(f"{path.stem}_{counter}")
        if not candidate.exists() and candidate.resolve() not in resolved_avoid:
            return candidate
        counter += 1


def write_hls_playlist(playlist_path: Path, entries: list[str]) -> None:
    """Write a simple HLS-style playlist with the provided entries."""
    lines = ["#EXTM3U"]
    for entry, duration in entries:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(entry)
    playlist_path.write_text("\n".join(lines) + "\n")


def probe_duration(video_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return float(result.stdout.strip())


def finalize_session(
    session_id: str,
    accepted_preflights: list[Path],
    accepted_videos: list[Path],
) -> None:
    """Move session files into a folder and write playlists for accepted outputs."""
    session_dir = Path(session_id)
    session_dir.mkdir(exist_ok=True)

    for path in Path.cwd().glob(f"{session_id}*"):
        if path.is_file():
            shutil.move(str(path), session_dir / path.name)

    moved_preflights = [session_dir / path.name for path in accepted_preflights]
    moved_videos = [session_dir / path.name for path in accepted_videos]

    preflight_entries = [
        (f"{session_id}/{path.name}", probe_duration(path))
        for path in moved_preflights
    ]
    final_entries = [
        (f"{session_id}/{path.name}", probe_duration(path))
        for path in moved_videos
    ]

    preflight_playlist = unique_output_path(Path(f"{session_id}_preflight.m3u8"))
    final_playlist = unique_output_path(Path(f"{session_id}.m3u8"))

    write_hls_playlist(preflight_playlist, preflight_entries)
    write_hls_playlist(final_playlist, final_entries)
    print(f"Playlists written to: {preflight_playlist}, {final_playlist}")


LOG_FILE = Path("generate_multi_video.log")


def log_response(response: object, prompt: str, image_path: str, output_path: Path, session_id: str, part: int, phase: str = "generate", error: str = None) -> None:
    """Append API response to the log file."""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "part": part,
        "phase": phase,
        "prompt": prompt,
        "image_path": str(image_path),
        "output_path": str(output_path),
        "error": error,
        "response": {
            "url": getattr(response, "url", None),
            "duration": getattr(response, "duration", None),
            "request_id": getattr(response, "request_id", None),
        } if response else None,
        "raw": str(response) if response else None,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(log_entry) + "\n")


def extract_last_frame(video_path: Path, output_path: Path) -> None:
    """Extract the last frame of a video using ffmpeg."""
    print(f"  Extracting final frame to: {output_path}")
    cmd = [
        "ffmpeg",
        "-sseof", "-0.1",
        "-i", str(video_path),
        "-frames:v", "1",
        "-pix_fmt", "rgb24",
        "-compression_level", "0",
        "-y",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")


def refine_prompt(client: Client, image_path: Path, prompt: str) -> str:
    """Refine a prompt using a multimodal Grok model and the provided image."""
    image_url = load_image_as_data_url(image_path)
    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            system(
                "You refine video prompts to better match a provided image without major story or thematic changes. "
                "Return only the revised prompt text, no preamble or commentary."
            ),
            user(
                "Adjust the prompt to better match the provided image while preserving intent and style.\n\n"
                f"Original prompt:\n{prompt}",
                image(image_url, detail="low"),
            ),
        ],
    )
    response = chat.sample()
    return response.content.strip()


def preflight_check(
    client: Client,
    image_path: Path,
    prompt: str,
    preflight_output_path: Path,
    session_id: str,
    part: int,
) -> bool:
    """Run a 1-second preflight to test if prompt passes moderation."""
    image_url = load_image_as_data_url(image_path)

    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=1,
        resolution="480p",
    )

    if not response.url:
        log_response(
            response,
            prompt,
            image_path,
            preflight_output_path,
            session_id,
            part,
            phase="preflight",
            error="Video did not respect moderation rules; URL is not available.",
        )
        print(f"  Preflight logged to: {LOG_FILE}")
        return False

    log_response(response, prompt, image_path, preflight_output_path, session_id, part, phase="preflight")
    print(f"  Preflight logged to: {LOG_FILE}")
    download_video(response.url, preflight_output_path)
    return True


def generate_video(client: Client, image_path: Path, prompt: str, output_path: Path, session_id: str, part: int) -> str:
    """Generate a video from an image and prompt using Grok Imagine Video."""
    image_url = load_image_as_data_url(image_path)

    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=15,
        resolution="720p",
    )

    if not response.url:
        log_response(response, prompt, image_path, output_path, session_id, part, phase="generate", error="Video did not respect moderation rules; URL is not available.")
        print(f"  Response logged to: {LOG_FILE}")
        raise RuntimeError("Video did not respect moderation rules; URL is not available.")

    log_response(response, prompt, image_path, output_path, session_id, part, phase="generate")
    print(f"  Response logged to: {LOG_FILE}")

    download_video(response.url, output_path)
    return response.url


def main():
    parser = argparse.ArgumentParser(
        description="Generate a chain of videos from sequential prompts"
    )
    parser.add_argument(
        "--image", "-i",
        required=True,
        help="Path to the starting image file"
    )
    parser.add_argument(
        "--num-prompts", "-n",
        type=int,
        required=True,
        help="Number of prompt files to process (expects 1.txt, 2.txt, ... n.txt)"
    )
    parser.add_argument(
        "--resume-session",
        help="Resume a previous session by providing its session ID/prefix"
    )
    parser.add_argument(
        "--start-part",
        type=int,
        default=1,
        help="Prompt number to start from when resuming (default: 1)"
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the 1-second preflight moderation check before each generation"
    )
    parser.add_argument(
        "--api-host",
        default="api.x.ai",
        help="API host to use (default: api.x.ai)"
    )
    parser.add_argument(
        "--refine-prompts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Refine prompts with Grok before generation (default: true)"
    )

    args = parser.parse_args()

    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        print("Error: XAI_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    starting_image = Path(args.image)
    if not starting_image.exists():
        print(f"Error: Starting image not found: {args.image}", file=sys.stderr)
        sys.exit(1)

    if args.start_part < 1 or args.start_part > args.num_prompts:
        print("Error: --start-part must be between 1 and --num-prompts", file=sys.stderr)
        sys.exit(1)

    for i in range(args.start_part, args.num_prompts + 1):
        prompt_file = Path(f"{i}.txt")
        archived_prompt = Path(f"{args.resume_session}_{i}.txt") if args.resume_session else None
        if not prompt_file.exists() and (not archived_prompt or not archived_prompt.exists()):
            print(f"Error: Prompt file not found: {prompt_file}", file=sys.stderr)
            sys.exit(1)

    session_id = args.resume_session or generate_session_id()
    print(f"Session ID: {session_id}")
    print(f"Starting image: {starting_image}")
    print(f"Number of prompts: {args.num_prompts}")
    print()

    client = create_client(api_key=api_key, api_host=args.api_host)
    accepted_preflights: list[Path] = []
    accepted_videos: list[Path] = []
    if args.start_part > 1:
        previous_frame = Path(f"{session_id}_part{args.start_part - 1}_finalframe.png")
        if not previous_frame.exists():
            print(f"Error: Previous frame not found for resume: {previous_frame}", file=sys.stderr)
            sys.exit(1)
        current_image = previous_frame
    else:
        current_image = starting_image

    for i in range(args.start_part, args.num_prompts + 1):
        prompt_file = Path(f"{i}.txt")
        archived_prompt = Path(f"{session_id}_{i}.txt")
        if prompt_file.exists():
            raw_prompt = prompt_file.read_text().strip()
        else:
            raw_prompt = archived_prompt.read_text().strip()

        prompt = raw_prompt
        if args.refine_prompts and i > 1 and raw_prompt:
            print("  Refining prompt with Grok...")
            try:
                refined_prompt = refine_prompt(client, current_image, raw_prompt)
                print("\n--- Refined prompt ---\n")
                print(refined_prompt)
                print("\n----------------------\n")
                use_refined = input("Use updated prompt? (y/n): ").strip().lower()
                if use_refined in {"y", "yes"}:
                    prompt = refined_prompt
            except Exception as e:
                print(f"  Prompt refinement failed, using original. ({e})")

        if prompt:
            prompt = f"{prompt}\n\n{PROMPT_AUDIO_INSTRUCTION}"

        base_image = current_image
        print(f"[Part {i}/{args.num_prompts}]")
        print(f"  Prompt file: {prompt_file}")
        print(f"  Input image: {current_image}")

        while True:
            preflight_video_path = None
            video_path = unique_output_path(Path(f"{session_id}_part{i}.mp4"))
            frame_path = unique_output_path(Path(f"{session_id}_part{i}_finalframe.png"))

            if not args.skip_preflight:
                while True:
                    print("  Running preflight check (1s)...")
                    try:
                        preflight_video_path = unique_output_path(Path(f"{session_id}_{i}_preflight.mp4"))
                        if not preflight_check(
                            client,
                            base_image,
                            prompt,
                            preflight_video_path,
                            session_id,
                            i,
                        ):
                            raise RuntimeError("Video did not respect moderation rules; URL is not available.")
                        print("  Preflight passed!")
                        break
                    except Exception as e:
                        print(f"\nError during preflight: {e}", file=sys.stderr)
                        retry = input("Retry preflight? (y/n): ").strip().lower()
                        if retry not in {"y", "yes"}:
                            print(f"Failed at part {i}. Stopping.", file=sys.stderr)
                            sys.exit(1)

            while True:
                print("  Generating full video (15s)...")
                try:
                    video_url = generate_video(client, base_image, prompt, video_path, session_id, i)
                    print(f"  Video URL: {video_url}")
                    break
                except Exception as e:
                    print(f"\nError generating video: {e}", file=sys.stderr)
                    retry = input("Retry generation? (y/n): ").strip().lower()
                    if retry not in {"y", "yes"}:
                        print(f"Failed at part {i}. Stopping.", file=sys.stderr)
                        sys.exit(1)

            while True:
                is_final_part = i == args.num_prompts
                if is_final_part:
                    prompt_text = "  End or regenerate? (y/n): "
                else:
                    prompt_text = "  Continue or regenerate? (y/n/r): "
                proceed = input(prompt_text).strip().lower()
                if proceed in {"y", "yes"}:
                    if preflight_video_path:
                        accepted_preflights.append(preflight_video_path)
                    accepted_videos.append(video_path)
                    if prompt_file.exists():
                        archived_prompt = unique_output_path(Path(f"{session_id}_{i}.txt"))
                        prompt_file.rename(archived_prompt)
                        print(f"  Prompt archived to: {archived_prompt}")
                    if i < args.num_prompts:
                        try:
                            extract_last_frame(video_path, frame_path)
                            current_image = frame_path
                        except RuntimeError as e:
                            print(f"\nError extracting frame: {e}", file=sys.stderr)
                            print(f"Failed at part {i}. Stopping.", file=sys.stderr)
                            sys.exit(1)
                    print()
                    break
                if proceed in {"n", "no"}:
                    if preflight_video_path:
                        accepted_preflights.append(preflight_video_path)
                    accepted_videos.append(video_path)
                    if prompt_file.exists():
                        archived_prompt = unique_output_path(Path(f"{session_id}_{i}.txt"))
                        prompt_file.rename(archived_prompt)
                        print(f"  Prompt archived to: {archived_prompt}")
                    finalize_session(session_id, accepted_preflights, accepted_videos)
                    print("Stopping after current scene.")
                    sys.exit(0)
                if proceed in {"r", "regen", "regenerate"}:
                    if is_final_part:
                        print("  Please enter 'y' or 'n'.")
                        continue
                    aborted_paths = [p for p in [preflight_video_path, video_path, frame_path] if p]
                    for path in aborted_paths:
                        if path.exists():
                            aborted_path = unique_output_path(path.with_stem(f"{path.stem}_aborted"))
                            path.rename(aborted_path)
                    break
                if is_final_part:
                    print("  Please enter 'y' or 'n'.")
                else:
                    print("  Please enter 'y', 'n', or 'r'.")

            if proceed in {"y", "yes"}:
                break

    finalize_session(session_id, accepted_preflights, accepted_videos)
    print(f"All {args.num_prompts} videos generated successfully!")
    print(f"Session ID: {session_id}")


if __name__ == "__main__":
    main()
