"""
Instagram publisher — uses Composio v3 Python SDK.

Migration note (15/5/2026):
  We previously used composio-core (legacy v1). Its Action enum registry
  is stale and reports INSTAGRAM_POST_IG_USER_MEDIA as deprecated even
  though the v3 backend still serves it. Switched to `composio` (v3 SDK)
  which uses tools.execute(slug=..., arguments=..., user_id=...).
"""
import logging

from config import COMPOSIO_API_KEY, IG_USER_ID, COMPOSIO_ENTITY_ID

log = logging.getLogger("publisher")


def _execute(client, slug: str, arguments: dict) -> dict:
    """Execute a Composio tool via v3 SDK."""
    return client.tools.execute(
        slug=slug,
        arguments=arguments,
        user_id=COMPOSIO_ENTITY_ID,
    )


def _unwrap_id(response) -> str:
    """Pull an 'id' field from common Composio v3 response shapes.
    The SDK may return either a dict or a Pydantic model — handle both."""
    if response is None:
        return ""
    # Pydantic model: convert to dict
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if not isinstance(response, dict):
        return ""
    # Try common shapes
    data = response.get("data")
    if isinstance(data, dict):
        if "id" in data:
            return str(data["id"])
        nested = data.get("response") or data.get("data")
        if isinstance(nested, dict):
            if "id" in nested:
                return str(nested["id"])
            nested2 = nested.get("data")
            if isinstance(nested2, dict) and "id" in nested2:
                return str(nested2["id"])
    # Maybe id is at the top level
    if "id" in response:
        return str(response["id"])
    return ""


def _unwrap_permalink(response) -> str:
    if response is None:
        return ""
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if not isinstance(response, dict):
        return ""

    def _find(obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                found = _find(v, key)
                if found:
                    return found
        return None

    val = _find(response, "permalink")
    return str(val) if val else ""


def publish_image(image_url: str, caption: str, max_wait_seconds: int = 90) -> dict:
    """
    Publish a single-image Instagram post.
    Returns: {ok: bool, media_id, permalink, error}
    """
    if not COMPOSIO_API_KEY:
        return {"ok": False, "error": "COMPOSIO_API_KEY missing"}

    try:
        from composio import Composio
    except ImportError as e:
        return {"ok": False, "error": f"composio SDK not installed: {e}"}

    try:
        client = Composio(api_key=COMPOSIO_API_KEY)
    except Exception as e:
        return {"ok": False, "error": f"Composio client init failed: {e}"}

    if not hasattr(client, "tools"):
        return {
            "ok": False,
            "error": (
                "Composio client has no .tools attribute — old SDK installed? "
                "Need composio>=0.8 (v3), not composio-core."
            ),
        }

    # === Step 1: create media container ===
    try:
        r1 = _execute(client, "INSTAGRAM_POST_IG_USER_MEDIA", {
            "ig_user_id": IG_USER_ID,
            "image_url": image_url,
            "caption": caption,
        })
    except Exception as e:
        log.exception("create container exception")
        return {"ok": False, "error": f"create container failed: {e}"}

    creation_id = _unwrap_id(r1)
    if not creation_id:
        return {
            "ok": False,
            "error": f"no creation_id in response. raw={r1!r:.500}",
        }
    log.info(f"container created: {creation_id}")

    # === Step 2: publish container ===
    try:
        r2 = _execute(client, "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
            "ig_user_id": IG_USER_ID,
            "creation_id": creation_id,
            "max_wait_seconds": max_wait_seconds,
        })
    except Exception as e:
        log.exception("publish exception")
        return {
            "ok": False,
            "creation_id": creation_id,
            "error": f"publish failed: {e}",
        }

    media_id = _unwrap_id(r2)
    if not media_id:
        return {
            "ok": False,
            "creation_id": creation_id,
            "error": f"no media_id in response. raw={r2!r:.500}",
        }
    log.info(f"published: {media_id}")

    # === Step 3: fetch permalink (non-fatal) ===
    permalink = ""
    try:
        r3 = _execute(client, "INSTAGRAM_GET_IG_MEDIA", {
            "ig_media_id": media_id,
            "fields": "id,permalink,timestamp",
        })
        permalink = _unwrap_permalink(r3)
    except Exception as e:
        log.warning(f"permalink fetch failed (non-fatal): {e}")

    return {
        "ok": True,
        "creation_id": creation_id,
        "media_id": media_id,
        "permalink": permalink,
    }
