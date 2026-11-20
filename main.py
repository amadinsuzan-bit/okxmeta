import os
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Meta OAuth Token & IG Asset Exchanger")

META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
# Must match the exact redirect_uri sent in your login dialog and whitelisted in Meta App Dashboard
META_REDIRECT_URI = os.getenv("META_REDIRECT_URI")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v27.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def extract_owner_info(me_payload: object) -> dict:
    """Safely extract id & name from /me whether Meta returns a dict, wrapped dict, or list."""
    if isinstance(me_payload, list):
        first = me_payload[0] if me_payload and isinstance(me_payload[0], dict) else {}
        return {"id": first.get("id"), "name": first.get("name")}
    if isinstance(me_payload, dict):
        if isinstance(me_payload.get("data"), list) and me_payload["data"]:
            first = me_payload["data"][0] if isinstance(me_payload["data"][0], dict) else {}
            return {"id": first.get("id"), "name": first.get("name")}
        return {"id": me_payload.get("id"), "name": me_payload.get("name")}
    return {"id": None, "name": None}


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
        # Step 1: Swap authorization `code` for a short-lived token
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

        short_lived_token = short_data["access_token"]

        # Step 2: Swap short-lived token for a long-lived / System User token
        long_resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_lived_token,
            },
        )
        long_data = long_resp.json()
        if long_resp.status_code != 200 or "access_token" not in long_data:
            raise HTTPException(status_code=400, detail={"step": "long_lived_token", "meta": long_data})

        access_token = long_data["access_token"]
        expires_in = long_data.get("expires_in")

        # Step 3: Fetch basic `/me` info (System User or User ID/name)
        me_resp = await client.get(
            f"{GRAPH_BASE}/me",
            params={
                "fields": "id,name",
                "access_token": access_token,
            },
        )
        me_payload = me_resp.json() if me_resp.status_code == 200 else {}
        account_owner = extract_owner_info(me_payload)

        # Step 4: Fetch assigned Instagram accounts (with pagination support)
        ig_accounts = []
        next_url: str | None = f"{GRAPH_BASE}/me/assigned_instagram_accounts"
        next_params: dict | None = {
            "fields": "id,username,name,account_type,profile_picture_url",
            "access_token": access_token,
        }

        while next_url:
            ig_resp = await client.get(next_url, params=next_params)
            ig_data = ig_resp.json()
            if ig_resp.status_code != 200 or "data" not in ig_data:
                raise HTTPException(
                    status_code=400,
                    detail={"step": "assigned_instagram_accounts", "meta": ig_data},
                )

            ig_accounts.extend(ig_data["data"])
            next_url = ig_data.get("paging", {}).get("next")
            next_params = None  # `next` URL already includes query parameters

        # TODO: Persist `access_token`, `account_owner`, and `ig_accounts` in your database
        return {
            "status": "success",
            "access_token": access_token,
            "expires_in_seconds": expires_in,
            "account_owner": account_owner,
            "instagram_accounts": [
                {
                    "ig_user_id": acc.get("id"),
                    "username": acc.get("username"),
                    "name": acc.get("name"),
                    "account_type": acc.get("account_type"),
                    "profile_picture_url": acc.get("profile_picture_url"),
                }
                for acc in ig_accounts
            ],
        }
