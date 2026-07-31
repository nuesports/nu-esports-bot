// PR/issue titles are attacker-controlled under pull_request_target. Neutralize
// Discord markdown in them so a title can't render as a masked link, bold/code
// span, blockquote, etc. in the trusted backlog channel.
const SPECIAL_CHARS = /[\\`*_~|>[\]()]/g;

module.exports = function escapeMarkdown(text) {
  // Collapse newlines first so embedded "\n> fake quote" or "\n# fake heading"
  // can't start a new line - those markers are only special at line-start.
  return text.replace(/\r?\n/g, " ").replace(SPECIAL_CHARS, "\\$&");
};
