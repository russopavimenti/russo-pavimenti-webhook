"""
Instagram publisher — uses Composio Python SDK to publish on @pavimenti_russo.

Composio SDK API (v0.x):
    from composio import Composio
    client = Composio(api_key=KEY)
    result = client.actions.execute(
        action="INSTAGRAM_POST_IG_USER_MEDIA",
        params={...},
        entity_id="default",
    )
"""
import logging

from config import COMPOSIO_API_KEY, IG_USER_ID, COMPOSIO_ENTITY_ID

log = logging.getLogger("publisher")


def _unwrap_id(response: dict) -> str:
    """Composio responses can nest the result under various keys.
    Try common shapes and pull out an 'id' field."""
    if not isinstance(response, dict):
        return ""
    # Most common: {"data": {"id": "..."}}
    data = response.get("data") or {}
    if isinstance(data, dict):
        if "id" in data:
            return str(data["id"])
        inner = data.get("response") or {}
        if isinstance(inner, dict):
            inner_data = inner.get("data") or {}
            if isinstance(inner_data, dict) and "id" in inner_data:
                return str(inner_data["id"])
    return ""


def _unwrap_permalink(response: dict) -> str:
    if not isinstance(response, dict):
        return ""
    data = response.get("data") or {}
    if isinstance(data, dict):
        if "permalink" in data:
            return str(data["permalink"])
        inner = data.get("response") or {}
        if isinstance(inner, dict):
            inner_data = inner.get("data") or {}
            if isinstance(inner_data, dict) and "permalink" in inner_data:
                return str(inner_data["permalink"])
    return ""


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

    # === Step 1: create media container ===
    try:
        r1 = client.actions.execute(
            action="INSTAGRAM_POST_IG_USER_MEDIA",
            params={
                "ig_user_id": IG_USER_ID,
                "image_url": image_url,
                "caption": caption,
            },
            entity_id=COMPOSIO_ENTITY_ID,
        )
    except Exception as e:
        return {"ok": False, "error": f"create container failed: {e}"}

    creation_id = _unwrap_id(r1)
    if not creation_id:
        return {"ok": False, "error": f"no creation_id in response: {r1!r}"}
    log.info(f"container created: {creation_id}")

    # === Step 2: publish container ===
    try:
        r2 = client.actions.execute(
            action="INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
            params={
                "ig_user_id": IG_USER_ID,
                "creation_id": creation_id,
                "max_wait_seconds": max_wait_seconds,
            },
            entity_id=COMPOSIO_ENTITY_ID,
        )
    except Exception as e:
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
            "error": f"no media_id in response: {r2!r}",
        }
    log.info(f"published: {media_id}")

    # === Step 3: fetch permalink (non-fatal) ===
    permalink = ""
    try:
        r3 = client.actions.execute(
            action="INSTAGRAM_GET_IG_MEDIA",
            params={
                "ig_media_id": media_id,
                "fields": "id,permalink,timestamp",
            },
            entity_id=COMPOSIO_ENTITY_ID,
        )
        permalink = _unwrap_permalink(r3)
    except Exception as e:
        log.warning(f"permalink fetch failed (non-fatal): {e}")

    return {
        "ok": True,
        "creation_id": creation_id,
        "media_id": media_id,
        "permalink": permalink,
    }
