from __future__ import annotations

import base64
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Iterable

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


DEFAULT_SYSTEM_INSTRUCTIONS = (
    "IMPORTANT: DO NOT INCLUDE BACKGROUND AUDIO OR GENERATE VOICE OVERS DESCRIBING THE SCENE. "
    "Maintain accurate human anatomy and realistic movements throughout - no extra limbs, distortions, "
    "morphing, impossible poses, or unnatural behaviors; ensure all elements remain consistent, "
    "physically plausible, and grounded in reality. Continue seamlessly from the provided image."
)


SUPPORTED_RESOLUTIONS = ["480p", "720p"]


class ModerationError(RuntimeError):
    """Raised when a request fails moderation checks.
    
    Attributes:
        pre_generation: True if caught before video was generated ($0.05 fee only),
                       False if caught after generation (video cost + $0.05 fee).
    """
    def __init__(self, message: str, pre_generation: bool = True):
        super().__init__(message)
        self.pre_generation = pre_generation


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

    image_bytes = image_path.read_bytes()
    base64_string = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{base64_string}"


def download_video(url: str, output_path: Path) -> None:
    """Download a video from a URL to a local file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
    """Extract the last frame of a video using ffmpeg (lossless PNG)."""
    cmd = [
        "ffmpeg",
        "-sseof",
        "-0.1",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-pix_fmt",
        "rgb24",
        "-compression_level",
        "0",
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


def analyze_image(client: Client, image_path: Path) -> str:
    """Analyze an image to generate grounding text describing characters and setting."""
    image_url = load_image_as_data_url(image_path)
    chat = client.chat.create(
        model="grok-4-1-fast-reasoning",
        messages=[
            system(
                "You are an expert visual analyzer for video generation. "
                "Describe the image in detail, focusing on:\n"
                "1. Characters (appearance, clothing, distinctive features)\n"
                "2. Setting/Environment (lighting, style, background elements)\n"
                "3. Artistic style and mood\n"
                "Provide a concise but comprehensive paragraph that can serve as grounding context."
            ),
            user(
                "Describe this image for consistent video generation.",
                image(image_url, detail="high"),
            ),
        ],
    )
    response = chat.sample()
    return response.content.strip()


def preflight_check(
    client: Client,
    image_path: Path,
    prompt: str,
    output_path: Path,
) -> str:
    """Run a 1-second preflight to test if prompt passes moderation."""
    image_url = load_image_as_data_url(image_path)
    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=1,
        resolution="480p",
    )
    url = response._video.url
    if not url:
        if not response.respect_moderation:
            # Check if video was generated (post-gen failure) or caught early (pre-gen)
            has_duration = getattr(response, 'duration', None) and response.duration > 0
            raise ModerationError(
                "Video did not respect moderation rules; URL is not available.",
                pre_generation=not has_duration,
            )
        raise RuntimeError("Video URL missing from response.")

    download_video(url, output_path)
    return url


def generate_video(
    client: Client,
    image_path: Path,
    prompt: str,
    output_path: Path,
    duration: int,
    resolution: str,
) -> str:
    """Generate a video from an image and prompt using Grok Imagine Video."""
    image_url = load_image_as_data_url(image_path)
    response = client.video.generate(
        prompt=prompt,
        model="grok-imagine-video",
        image_url=image_url,
        duration=duration,
        resolution=resolution,
    )
    url = response._video.url
    if not url:
        if not response.respect_moderation:
            # Check if video was generated (post-gen failure) or caught early (pre-gen)
            has_duration = getattr(response, 'duration', None) and response.duration > 0
            raise ModerationError(
                "Video did not respect moderation rules; URL is not available.",
                pre_generation=not has_duration,
            )
        raise RuntimeError("Video URL missing from response.")

    download_video(url, output_path)
    return url


def write_hls_playlist(playlist_path: Path, entries: Iterable[tuple[str, float]]) -> None:
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
    session_dir: Path,
) -> tuple[Path, Path]:
    """Write playlists for accepted outputs inside the session directory."""
    # Ensure all paths are relative to session_dir or absolute.
    # accepted_preflights/videos are currently paths like 'sessions/SID/file.mp4'.
    # We want entries in the playlist to be just the filename if the playlist is in the same dir.

    preflight_entries = [
        (path.name, probe_duration(path))
        for path in accepted_preflights
        if path.exists()
    ]
    final_entries = [
        (path.name, probe_duration(path))
        for path in accepted_videos
        if path.exists()
    ]

    preflight_playlist = unique_output_path(session_dir / f"{session_id}_preflight.m3u8")
    final_playlist = unique_output_path(session_dir / f"{session_id}.m3u8")

    write_hls_playlist(preflight_playlist, preflight_entries)
    write_hls_playlist(final_playlist, final_entries)
    return preflight_playlist, final_playlist
