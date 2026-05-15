"""
Instagram publisher — uses Composio Python SDK to publish on @pavimenti_russo.

Bug fix (15/5/2026): the SDK requires Action enum objects, not plain strings.
Plain strings cause: "str object has no attribute 'no_auth'" because the SDK
internally accesses action.no_auth. We use Action[name] (enum lookup) with
fallbacks for resilience across SDK versions.
"""
import logging

from config import COMPOSIO_API_KEY, IG_USER_ID, COMPOSIO_ENTITY_ID

log = logging.getLogger("publisher")


def _resolve_action(name: str):
    """Convert action name string into the type Composio SDK expects.

    Tries (in order):
      1. Action[name]  — enum lookup by member name (composio-core ≥0.5)
      2. Action(name)  — enum constructor with raw value
      3. raw string    — as last resort (older SDKs accepted strings)
    """
    try:
        from composio import Action
    except ImportError:
        return name

    # Try enum member access by name
    try:
        return Action[name]
    except (KeyError, AttributeError):
        pass

    # Try enum constructor (value-based)
    try:
        return Action(name)
    except (ValueError, KeyError):
        pass

    log.warning(f"Action {name!r} not found in enum, passing as raw string")
    return name


def _execute(client, action_name: str, params: dict) -> dict:
    """Execute a Composio tool with version-robust patterns."""
    action = _resolve_action(action_name)

    # Pattern A: entity.execute (newer SDKs)
    try:
        entity = client.get_entity(id=COMPOSIO_ENTITY_ID)
        return entity.execute(action=action, params=params)
    except AttributeError:
        pass  # entity may not have execute() in some versions
    except TypeError:
        pass  # signature differs

    # Pattern B: client.actions.execute
    return client.actions.execute(
        action=action,
        params=params,
        entity_id=COMPOSIO_ENTITY_ID,
    )


def _unwrap_id(response: dict) -> str:
    if not isinstance(response, dict):
        return ""
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
        return {"ok": False, "error": f"no creation_id in response: {r1!r}"}
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
            "error": f"no media_id in response: {r2!r}",
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
