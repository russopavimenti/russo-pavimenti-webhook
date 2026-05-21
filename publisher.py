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
    """Execute a Composio tool via v3 SDK.

    Tries several signatures because the v3 SDK is rapidly evolving:
      a) tools.execute(slug, arguments, user_id, dangerously_skip_version_check)
      b) tools.execute(slug, arguments, user_id)  — relies on client-level skip
    """
    last_err = None
    for kwargs in (
        {"slug": slug, "arguments": arguments, "user_id": COMPOSIO_ENTITY_ID,
         "dangerously_skip_version_check": True},
        {"slug": slug, "arguments": arguments, "user_id": COMPOSIO_ENTITY_ID,
         "skip_version_check": True},
        {"slug": slug, "arguments": arguments, "user_id": COMPOSIO_ENTITY_ID},
    ):
        try:
            return client.tools.execute(**kwargs)
        except TypeError as e:
            # signature mismatch — try next variant
            last_err = e
            continue
    raise RuntimeError(f"all execute signatures failed; last: {last_err}")


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

    # Try multiple init signatures — composio v3 has evolving API
    client = None
    init_errors = []
    for init_kwargs in (
        {"api_key": COMPOSIO_API_KEY, "dangerously_skip_version_check": True},
        {"api_key": COMPOSIO_API_KEY, "skip_version_check": True},
        {"api_key": COMPOSIO_API_KEY},
    ):
        try:
            client = Composio(**init_kwargs)
            break
        except TypeError as e:
            init_errors.append(f"{init_kwargs.keys()}: {e}")
        except Exception as e:
            init_errors.append(f"{init_kwargs.keys()}: {e}")

    if client is None:
        return {
            "ok": False,
            "error": f"Composio client init failed: {init_errors}",
        }

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


def _make_client():
    """Init a Composio v3 client. Returns (client, error_str). client is None on error."""
    if not COMPOSIO_API_KEY:
        return None, "COMPOSIO_API_KEY missing"
    try:
        from composio import Composio
    except ImportError as e:
        return None, f"composio SDK not installed: {e}"

    client = None
    errs = []
    for kw in (
        {"api_key": COMPOSIO_API_KEY, "dangerously_skip_version_check": True},
        {"api_key": COMPOSIO_API_KEY, "skip_version_check": True},
        {"api_key": COMPOSIO_API_KEY},
    ):
        try:
            client = Composio(**kw)
            break
        except Exception as e:
            errs.append(str(e))
    if client is None:
        return None, f"Composio client init failed: {errs}"
    if not hasattr(client, "tools"):
        return None, ("Composio client has no .tools attribute — old SDK? "
                      "Need composio>=0.8 (v3).")
    return client, ""


def publish_carousel(image_urls, caption: str, max_wait_seconds: int = 120) -> dict:
    """
    Publish a multi-image Instagram carousel (2-10 images).

    Instagram 3-step carousel flow:
      1. Create a child media container per image (is_carousel_item=true).
      2. Create a parent container (media_type=CAROUSEL, children=[ids], caption).
      3. Publish the parent container.

    Returns: {ok: bool, media_id, permalink, error}
    """
    image_urls = list(image_urls or [])
    if not (2 <= len(image_urls) <= 10):
        return {"ok": False,
                "error": f"carousel needs 2-10 images, got {len(image_urls)}"}

    client, err = _make_client()
    if client is None:
        return {"ok": False, "error": err}

    # === Step 1: one child container per image ===
    child_ids = []
    for i, url in enumerate(image_urls):
        try:
            r = _execute(client, "INSTAGRAM_POST_IG_USER_MEDIA", {
                "ig_user_id": IG_USER_ID,
                "image_url": url,
                "is_carousel_item": True,
            })
        except Exception as e:
            log.exception(f"carousel child {i} exception")
            return {"ok": False, "error": f"child container {i} failed: {e}"}
        cid = _unwrap_id(r)
        if not cid:
            return {"ok": False,
                    "error": f"child {i}: no container id. raw={r!r:.300}"}
        child_ids.append(cid)
        log.info(f"carousel child {i + 1}/{len(image_urls)} container: {cid}")

    # === Step 2: parent carousel container ===
    try:
        rp = _execute(client, "INSTAGRAM_POST_IG_USER_MEDIA", {
            "ig_user_id": IG_USER_ID,
            "media_type": "CAROUSEL",
            "children": child_ids,
            "caption": caption,
        })
    except Exception as e:
        log.exception("carousel parent container exception")
        return {"ok": False, "error": f"parent carousel container failed: {e}"}
    parent_id = _unwrap_id(rp)
    if not parent_id:
        return {"ok": False,
                "error": f"no parent carousel container id. raw={rp!r:.300}"}
    log.info(f"carousel parent container: {parent_id}")

    # === Step 3: publish parent ===
    try:
        rpub = _execute(client, "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
            "ig_user_id": IG_USER_ID,
            "creation_id": parent_id,
            "max_wait_seconds": max_wait_seconds,
        })
    except Exception as e:
        log.exception("carousel publish exception")
        return {"ok": False, "creation_id": parent_id,
                "error": f"carousel publish failed: {e}"}
    media_id = _unwrap_id(rpub)
    if not media_id:
        return {"ok": False, "creation_id": parent_id,
                "error": f"no media_id after publish. raw={rpub!r:.300}"}
    log.info(f"carousel published: {media_id}")

    # === Step 4: permalink (non-fatal) ===
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
        "creation_id": parent_id,
        "media_id": media_id,
        "permalink": permalink,
    }
