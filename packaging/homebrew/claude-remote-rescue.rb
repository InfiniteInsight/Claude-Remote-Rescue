class ClaudeRemoteRescue < Formula
  include Language::Python::Virtualenv

  desc "Keep Claude Code sessions alive and rescuable when terminals or hosts die"
  homepage "https://github.com/InfiniteInsight/Claude-Remote-Rescue"
  url "https://github.com/InfiniteInsight/Claude-Remote-Rescue/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "PLACEHOLDER_FILL_AFTER_TAGGING_SEE_packaging_README"
  license "MIT"

  depends_on "python@3.12"

  # No `resource` blocks: claude-remote-rescue has zero runtime
  # dependencies by design (stdlib-only web server, dependency-free
  # shell shims). Do not add resources here without also revisiting
  # that project guarantee.

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "crr", shell_output("#{bin}/crr --help")
  end
end
