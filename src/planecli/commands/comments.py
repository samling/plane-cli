"""Comment commands for work items."""

from __future__ import annotations

from typing import Annotated, Literal

import cyclopts
from cyclopts import Parameter
from plane.errors import PlaneError

from planecli.api.async_sdk import run_sdk
from planecli.api.client import get_client, get_workspace, handle_api_error
from planecli.exceptions import PlaneCLIError, ValidationError
from planecli.formatters import output, output_single
from planecli.utils.resolve import (
    resolve_work_item_across_projects_async,
    resolve_work_item_async,
)

comment_app = cyclopts.App(
    name=["comment", "comments"],
    help="Manage work item comments.",
)

COMMENT_COLUMNS = [
    ("id", "ID"),
    ("actor_name", "Author"),
    ("body_text", "Comment"),
    ("created_at", "Created"),
]


def _enrich_comment(data: dict, members_map: dict[str, str] | None = None) -> dict:
    """Add convenience fields to a comment dict.

    members_map: optional {member_id: display_name} used to resolve the actor
    UUID to a human name. Without it (or on a miss), actor_name falls back to
    the raw UUID — the public comment API returns no actor_detail.
    """
    actor = data.get("actor_detail") or data.get("actor") or {}
    if isinstance(actor, dict):
        data["actor_name"] = actor.get("display_name") or actor.get("first_name", "")
    else:
        resolved = members_map.get(actor) if members_map else None
        data["actor_name"] = resolved or (str(actor) if actor else "")

    # Strip HTML from comment body
    body_html = data.get("comment_html") or ""
    if body_html:
        import re
        data["body_text"] = re.sub(r"<[^>]+>", "", body_html).strip()
    else:
        data["body_text"] = ""

    return data


async def fetch_issue_comments(
    workspace: str, project_id: str, item_id: str
) -> list[dict]:
    """Fetch, enrich, and chronologically sort all comments for a work item.

    Single source of truth shared by `comment ls` and `wi show`. Resolves the
    actor UUID to a display name via the (already 1h-cached) members list.

    Always raises on failure — callers own the failure policy.
    """
    from planecli.cache import cached_list_comments, cached_list_members

    comments = await cached_list_comments(workspace, project_id, item_id)
    # Author-name resolution is a nicety, not the point of this call: if the
    # members list fails to load, fall back to an empty map (actor_name then
    # falls back to the raw UUID, per _enrich_comment) instead of losing the
    # comments themselves.
    try:
        members = await cached_list_members(workspace)
    except (PlaneError, PlaneCLIError):
        members = []
    members_map = {
        m["id"]: (m.get("display_name") or m.get("first_name") or "")
        for m in members
        if m.get("id")
    }
    enriched = [_enrich_comment(c, members_map) for c in comments]
    enriched.sort(key=lambda c: c.get("created_at") or "")
    return enriched


@comment_app.command(name="list", alias="ls")
async def list_(
    issue: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    limit: Annotated[int, Parameter(alias="-l")] = 50,
    json: bool = False,
) -> None:
    """List comments on a work item.

    Parameters
    ----------
    issue
        Work item identifier (ABC-123) or UUID.
    project
        Project name/ID (required for name-based lookup).
    limit
        Maximum comments to show (the most recent N).
    """
    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            from planecli.utils.resolve import resolve_project_async
            proj = await resolve_project_async(project, client, workspace)
            project_id = proj["id"]
            item = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item["id"]
        comments = await fetch_issue_comments(workspace, project_id, item_id)
    except PlaneError as e:
        raise handle_api_error(e)

    # --limit selects the most recent N, still rendered oldest -> newest.
    # Non-positive limits mean "no results" (consistent with `wi ls`'s
    # `data[:limit]`, where limit=0 already yields an empty list).
    n = max(limit, 0)
    data = comments[-n:] if n else []
    output(data, COMMENT_COLUMNS, title=f"Comments on {issue}", as_json=json)


def _prepare_comment_html(
    body: str | None, body_file: str | None, format: Literal["plain", "markdown"]
) -> str:
    """Read one comment input and render it for Plane's HTML API."""
    if (body is None) == (body_file is None):
        raise ValidationError("Provide exactly one of --body or --body-file.")

    if body_file is not None:
        import sys
        from pathlib import Path

        try:
            body = sys.stdin.read() if body_file == "-" else Path(body_file).read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError) as e:
            raise ValidationError(f"Cannot read comment from {body_file!r}: {e}") from e

    assert body is not None
    if format == "markdown":
        from markdown_it import MarkdownIt

        return MarkdownIt("commonmark", {"html": False}).enable("table").render(body)
    return f"<p>{body}</p>"


