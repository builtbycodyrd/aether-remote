"""The add-on's version, in one place.

Releases are git tags named  homelab-vX.Y.Z  in the aether-remote repo -
deliberately NOT GitHub Releases, so the Windows app's update check (which
reads the repo's latest Release) can never mistake an add-on version for a
Windows update.
"""

VERSION = "0.2.0"
TAG_PREFIX = "homelab-v"
TAG = TAG_PREFIX + VERSION
REPO = "builtbycodyrd/aether-remote"

if __name__ == "__main__":
    print(VERSION)
