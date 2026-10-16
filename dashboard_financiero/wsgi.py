"""WSGI para PythonAnywhere: en el panel Web apunta el fichero WSGI a ``from wsgi import application``."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from findash import create_app  # noqa: E402

application = create_app()
