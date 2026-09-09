# -*- coding: utf-8 -*-
"""Location: ./tests/http_test_server/server.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Stdlib-only HTTP test server for PR3 integration tests.

Serves OpenAPI specs (JSON/YAML/two versions/an external-$ref variant) and
the business endpoints those specs describe, so the full registry chain —
import, candidate, diff, preview, activate, tool sync, invoke, health check
— runs against a real HTTP transport.  Run with ``--port``.
"""

# Standard
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Third-Party
import yaml

# ---------------------------------------------------------------------------
# OpenAPI documents
# ---------------------------------------------------------------------------

PET_SCHEMA = {
    "type": "object",
    "required": ["id", "name"],
    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "tag": {"type": "string"}},
}

NEW_PET_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}, "tag": {"type": "string"}},
}

SPEC_V1 = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore HTTP Test Server", "version": "1.0.0"},
    "paths": {
        "/v1/pets": {
            "get": {
                "operationId": "listPets",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
                    {"name": "tag", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}},
                    }
                },
            },
            "post": {
                "operationId": "createPet",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewPet"}}},
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}},
                    }
                },
            },
        },
        "/v1/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {
                "operationId": "getPet",
                "responses": {
                    "200": {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}
                },
            },
            "delete": {"operationId": "deletePet", "responses": {"204": {"description": "deleted"}}},
        },
        "/v1/pets/{petId}/head": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "head": {"operationId": "headPet", "responses": {"200": {"description": "ok"}}},
        },
        "/v1/pets/echo-form": {
            "post": {
                "operationId": "echoForm",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/x-www-form-urlencoded": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}
                        },
                    }
                },
            }
        },
        "/v1/pets/photo": {
            "post": {
                "operationId": "uploadPhoto",
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": {"petId": {"type": "integer"}, "caption": {"type": "string"}},
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"type": "object", "properties": {"received": {"type": "boolean"}}}}
                        },
                    }
                },
            }
        },
        "/v1/external-redirect": {
            # Declares 200 so the provider keeps the operation; the server
            # still answers 302 and the adapter must block hop 2 (SSRF).
            "get": {"operationId": "externalRedirect", "responses": {"200": {"description": "ok"}}}
        },
    },
    "components": {"schemas": {"Pet": PET_SCHEMA, "NewPet": NEW_PET_SCHEMA}},
}

# v2 adds one operation and removes the form operation (schema drift).
SPEC_V2 = json.loads(json.dumps(SPEC_V1))
SPEC_V2["info"]["version"] = "2.0.0"
del SPEC_V2["paths"]["/v1/pets/echo-form"]
SPEC_V2["paths"]["/v1/pets/filter"] = {
    "get": {
        "operationId": "filterPets",
        "parameters": [{"name": "status", "in": "query", "required": False, "schema": {"type": "string"}}],
        "responses": {
            "200": {
                "description": "ok",
                "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}},
            }
        },
    }
}

PETS = [{"id": 1, "name": "Fido", "tag": "dog"}]
NEXT_PET_ID = [2]


class TestServerHandler(BaseHTTPRequestHandler):
    """Serve the specs above and their matching business endpoints."""

    def log_message(self, fmt, *args):  # pylint: disable=arguments-differ
        """Keep the test server quiet."""

    def _send_raw(self, body: bytes, content_type: str, status: int = 200) -> None:
        """Send raw bytes with the given content type."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status: int = 200) -> None:
        """Send a JSON payload."""
        self._send_raw(json.dumps(payload).encode(), "application/json", status)

    def _send_redirect(self, location: str) -> None:
        """Send a 302 redirect."""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> bytes:
        """Read the request body per Content-Length."""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _extref_spec(self) -> dict:
        """SPEC_V1 plus one operation whose response schema is an external $ref."""
        spec = json.loads(json.dumps(SPEC_V1))
        spec["paths"]["/v1/pets/external"] = {
            "get": {
                "operationId": "getPetExternal",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"http://127.0.0.1:{self.server.server_port}/schemas/pet.json"}
                            }
                        },
                    }
                },
            }
        }
        return spec

    def _pet_by_id(self, pet_id: int):
        """Return one pet row or None."""
        return next((pet for pet in PETS if pet["id"] == pet_id), None)

    def do_HEAD(self) -> None:  # pylint: disable=invalid-name
        """Answer HEAD requests for the spec's headPet operation."""
        path = urlparse(self.path).path
        if path.startswith("/v1/pets/") and path.endswith("/head"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Dispatch GET requests for specs and business endpoints."""
        path = urlparse(self.path).path
        if path in {"/health", "/"}:
            # The health checker GETs ``base_url`` verbatim, so "/" must answer 200.
            self._send_json({"status": "ok"})
        elif path == "/openapi.json":
            self._send_raw(json.dumps(SPEC_V1).encode(), "application/json")
        elif path == "/openapi-v2.json":
            self._send_raw(json.dumps(SPEC_V2).encode(), "application/json")
        elif path == "/openapi.yaml":
            self._send_raw(yaml.safe_dump(SPEC_V1, sort_keys=False).encode(), "application/x-yaml")
        elif path == "/openapi-extref.json":
            self._send_raw(json.dumps(self._extref_spec()).encode(), "application/json")
        elif path == "/schemas/pet.json":
            self._send_raw(json.dumps(PET_SCHEMA).encode(), "application/json")
        elif path == "/v1/pets":
            self._send_json(PETS)
        elif path == "/v1/pets/filter":
            self._send_json([])
        elif path == "/v1/pets/external":
            self._send_json(PETS[0])
        elif path == "/v1/external-redirect":
            self._send_redirect("http://169.254.169.254/latest/meta-data/")
        elif path.startswith("/v1/pets/"):
            try:
                pet_id = int(path.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                self._send_json({"error": "not found"}, 404)
                return
            pet = self._pet_by_id(pet_id)
            if pet is None:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json(pet)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """Dispatch POST requests for create/form/multipart endpoints."""
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/v1/pets":
            payload = json.loads(body or b"{}")
            pet = {"id": NEXT_PET_ID[0], "name": payload.get("name"), "tag": payload.get("tag")}
            NEXT_PET_ID[0] += 1
            PETS.append(pet)
            self._send_json(pet, 201)
        elif path == "/v1/pets/echo-form":
            form = parse_qs(body.decode())
            self._send_json({"name": form.get("name", [""])[0]})
        elif path == "/v1/pets/photo":
            self._send_json({"received": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # pylint: disable=invalid-name
        """Dispatch DELETE requests for the deletePet operation."""
        path = urlparse(self.path).path
        if not path.startswith("/v1/pets/"):
            self._send_json({"error": "not found"}, 404)
            return
        try:
            pet_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            self._send_json({"error": "not found"}, 404)
            return
        if self._pet_by_id(pet_id) is None:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    """Parse arguments and serve until killed."""
    parser = argparse.ArgumentParser(description="PR3 HTTP registry test server")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), TestServerHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
