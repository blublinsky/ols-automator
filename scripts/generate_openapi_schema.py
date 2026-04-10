"""Generate the OpenAPI schema JSON from the FastAPI application."""

import json
import sys

from fastapi.openapi.utils import get_openapi

from app.main import app


def generate_schema() -> dict:
    """Build the OpenAPI schema dict from the running app definition."""
    return get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        license_info=app.license_info,
        routes=app.routes,
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python generate_openapi_schema.py <filename>")
        sys.exit(1)

    filename = sys.argv[1]
    schema = generate_schema()

    with open(filename, "w", encoding="utf-8") as fout:
        json.dump(schema, fout, indent=4)
        fout.write("\n")

    print(f"OpenAPI schema written to {filename}")
