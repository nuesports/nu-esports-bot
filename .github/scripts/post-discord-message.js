// Shared by the discord-backlog.yml jobs so both PR and issue notifications
// get the same mention-suppression and failure handling in one place.
const DISCORD_MESSAGE_LIMIT = 2000;
// Discord message flag: suppresses auto-embeds for every link in the content
// (the PR link, each closed-issue link, the author link) without affecting
// their clickability - otherwise a PR closing 3 issues would drop 4+ preview
// embeds into the channel.
const SUPPRESS_EMBEDS = 1 << 2;

module.exports = async function postDiscordMessage(core, webhookUrl, content) {
  if (!webhookUrl) {
    core.setFailed("Discord webhook secret is not set.");
    return;
  }

  const truncated =
    content.length > DISCORD_MESSAGE_LIMIT ? `${content.slice(0, DISCORD_MESSAGE_LIMIT - 1)}…` : content;

  let response;
  try {
    response = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // parse: [] suppresses @everyone/@here/@user/@role in PR and issue
      // titles, which are attacker-controlled text under pull_request_target.
      body: JSON.stringify({ content: truncated, allowed_mentions: { parse: [] }, flags: SUPPRESS_EMBEDS }),
    });
  } catch (err) {
    core.setFailed(`Failed to reach Discord webhook: ${err.message}`);
    return;
  }

  if (!response.ok) {
    core.setFailed(`Discord webhook responded with ${response.status}: ${await response.text()}`);
  }
};
