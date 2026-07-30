// Shared by the discord-backlog.yml jobs so both PR and issue notifications
// get the same mention-suppression and failure handling in one place.
module.exports = async function postDiscordMessage(core, webhookUrl, content) {
  if (!webhookUrl) {
    core.setFailed("Discord webhook secret is not set.");
    return;
  }

  const response = await fetch(webhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // parse: [] suppresses @everyone/@here/@user/@role in PR and issue
    // titles, which are attacker-controlled text under pull_request_target.
    body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
  });

  if (!response.ok) {
    core.setFailed(`Discord webhook responded with ${response.status}: ${await response.text()}`);
  }
};
