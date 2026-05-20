import json
import os
import re
import urllib.request
import urllib.error

API_BASE = "https://api.cloudflare.com/client/v4"

DEFAULT_ALLOWED = [
    "api.telegram.org",
    "discord.com",
    "discordapp.com",
    "gateway.discord.gg",
    "status.discord.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
]


def cf_request(method, path, token, body=None, content_type="application/json"):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            msg = (err.get("errors") or [{}])[0].get("message", str(e))
        except Exception:
            msg = str(e)
        raise RuntimeError(f"Cloudflare API {e.code}: {msg}")
    if not payload.get("success"):
        msg = (payload.get("errors") or [{}])[0].get("message", "Unknown error")
        raise RuntimeError(msg)
    return payload["result"]


def slugify(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or "eumenes-proxy")[:63].rstrip("-")


def render_worker(allowed_targets):
    return f"""addEventListener("fetch", (event) => {{
  event.respondWith(handleRequest(event.request));
}});

const ALLOWED_TARGETS = {json.dumps(allowed_targets)};

function isAllowedHost(hostname) {{
  const normalized = String(hostname || "").trim().toLowerCase();
  if (!normalized) return false;
  return ALLOWED_TARGETS.some((domain) => normalized === domain || normalized.endsWith(`.${{domain}}`));
}}

async function handleRequest(request) {{
  const url = new URL(request.url);
  const queryTarget = url.searchParams.get("proxy_target");
  const targetHost = request.headers.get("x-target-host") || queryTarget;

  let targetBase = "";
  if (targetHost) {{
    if (!isAllowedHost(targetHost)) {{
      return new Response(`Forbidden: Host ${{targetHost}} is not allowed.`, {{ status: 403 }});
    }}
    targetBase = `https://${{targetHost}}`;
  }} else {{
    return new Response("Invalid request: No target host provided.", {{ status: 400 }});
  }}

  const cleanSearch = new URLSearchParams(url.search);
  cleanSearch.delete("proxy_target");
  cleanSearch.delete("proxy_key");
  const searchStr = cleanSearch.toString();
  const targetUrl = targetBase + url.pathname + (searchStr ? `?${{searchStr}}` : "");

  const headers = new Headers(request.headers);
  for (const header of ["cf-connecting-ip", "cf-ray", "cf-visitor", "host", "x-real-ip", "x-target-host", "x-proxy-key"]) {{
    headers.delete(header);
  }}

  try {{
    return await fetch(new Request(targetUrl, {{
      method: request.method,
      headers,
      body: request.body,
      redirect: "follow",
    }}));
  }} catch (error) {{
    return new Response(`Proxy Error: ${{error.message}}`, {{ status: 502 }});
  }}
}}"""


def resolve_account_and_subdomain(api_token):
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account_id:
        accounts = cf_request("GET", "/accounts", api_token)
        if not accounts:
            raise RuntimeError("No Cloudflare account found")
        account_id = accounts[0]["id"]

    subdomain_info = cf_request("GET", f"/accounts/{account_id}/workers/subdomain", api_token)
    subdomain = (subdomain_info or {}).get("subdomain", "")
    if not subdomain:
        raise RuntimeError("Workers subdomain not configured. Enable workers.dev first.")
    return account_id, subdomain


def derive_worker_name():
    explicit = os.environ.get("CLOUDFLARE_PROXY_WORKER_NAME", "").strip()
    if explicit:
        return slugify(explicit)
    space_host = os.environ.get("SPACE_HOST", "").strip()
    if space_host:
        return slugify(f"{space_host.replace('.hf.space', '')}-proxy")
    return "eumenes-proxy"


def setup(api_token):
    existing_url = os.environ.get("CLOUDFLARE_PROXY_URL", "").strip()
    if existing_url:
        return existing_url

    try:
        account_id, subdomain = resolve_account_and_subdomain(api_token)
        worker_name = derive_worker_name()

        allowed = DEFAULT_ALLOWED.copy()
        extra_raw = os.environ.get("CLOUDFLARE_PROXY_DOMAINS", "").strip()
        if extra_raw and extra_raw != "*":
            extra = [v.strip() for v in extra_raw.split(",") if v.strip()]
            allowed.extend(extra)

        cf_request(
            "PUT",
            f"/accounts/{account_id}/workers/scripts/{worker_name}",
            api_token,
            body=render_worker(allowed).encode("utf-8"),
            content_type="application/javascript",
        )
        cf_request(
            "POST",
            f"/accounts/{account_id}/workers/scripts/{worker_name}/subdomain",
            api_token,
            body=json.dumps({"enabled": True, "previews_enabled": True}).encode("utf-8"),
        )

        proxy_url = f"https://{worker_name}.{subdomain}.workers.dev"
        return proxy_url
    except Exception as exc:
        print(f"Cloudflare proxy setup failed: {exc}")
        return ""
