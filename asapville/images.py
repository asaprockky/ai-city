"""Text-to-image for the manga: whichever image service the environment has a key for.

Cloudflare Workers AI, fal.ai (FLUX schnell) or OpenAI Images. `available()` names the one in
use; `draw(prompt)` returns image bytes or raises. On Cloudflare the model is
`ASAPVILLE_CF_IMAGE_MODEL` (default `CF_DEFAULT_MODEL`); FLUX.2 models also take up to four
reference pictures (`refs`), which the manga uses to keep the citizens' faces.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.request
import uuid

CF_DEFAULT_MODEL = "@cf/black-forest-labs/flux-2-dev"
CF_MAX_REFS = 4  # FLUX.2 on Workers AI: input_image_0..3, each under 512x512
NEGATIVE = "text, letters, words, captions, speech bubbles, watermark, signature, colour"


def cf_model() -> str:
    return os.environ.get("ASAPVILLE_CF_IMAGE_MODEL") or CF_DEFAULT_MODEL


def takes_refs() -> bool:
    """Whether the image service in use draws from reference pictures."""
    return available() == "cloudflare" and "flux-2" in cf_model()


def _cf_account(token: str) -> str | None:
    """The Cloudflare account id: from the environment (either spelling), else asked of the API."""
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CLOUDFARE_ACCOUNT_ID")
    if acct:
        return acct
    req = urllib.request.Request("https://api.cloudflare.com/client/v4/accounts",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read()).get("result") or []
    except Exception:  # no account visible to this token: the service is just not available
        return None
    return result[0]["id"] if result else None


def available() -> str | None:
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        return "cloudflare"
    if os.environ.get("FAL_KEY"):
        return "fal"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _post(url: str, body: dict, headers: dict[str, str], timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _ref_png(image: bytes) -> bytes:
    """A reference picture as Workers AI wants it: PNG, under 512x512."""
    from PIL import Image

    im = Image.open(io.BytesIO(image)).convert("RGB")
    im.thumbnail((511, 511))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _multipart(fields: dict[str, str | bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if isinstance(value, bytes):
            head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{name}.png"\r\nContent-Type: image/png\r\n\r\n')
            parts.append(head.encode() + value + b"\r\n")
        else:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                         f"{value}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def cf_request(model: str, prompt: str, size: tuple[int, int], refs: list[bytes],
               seed: int | None = None) -> tuple[bytes, str]:
    """Body and content type for one Workers AI text-to-image call; each model family has its own."""
    w, h = size
    if "flux-2" in model:  # multipart form, even without pictures
        fields: dict[str, str | bytes] = {"prompt": prompt, "width": str(w), "height": str(h)}
        if model.endswith("flux-2-dev"):
            fields["steps"] = "28"  # klein runs a fixed 4 steps
        if seed is not None:
            fields["seed"] = str(seed)
        for i, ref in enumerate(refs[:CF_MAX_REFS]):
            fields[f"input_image_{i}"] = _ref_png(ref)
        return _multipart(fields)
    if "flux-1-schnell" in model:  # takes nothing but the prompt and steps
        body: dict = {"prompt": prompt[:2048], "steps": 8}
    elif "leonardo" in model:
        body = {"prompt": prompt, "width": w, "height": h, "num_steps": 30}
        if "phoenix" in model:
            body["negative_prompt"] = NEGATIVE
    else:  # the Stable Diffusion family
        body = {"prompt": prompt, "width": w, "height": h, "negative_prompt": NEGATIVE}
    if seed is not None and "flux-1" not in model:
        body["seed"] = seed
    return json.dumps(body).encode(), "application/json"


def _cf_draw(prompt: str, size: tuple[int, int], refs: list[bytes]) -> bytes:
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    acct = _cf_account(token)
    if not acct:
        raise RuntimeError("cloudflare: no account id (set CLOUDFLARE_ACCOUNT_ID)")
    model = cf_model()
    data, ctype = cf_request(model, prompt, size, refs)
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"
    req = urllib.request.Request(url, data=data,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw, rtype = resp.read(), resp.headers.get("Content-Type", "")
    if "json" not in rtype:  # Phoenix and Stable Diffusion answer with the picture itself
        return raw
    reply = json.loads(raw)
    image = (reply.get("result") or {}).get("image")
    if not image:
        raise RuntimeError(f"cloudflare {model} drew nothing: {str(reply.get('errors'))[:200]}")
    return base64.b64decode(image)


def draw(prompt: str, *, size: tuple[int, int] = (1024, 768), refs: list[bytes] | None = None) -> bytes:
    """Draw `prompt` at about `size`; `refs` (portraits) are used where the model takes them."""
    service = available()
    if service == "cloudflare":
        return _cf_draw(prompt, size, list(refs or []) if takes_refs() else [])
    square = size[0] == size[1]
    if service == "fal":
        data = _post("https://fal.run/fal-ai/flux/schnell",
                     {"prompt": prompt, "image_size": "square_hd" if square else "landscape_4_3",
                      "num_images": 1}, {"Authorization": f"Key {os.environ['FAL_KEY']}"})
        url = (data.get("images") or [{}])[0].get("url")
        if not url:
            raise RuntimeError(f"fal drew nothing: {str(data)[:200]}")
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()
    if service == "openai":
        data = _post("https://api.openai.com/v1/images/generations",
                     {"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
                      "size": "1024x1024" if square else "1536x1024", "n": 1},
                     {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        b64 = (data.get("data") or [{}])[0].get("b64_json")
        if not b64:
            raise RuntimeError(f"openai drew nothing: {str(data)[:200]}")
        return base64.b64decode(b64)
    raise RuntimeError("no image service key in the environment")
