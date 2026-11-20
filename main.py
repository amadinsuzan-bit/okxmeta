import os
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Meta OAuth Token Exchanger")

META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
# Must match the exact redirect_uri sent in your login dialog and whitelisted in Meta App Dashboard
META_REDIRECT_URI = os.getenv("META_REDIRECT_URI")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v27.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/auth/meta/callback")
async def meta_oauth_callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_reason: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    if error:
        raise HTTPException(
            status_code=400,
            detail={
                "error": error,
                "error_reason": error_reason,
                "error_description": error_description,
            },
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization 'code' in query params")

    if not META_APP_ID or not META_APP_SECRET or not META_REDIRECT_URI:
        raise HTTPException(
            status_code=500,
            detail="Server missing META_APP_ID, META_APP_SECRET, or META_REDIRECT_URI environment variables",
        )

    async with httpx.AsyncClient(timeout=20.0) as client:
        # Step 1: Swap authorization `code` for a short-lived user access token (~1-2 hours)
        short_resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "redirect_uri": META_REDIRECT_URI,
                "code": code,
            },
        )
        short_data = short_resp.json()
        if short_resp.status_code != 200 or "access_token" not in short_data:
            raise HTTPException(status_code=400, detail={"step": "short_lived_token", "meta": short_data})

        short_lived_user_token = short_data["access_token"]

        # Step 2: Swap short-lived user token for a long-lived user access token (~60 days)
        long_resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_lived_user_token,
            },
        )
        long_data = long_resp.json()
        if long_resp.status_code != 200 or "access_token" not in long_data:
            raise HTTPException(status_code=400, detail={"step": "long_lived_user_token", "meta": long_data})

        long_lived_user_token = long_data["access_token"]
        user_token_expires_in = long_data.get("expires_in")

        # Step 3: Use long-lived user token to fetch Page access tokens (no expiration date)
        # Note: Handles pagination via `paging.next` if a user manages many Pages
        pages = []
        next_url: str | None = f"{GRAPH_BASE}/me/accounts"
        next_params: dict | None = {
            "fields": "id,name,category,access_token,instagram_business_account,tasks",
            "access_token": long_lived_user_token,
        }

        while next_url:
            pages_resp = await client.get(next_url, params=next_params)
            pages_data = pages_resp.json()
            if pages_resp.status_code != 200 or "data" not in pages_data:
                raise HTTPException(status_code=400, detail={"step": "page_access_tokens", "meta": pages_data})

            pages.extend(pages_data["data"])
            next_url = pages_data.get("paging", {}).get("next")
            next_params = None  # `next` URL already contains query params

        # TODO: Persist `long_lived_user_token` and `pages` (each Page's `access_token`) in your DB
        return {
            "status": "success",
            "long_lived_user_token": long_lived_user_token,
            "user_token_expires_in_seconds": user_token_expires_in,
            "pages": [
                {
                    "page_id": page.get("id"),
                    "page_name": page.get("name"),
                    "page_access_token": page.get("access_token"),
                    "instagram_business_account_id": (page.get("instagram_business_account") or {}).get("id"),
                    "tasks": page.get("tasks", []),
                }
                for page in pages
            ],
        }
