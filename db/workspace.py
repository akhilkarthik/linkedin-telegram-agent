import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
MAX_HISTORY = 60
MAX_ITEMS = 30


def _headers():
    return {
        "Authorization": f"token {os.getenv('GITHUB_TOKEN')}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github.v3+json",
    }


def _repo():
    return os.getenv("GITHUB_REPO", "akhilkarthik/linkedin-telegram-agent")


def load_workspace(user_id: int):
    path = f"data/workspace_{user_id}.json"
    url = f"https://api.github.com/repos/{_repo()}/contents/{path}"
    try:
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        content = json.loads(base64.b64decode(data["content"]).decode())
        return content, data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"user_id": user_id, "history": [], "items": []}, None
        raise


def save_workspace(user_id: int, workspace: dict, sha: str = None) -> str:
    path = f"data/workspace_{user_id}.json"
    url = f"https://api.github.com/repos/{_repo()}/contents/{path}"

    workspace["history"] = workspace["history"][-MAX_HISTORY:]
    workspace["items"] = workspace["items"][-MAX_ITEMS:]

    body = {
        "message": "workspace update",
        "content": base64.b64encode(json.dumps(workspace).encode()).decode()
    }
    if sha:
        body["sha"] = sha

    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT", headers=_headers())
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["content"]["sha"]


def add_item(workspace: dict, item_type: str, content: str, label: str) -> str:
    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    short_id = str(abs(hash(label + content)))[-6:]
    item_id = f"{item_type[:2]}_{short_id}"

    workspace["items"] = [i for i in workspace["items"] if i["id"] != item_id]
    workspace["items"].append({
        "id": item_id,
        "type": item_type,
        "label": label,
        "content": content,
        "saved_at": now
    })
    return item_id


def get_item_by_type(workspace: dict, item_type: str):
    matches = [i for i in workspace["items"] if i["type"] == item_type]
    return matches[-1] if matches else None


def get_items_context(workspace: dict) -> str:
    items = workspace.get("items", [])
    if not items:
        return "No saved items yet."
    lines = []
    for item in reversed(items[-15:]):
        label = item["type"].replace("_", " ").title()
        lines.append(f"[{item['id']}] {label}: \"{item['label']}\" — {item['saved_at']}")
    return "\n".join(lines)
