from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


GRAPHQL_URL = "https://api.github.com/graphql"


@dataclass(frozen=True)
class StarData:
    repo: str
    description: str
    stars: tuple[datetime, ...]
    total: int
    fetched_at: datetime


def _request(token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-star-chart/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub GraphQL returned HTTP {exc.code}: {detail}") from exc
    if result.get("errors"):
        raise RuntimeError("GitHub GraphQL error: " + json.dumps(result["errors"], ensure_ascii=False))
    return result["data"]


def fetch_star_history(repo: str, token: str) -> StarData:
    if "/" not in repo:
        raise ValueError("repo must use OWNER/NAME format")
    owner, name = repo.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        description
        stargazerCount
        stargazers(first:100, after:$cursor, orderBy:{field:STARRED_AT, direction:ASC}) {
          edges { starredAt }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    cursor = None
    stars: list[datetime] = []
    description = ""
    total = 0
    while True:
        data = _request(token, {"query": query, "variables": {"owner": owner, "name": name, "cursor": cursor}})
        repository = data.get("repository")
        if not repository:
            raise RuntimeError(f"repository {repo!r} was not found or token lacks access")
        description = repository.get("description") or ""
        total = int(repository.get("stargazerCount") or 0)
        connection = repository["stargazers"]
        for edge in connection.get("edges") or []:
            stars.append(datetime.fromisoformat(edge["starredAt"].replace("Z", "+00:00")))
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    return StarData(repo, description, tuple(stars), total, datetime.now(timezone.utc))


def load_fixture(path: str) -> StarData:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    stars = tuple(datetime.fromisoformat(item.replace("Z", "+00:00")) for item in value["stars"])
    fetched = datetime.fromisoformat(value.get("fetched_at", "2026-08-27T00:00:00+00:00").replace("Z", "+00:00"))
    return StarData(value["repo"], value.get("description", ""), stars, int(value.get("total", len(stars))), fetched)
