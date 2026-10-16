"""Punto de entrada compatible con PythonAnywhere / gunicorn / ``flask run``.

    gunicorn app:app          # Render
    flask --app app run       # local

Toda la lógica vive en el paquete ``findash``.
"""
from findash import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
