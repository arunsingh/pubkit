# Homebrew formula template.
#
# Publish this in your own tap so users can `brew install arunsingh/tap/pubkit`:
#
#   gh repo create homebrew-tap --public
#   # then regenerate the sha256 below from the PyPI sdist:
#   curl -sL https://files.pythonhosted.org/.../pubkit-0.1.0.tar.gz | shasum -a 256
#
# `brew create --python` will fill in the resource blocks for you once pubkit is
# on PyPI — run that rather than writing them by hand.
class Pubkit < Formula
  include Language::Python::Virtualenv

  desc "Publish one source to many platforms, safely"
  homepage "https://github.com/arunsingh/pubkit"
  url "https://files.pythonhosted.org/packages/source/p/pubkit/pubkit-0.1.0.tar.gz"
  sha256 "REPLACE_WITH_SDIST_SHA256"
  license "Apache-2.0"

  depends_on "python@3.12"

  # brew create --python https://pypi.org/project/pubkit/ generates these.
  # resource "pydantic" do ... end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "pubkit", shell_output("#{bin}/pubkit --version")
    system bin/"pubkit", "init", testpath/"demo"
    system bin/"pubkit", "validate", testpath/"demo/content"
  end
end
