"""Text-to-image for the manga: whichever image service the environment has a key for.

Cloudflare Workers AI (FLUX schnell, free daily allowance), fal.ai (FLUX schnell) or OpenAI
Images. `available()` names the one in use; `draw(prompt)` returns image bytes or raises.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request

CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def available() -> str | None:
    if os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"):
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


def draw(prompt: str, *, square: bool = False) -> bytes:
    service = available()
    if service == "cloudflare":
        acct, token = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
        data = _post(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{CF_MODEL}",
                     {"prompt": prompt[:2000], "steps": 8}, {"Authorization": f"Bearer {token}"})
        image = (data.get("result") or {}).get("image")
        if not image:
            raise RuntimeError(f"cloudflare drew nothing: {str(data.get('errors'))[:200]}")
        return base64.b64decode(image)
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