@comment_app.command(alias="new")
async def create(
    issue: str,
    *,
    body: Annotated[str | None, Parameter(alias="-b", allow_leading_hyphen=True)] = None,
    body_file: Annotated[str | None, Parameter(allow_leading_hyphen=True)] = None,
    format: Literal["plain", "markdown"] = "plain",
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    json: bool = False,
) -> None:
    """Add a comment to a work item.

    Parameters
    ----------
    issue
        Work item identifier (ABC-123) or UUID.
    body
        Comment text. Mutually exclusive with --body-file.
    body_file
        Read comment text from a UTF-8 file, or - for stdin.
    format
        Input format: plain (legacy paragraph wrapping) or markdown (rendered HTML).
    project
        Project name/ID (required for name-based lookup).
    """
    from plane.models.work_items import CreateWorkItemComment

    comment_html = _prepare_comment_html(body, body_file, format)

    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            from planecli.utils.resolve import resolve_project_async
            proj = await resolve_project_async(project, client, workspace)
            project_id = proj["id"]
            item = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item["id"]

        comment_data = CreateWorkItemComment(comment_html=comment_html)
        comment = await run_sdk(
            client.work_items.comments.create,
            workspace, project_id, item_id, comment_data,
        )
        data = _enrich_comment(comment.model_dump())
        from planecli.cache import invalidate_resource
        await invalidate_resource("comments", workspace, project_id, item_id)
    except PlaneError as e:
        raise handle_api_error(e)

    if json:
        output_single(data, [], as_json=True)
    else:
        from planecli.formatters import console
        console.print(f"[green]Comment added to {issue}.[/]")


@comment_app.command
async def update(
    comment_id: str,
    *,
    issue: str,
    body: Annotated[str | None, Parameter(alias="-b", allow_leading_hyphen=True)] = None,
    body_file: Annotated[str | None, Parameter(allow_leading_hyphen=True)] = None,
    format: Literal["plain", "markdown"] = "plain",
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    json: bool = False,
) -> None:
    """Update a comment on a work item.

    Parameters
    ----------
    comment_id
        Comment UUID.
    issue
        Work item identifier (ABC-123) or UUID.
    body
        New comment text. Mutually exclusive with --body-file.
    body_file
        Read comment text from a UTF-8 file, or - for stdin.
    format
        Input format: plain (legacy paragraph wrapping) or markdown (rendered HTML).
    project
        Project name/ID (required for name-based lookup).
    """
    from plane.models.work_items import UpdateWorkItemComment

    comment_html = _prepare_comment_html(body, body_file, format)

    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            from planecli.utils.resolve import resolve_project_async
            proj = await resolve_project_async(project, client, workspace)
            project_id = proj["id"]
            item = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item["id"]

        update_data = UpdateWorkItemComment(comment_html=comment_html)
        comment = await run_sdk(
            client.work_items.comments.update,
            workspace, project_id, item_id, comment_id, update_data,
        )
        data = _enrich_comment(comment.model_dump())
        from planecli.cache import invalidate_resource
        await invalidate_resource("comments", workspace, project_id, item_id)
    except PlaneError as e:
        raise handle_api_error(e)

    if json:
        output_single(data, [], as_json=True)
    else:
        from planecli.formatters import console
        console.print(f"[green]Comment updated on {issue}.[/]")


@comment_app.command
async def delete(
    comment_id: str,
    *,
    issue: str,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
) -> None:
    """Delete a comment from a work item.

    Parameters
    ----------
    comment_id
        Comment UUID.
    issue
        Work item identifier (ABC-123) or UUID.
    project
        Project name/ID (required for name-based lookup).
    """
    from planecli.formatters import console

    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            from planecli.utils.resolve import resolve_project_async
            proj = await resolve_project_async(project, client, workspace)
            project_id = proj["id"]
            item = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item["id"]
        await run_sdk(
            client.work_items.comments.delete,
            workspace, project_id, item_id, comment_id,
        )
        from planecli.cache import invalidate_resource
        await invalidate_resource("comments", workspace, project_id, item_id)
    except PlaneError as e:
        raise handle_api_error(e)

    console.print("[green]Comment deleted.[/]")
