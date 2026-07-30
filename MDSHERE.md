# Discord PR Backlog Webhook

## Description

Add a webhook that notifies the `#pr-backlog` channel in Discord whenever a pull request is opened on the repository. This provides visibility into new PRs across the team without requiring manual updates.

## Acceptance Criteria

- [ ] When a PR is opened, a message is automatically posted to the `#pr-backlog` Discord channel
- [ ] Message format displays as: `pr XX - subject name`
- [ ] If the PR closes an issue (detected from PR description/title), append: `(closes issue XX)`
- [ ] Both the PR number and issue number are clickable hyperlinks pointing to their respective GitHub pages
- [ ] The webhook uses the repository's GitHub Actions or webhook configuration
- [ ] Configuration is stored securely (Discord webhook URL in secrets, not in code)

## Example Message

```
pr 123 - Add Discord PR backlog webhook (closes issue 45)
```

Where:
- `pr 123` links to `https://github.com/[org]/[repo]/pull/123`
- `issue 45` links to `https://github.com/[org]/[repo]/issues/45`

## Implementation Notes

- Consider using GitHub Actions with a webhook trigger on `pull_request` opened events
- Parse PR title/body for "closes #XX" or "fixes #XX" patterns to detect related issues
- Format Discord message with markdown link syntax: `[text](url)`
- Store Discord webhook URL in repository secrets or environment configuration
