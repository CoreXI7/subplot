"""Subplot's auth.py: the shared signin-core account layer (subplot/signin_core/auth.py), configured for Subplot.
Kept at this path so the documented host commands work:
  sudo runuser -u <service account> -- env SUBS_STATE=<state folder> python3 -I <code folder>/subplot/auth.py \
      status | unlock | setup-code | signout | reset [--yes]
Imported as `auth`, it IS signin_core.auth (configured)."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signin                                     # noqa: E402  (checks the pin, builds the Config, configures auth)
from signin_core import auth as _core             # noqa: E402

if __name__ == "__main__":
    sys.exit(_core.cli(signin.CONFIG, sys.argv[1:]))
sys.modules[__name__] = _core
