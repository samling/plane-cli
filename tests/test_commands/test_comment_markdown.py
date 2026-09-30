"""Comment input and HTML payload tests; no live Plane requests."""

from __future__ import annotations

from io import StringIO
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from planecli.commands import comments
from planecli.exceptions import ValidationError


@pytest.fixture(params=["create", "update"])
def comment_command(request, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(comments, "get_client", MagicMock(return_value=client))
    monkeypatch.setattr(comments, "get_workspace", lambda: "ws")
    monkeypatch.setattr(
        comments,
        "resolve_work_item_across_projects_async",
        AsyncMock(return_value=({"id": "item-1"}, "p1")),
    )
    sdk = AsyncMock(return_value=MagicMock(model_dump=lambda: {"id": "comment-1"}))
    monkeypatch.setattr(comments, "run_sdk", sdk)
    invalidate = AsyncMock()
    monkeypatch.setattr("planecli.cache.invalidate_resource", invalidate)
    monkeypatch.setattr(comments, "output_single", MagicMock())
    command = getattr(comments, request.param)
    args = {"issue": "ABC-1", "json": True}
    if request.param == "update":
        args["comment_id"] = "comment-1"
    return command, args, sdk, invalidate


async def test_markdown_code_block_payload(comment_command):
    command, args, sdk, invalidate = comment_command
    body = '# Diagnostics\n\n```python\nif x < 2:\n    print("a & b")\n```\n'

    await command(**args, body=body, format="markdown")

    assert sdk.await_args.args[-1].comment_html == (
        '<h1>Diagnostics</h1>\n<pre><code class="language-python">'
        'if x &lt; 2:\n    print(&quot;a &amp; b&quot;)\n</code></pre>\n'
    )
    invalidate.assert_awaited_once_with("comments", "ws", "p1", "item-1")


async def test_markdown_rich_formatting(comment_command):
    command, args, sdk, _ = comment_command
    body = (
        "## Status\n\n**Ready** and *verified* with `kubectl`\n\n"
        "- first\n- second\n\n1. inspect\n2. verify\n\n"
        "> observation\n\n[Docs](https://example.com)\n\n"
        "| Host | State |\n| --- | --- |\n| dh2 | Ready |\n"
    )

    await command(**args, body=body, format="markdown")

    result = sdk.await_args.args[-1].comment_html
    for fragment in (
        "<h2>Status</h2>", "<strong>Ready</strong>", "<em>verified</em>",
        "<code>kubectl</code>", "<ul>", "<li>first</li>", "<ol>", "<blockquote>",
        '<a href="https://example.com">Docs</a>', "<table>", "<td>Ready</td>",
    ):
        assert fragment in result


async def test_markdown_escapes_raw_html_and_unsafe_links(comment_command):
    command, args, sdk, _ = comment_command
    await command(
        **args, body='<script>alert(1)</script>\n\n[click](javascript:alert(1))',
        format="markdown",
    )
    result = sdk.await_args.args[-1].comment_html
    assert "&lt;script&gt;" in result
    assert "<script>" not in result
    assert 'href="javascript:' not in result


@pytest.mark.parametrize("body", ["hello", "## literal\n```text\ncode\n```", "<b>legacy</b>", ""])
async def test_default_format_preserves_legacy_payload(comment_command, body):
    command, args, sdk, _ = comment_command
    await command(**args, body=body)
    assert sdk.await_args.args[-1].comment_html == f"<p>{body}</p>"


async def test_markdown_body_file_is_utf8(comment_command, tmp_path):
    command, args, sdk, _ = comment_command
    path = tmp_path / "comment.md"
    path.write_text("## Résumé\n\n```\n  α < β\n```\n", encoding="utf-8")
    await command(**args, body_file=str(path), format="markdown")
    assert sdk.await_args.args[-1].comment_html == (
        "<h2>Résumé</h2>\n<pre><code>  α &lt; β\n</code></pre>\n"
    )


async def test_markdown_from_stdin(comment_command, monkeypatch):
    command, args, sdk, _ = comment_command
    monkeypatch.setattr("sys.stdin", StringIO("```sh\necho ok\n```\n"))
    await command(**args, body_file="-", format="markdown")
    assert sdk.await_args.args[-1].comment_html == (
        '<pre><code class="language-sh">echo ok\n</code></pre>\n'
    )


async def test_plain_body_file(comment_command, tmp_path):
    command, args, sdk, _ = comment_command
    path = tmp_path / "comment.txt"
    path.write_text("## Not a heading\n", encoding="utf-8")
    await command(**args, body_file=str(path), format="plain")
    assert sdk.await_args.args[-1].comment_html == "<p>## Not a heading\n</p>"


@pytest.mark.parametrize("inputs", [{}, {"body": "", "body_file": "-"}])
async def test_requires_exactly_one_input_before_api_access(comment_command, inputs):
    command, args, sdk, _ = comment_command
    with pytest.raises(ValidationError, match="exactly one of --body or --body-file"):
        await command(**args, **inputs)
    cast(MagicMock, comments.get_client).assert_not_called()
    sdk.assert_not_awaited()


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8"])
async def test_file_errors_are_validation_errors(comment_command, tmp_path, kind):
    command, args, sdk, _ = comment_command
    path = tmp_path / "comment.md"
    if kind == "directory":
        path.mkdir()
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    with pytest.raises(ValidationError, match="Cannot read comment"):
        await command(**args, body_file=str(path), format="markdown")
    cast(MagicMock, comments.get_client).assert_not_called()
    sdk.assert_not_awaited()


@pytest.mark.parametrize("source", ["body", "file", "stdin"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "```bash\necho '<ok>'\n```\n",
            '<pre><code class="language-bash">echo \'&lt;ok&gt;\'\n</code></pre>\n',
        ),
        ("- first\n- second", "<ul>\n<li>first</li>\n<li>second</li>\n</ul>\n"),
    ],
)
async def test_cli_markdown_input(comment_command, tmp_path, monkeypatch, source, body, expected):
    command, _, sdk, _ = comment_command
    tokens = [command.__name__]
    tokens += ["ABC-1"] if command is comments.create else ["comment-1", "--issue", "ABC-1"]
    tokens += ["--format", "markdown", "--json"]
    if source == "body":
        tokens += ["-b", body]
    elif source == "file":
        path = tmp_path / "comment.md"
        path.write_text(body, encoding="utf-8")
        tokens += ["--body-file", str(path)]
    else:
        monkeypatch.setattr("sys.stdin", StringIO(body))
        tokens += ["--body-file", "-"]

    parsed, bound, _ = comments.comment_app.parse_args(
        tokens, exit_on_error=False, print_error=False
    )
    await parsed(*bound.args, **bound.kwargs)

    assert sdk.await_args.args[-1].comment_html == expected
    cast(MagicMock, comments.output_single).assert_called_once_with(
        {"id": "comment-1", "actor_name": "", "body_text": ""}, [], as_json=True
    )
