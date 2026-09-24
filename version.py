"""The version, in one place.

Everything that needs it reads it from here: the installer, the Windows file
properties on both exes, the portable zip's filename, and the git tag the
release is cut from. When these were separate strings they drifted.

Releases are additive. A new build gets a new version and a new tag; an
existing release is never rewritten, because a link someone already has must
keep pointing at the bytes they were given.
"""

VERSION = "1.3.2"

# Windows wants a 4-part tuple for the file version resource.
VERSION_TUPLE = tuple(int(x) for x in VERSION.split(".")) + (0,)

TAG = "v" + VERSION
PUBLISHER = "builtbycodyrd"
APP_NAME = "Aether Remote"


if __name__ == "__main__":
    print(VERSION)
