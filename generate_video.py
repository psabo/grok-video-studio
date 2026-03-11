#!/usr/bin/env python3
"""Generate a video from an image using xAI's Grok Imagine Video API."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

from xai_sdk import Client

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


def load_image_as_data_url(image_path: str) -> str:
    """Load an image file and return it as a base64 data URL."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    suffix = path.suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    mime_type = mime_types.get(suffix, "image/jpeg")

    with open(path, "rb") as f:
        image_bytes = f.read()

    base64_string = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{base64_string}"


PROMPT_AUDIO_INSTRUCTION = (
    "IMPORTANT: DO NOT INCLUDE BACKGROUND AUDIO OR GENERATE VOICE OVERS DESCRIBING THE SCEEN"
)


def download_video(url: str, output_path: Path) -> None:
    """Download a video from a URL to a local file."""
    print(f"Downloading video to: {output_path}")
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


def extract_last_frame(video_path: Path, output_path: Path) -> None:
    """Extract the last frame of a video using ffmpeg."""
    print(f"Extracting last frame from: {video_path}")
    cmd = [
        "ffmpeg",
        "-sseof", "-0.1",
        "-i", str(video_path),
        "-update", "1",
        "-q:v", "2",
        "-y",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")


LOG_FILE = Path("generate_video.log")


def log_response(response: object, prompt: str, image_path: str, output_path: Path, error: str = None) -> None:
    """Append API response to the log file."""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt,
        "image_path": image_path,
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


def preflight_check(
    client: Client,
    image_path: str,
    image_url: str,
    prompt: str,
    preflight_output_path: Path,
) -> None:
    """Run a 1-second preflight to test if prompt passes moderation."""
    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=1,
        resolution="480p",
    )

    if not response.url:
        log_response(response, prompt, image_path, preflight_output_path, error="Video did not respect moderation rules; URL is not available.")
        print(f"Response logged to: {LOG_FILE}")
        raise ValueError("Video did not respect moderation rules; URL is not available.")

    log_response(response, prompt, image_path, preflight_output_path)
    print(f"Response logged to: {LOG_FILE}")
    download_video(response.url, preflight_output_path)


def generate_video(
    image_path: str,
    prompt: str,
    output_path: Path,
    preflight_output_path: Path,
    duration: int,
    api_host: str,
) -> str:
    """Generate a video from an image and prompt using Grok Imagine Video."""
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        raise ValueError("XAI_API_KEY environment variable is required")

    client = create_client(api_key=api_key, api_host=api_host)
    image_url = load_image_as_data_url(image_path)

    print(f"Generating video from: {image_path}")
    print(f"Prompt: {prompt}")
    print("This may take a few minutes...")

    preflight_check(client, image_path, image_url, prompt, preflight_output_path)

    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=duration,
        resolution="720p",
    )

    if not response.url:
        log_response(response, prompt, image_path, output_path, error="Video did not respect moderation rules; URL is not available.")
        print(f"Response logged to: {LOG_FILE}")
        raise ValueError("Video did not respect moderation rules; URL is not available.")

    log_response(response, prompt, image_path, output_path)
    print(f"Response logged to: {LOG_FILE}")

    download_video(response.url, output_path)
    return response.url


def main():
    parser = argparse.ArgumentParser(
        description="Generate a video from an image using Grok Imagine Video"
    )
    parser.add_argument(
        "--image", "-i",
        default=None,
        help="Path to the input image file"
    )
    parser.add_argument(
        "--seed-video", "-s",
        default=None,
        help="Path to a video file to extract the last frame from as input"
    )
    parser.add_argument(
        "--prompt", "-p",
        default=None,
        help="Text prompt describing the desired video"
    )
    parser.add_argument(
        "--prompt-file", "-f",
        default=None,
        help="Path to a text file containing the prompt"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output path for the video file (default: <image_name>_video.mp4)"
    )
    parser.add_argument(
        "--api-host",
        default="api.x.ai",
        help="API host to use (default: api.x.ai)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=15,
        help="Video duration in seconds (max: 15, default: 15)"
    )

    args = parser.parse_args()

    if not args.image and not args.seed_video:
        print("Error: Either --image or --seed-video is required", file=sys.stderr)
        sys.exit(1)

    if args.image and args.seed_video:
        print("Error: Cannot specify both --image and --seed-video", file=sys.stderr)
        sys.exit(1)

    if args.seed_video:
        seed_video_path = Path(args.seed_video)
        if not seed_video_path.exists():
            print(f"Error: Seed video not found: {args.seed_video}", file=sys.stderr)
            sys.exit(1)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            temp_frame_path = Path(tmp.name)
        try:
            extract_last_frame(seed_video_path, temp_frame_path)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        image_path = str(temp_frame_path)
        image_stem = seed_video_path.stem
    else:
        image_path = args.image
        image_stem = Path(args.image).stem

    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.exists():
            print(f"Error: Prompt file not found: {args.prompt_file}", file=sys.stderr)
            sys.exit(1)
        prompt = prompt_path.read_text().strip()
    elif args.prompt:
        prompt = args.prompt
    else:
        print("Error: Either --prompt or --prompt-file is required", file=sys.stderr)
        sys.exit(1)

    if not prompt:
        print("Error: Prompt cannot be empty", file=sys.stderr)
        sys.exit(1)

    prompt = f"{prompt}\n\n{PROMPT_AUDIO_INSTRUCTION}"

    if args.duration < 1 or args.duration > 15:
        print("Error: --duration must be between 1 and 15 seconds", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(f"{image_stem}_video.mp4")

    seed_video_resolved = Path(args.seed_video).resolve() if args.seed_video else None

    if output_path.exists() or (seed_video_resolved and output_path.resolve() == seed_video_resolved):
        counter = 1
        while True:
            new_path = output_path.with_stem(f"{output_path.stem}_{counter}")
            if not new_path.exists() and (not seed_video_resolved or new_path.resolve() != seed_video_resolved):
                output_path = new_path
                break
            counter += 1

    base_output_path = output_path
    while True:
        video_output_path = unique_output_path(base_output_path)
        preflight_output_path = unique_output_path(
            video_output_path.with_stem(f"{video_output_path.stem}_preflight")
        )
        try:
            video_url = generate_video(
                image_path,
                prompt,
                video_output_path,
                preflight_output_path,
                args.duration,
                args.api_host,
            )
            print(f"\nVideo generated and downloaded successfully!")
            print(f"Saved to: {video_output_path}")
            print(f"URL: {video_url}")
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            retry = input("Retry generation? (y/n): ").strip().lower()
            if retry in {"y", "yes"}:
                continue
            sys.exit(1)
        except Exception as e:
            print(f"Error generating video: {e}", file=sys.stderr)
            retry = input("Retry generation? (y/n): ").strip().lower()
            if retry in {"y", "yes"}:
                continue
            sys.exit(1)

        regenerate = input("Regenerate? (y/n): ").strip().lower()
        if regenerate in {"y", "yes"}:
            aborted_paths = [preflight_output_path, video_output_path]
            for path in aborted_paths:
                if path.exists():
                    aborted_path = unique_output_path(path.with_stem(f"{path.stem}_aborted"))
                    path.rename(aborted_path)
            continue
        return


if __name__ == "__main__":
    main()
