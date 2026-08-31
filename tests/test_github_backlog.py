import hashlib
import hmac

from cogs import github_backlog as backlog


def test_strip_markdown_strips_heading_link_and_bold():
    text = "## Fix [the bug](https://example.com) in **bold**"
    assert backlog.strip_markdown(text) == "Fix the bug in bold"


def test_strip_markdown_collapses_newlines():
    text = "line one\nline two\r\nline three"
    assert backlog.strip_markdown(text) == "line one line two line three"


def test_verify_signature_accepts_correct_signature():
    secret = "supersecret"
    body = b'{"action": "opened"}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert backlog.verify_signature(secret, body, signature) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"action": "opened"}'
    signature = "sha256=" + hmac.new(b"othersecret", body, hashlib.sha256).hexdigest()
    assert backlog.verify_signature("supersecret", body, signature) is False


def test_verify_signature_rejects_missing_header():
    assert backlog.verify_signature("supersecret", b"{}", None) is False


def test_verify_signature_rejects_wrong_prefix():
    assert backlog.verify_signature("supersecret", b"{}", "sha1=deadbeef") is False


def test_opengraph_image_url_pr_vs_issue():
    assert (
        backlog.opengraph_image_url("nuesports/bot", "pr", 45)
        == "https://opengraph.githubassets.com/1/nuesports/bot/pull/45"
    )
    assert (
        backlog.opengraph_image_url("nuesports/bot", "issue", 49)
        == "https://opengraph.githubassets.com/1/nuesports/bot/issues/49"
    )


def test_first_body_line_skips_blank_lines_and_strips_markdown():
    body = "\n\n## Heading line\nActual first content line\nsecond line"
    assert backlog.first_body_line(body) == "Heading line"


def test_first_body_line_none_body_returns_placeholder():
    assert backlog.first_body_line(None) == "No description provided."
