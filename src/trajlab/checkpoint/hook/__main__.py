"""Print the rendered settings.hooks.json (see `trajlab.checkpoint.hook`)."""

import sys

from trajlab.checkpoint.hook import render_settings

sys.stdout.write(render_settings())
