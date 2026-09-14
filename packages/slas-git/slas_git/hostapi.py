"""Merge/pull request creation on GitLab, Gitea and GitHub (CLAUDE.md §5.7 push policy).

Each adapter builds one request; an `HttpClient` sends it. The token travels in the
request header the host expects and nowhere else; the spec's `describe()` is what a log
may see and never includes headers.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import quote

from pydantic import Field

from slas_git.hosts import GitHost, RemoteUri
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class HttpRequestSpec(SlasModel):
    method: str = Field(pattern=r"^(GET|POST|PUT|PATCH)$")
    url: str = Field(pattern=r"^https?://")
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, object] | None = None
    timeout_s: int = 30

    def describe(self) -> str:
        return f"{self.method} {self.url}"


class HttpResponse(SlasModel):
    status: int
    body: str = ""

    def payload(self) -> dict[str, object]:
        try:
            data = json.loads(self.body or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class HttpClient(Protocol):
    def send(self, spec: HttpRequestSpec) -> HttpResponse: ...


class UrllibHttpClient:
    """Standard-library HTTPS; the platform's own CA bundle when the hosts are internal."""

    def __init__(self, *, ca_bundle: str | None = None) -> None:
        self.ca_bundle = ca_bundle

    def send(self, spec: HttpRequestSpec) -> HttpResponse:
        import ssl

        data = json.dumps(spec.body).encode("utf-8") if spec.body is not None else None
        headers = {**spec.headers, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 — scheme fixed to http(s) by the spec pattern
            spec.url, data=data, headers=headers, method=spec.method
        )
        context = (
            ssl.create_default_context(cafile=self.ca_bundle)
            if spec.url.startswith("https")
            else None
        )
        try:
            # The broker allowlists the host and the scheme is http(s) by the spec's pattern.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=spec.timeout_s, context=context
            ) as response:
                return HttpResponse(
                    status=response.status, body=response.read().decode("utf-8", "replace")
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read().decode("utf-8", "replace"))


class FakeHttpClient:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.sent: list[HttpRequestSpec] = []

    def send(self, spec: HttpRequestSpec) -> HttpResponse:
        self.sent.append(spec)
        return self.responses.pop(0) if self.responses else HttpResponse(status=201, body="{}")


class MergeRequest(SlasModel):
    url: str
    number: int | None = None
    title: str

    def sentence(self) -> str:
        return f"Opened a review request: {self.url}"


class HostApiError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def merge_request_spec(
    host: GitHost,
    uri: RemoteUri,
    *,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
    token: str,
) -> HttpRequestSpec:
    if host.api_base is None or host.kind == "generic":
        raise HostApiError(
            ThreePartMessage(
                f"{host.hostname} has no API the broker knows, so no review request was opened.",
                "Merge requests are created on GitLab, Gitea and GitHub hosts with an api_base.",
                "The branch was pushed; open the review request on the host yourself.",
            )
        )
    owner, repo = uri.owner_and_repo
    if host.kind == "gitlab":
        project = quote(uri.path, safe="")
        return HttpRequestSpec(
            method="POST",
            url=f"{host.api_base}/projects/{project}/merge_requests",
            headers={"PRIVATE-TOKEN": token},
            body={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": body,
                "remove_source_branch": True,
            },
        )
    if host.kind == "gitea":
        return HttpRequestSpec(
            method="POST",
            url=f"{host.api_base}/repos/{owner}/{repo}/pulls",
            headers={"Authorization": f"token {token}"},
            body={"head": source_branch, "base": target_branch, "title": title, "body": body},
        )
    return HttpRequestSpec(
        method="POST",
        url=f"{host.api_base}/repos/{owner}/{repo}/pulls",
        headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
        body={"head": source_branch, "base": target_branch, "title": title, "body": body},
    )


def parse_merge_request(host: GitHost, response: HttpResponse, *, title: str) -> MergeRequest:
    if response.status not in (200, 201):
        raise HostApiError(
            ThreePartMessage(
                f"{host.hostname} did not open the review request (HTTP {response.status}).",
                "The token may lack API scope, the target branch may not exist, or a request for "
                "this branch may already be open.",
                "The branch was pushed; check the token's scopes and open the request on the host.",
            )
        )
    data = response.payload()
    url = data.get("web_url") or data.get("html_url") or data.get("url")
    number = data.get("iid") or data.get("number")
    if not isinstance(url, str):
        raise HostApiError(
            ThreePartMessage(
                f"{host.hostname} answered without the address of the review request.",
                "The API response had no web_url or html_url.",
                "The branch was pushed; look for the request on the host.",
            )
        )
    return MergeRequest(
        url=url, number=int(number) if isinstance(number, int) else None, title=title
    )
