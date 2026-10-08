# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/wsdl.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

WSDL contract discovery (PR5, design-document §31–§32).

``WsdlContractProvider`` parses a WSDL artifact with zeep and compiles it
into an :class:`OperationCatalog`.  Zeep is used strictly as a WSDL/binding/
type parser — the business runtime never calls ``zeep.Client(...).service``
(design §32).  Each WSDL operation becomes an ``OperationDefinition`` with
the stable key ``service:port:binding:operation`` (§5.3).

Each operation's request and response body schema is derived from zeep's
already-resolved type tree (``ZeepJsonSchemaMapper``), so the tools generated
from a WSDL carry real parameter names, types and cardinality.

The provider is offline-only: remote WSDL imports/XSD includes are not
followed (design §66 defers external reference fetching to the shared
``SafeReferenceFetcher``/``ContractArtifactResolver``).  A transport that
rejects http(s) fetches is installed so an artifact cannot trigger SSRF
through zeep.
"""

# Standard
import hashlib
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Third-Party
# ``zeep`` is an optional dependency (pyproject ``soap`` extra, design §35).
try:
    from zeep import Client, Transport
    from zeep.exceptions import Error as ZeepError

    ZEEP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional "soap" extra
    Client = None  # type: ignore[assignment]
    Transport = None  # type: ignore[assignment]
    ZeepError = None  # type: ignore[assignment]
    ZEEP_AVAILABLE = False

# First-Party
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    ContractDiagnostic,
    ContractProviderError,
    DiscoveryContext,
    OperationCatalog,
    OperationDefinition,
)
from mcpgateway.protocols.contracts.zeep_json_schema import ZeepJsonSchemaMapper
from mcpgateway.protocols.http.models import (
    HttpBodyVariant,
    HttpRequestContract,
    HttpResponseContract,
    HttpResponseVariant,
)


if ZEEP_AVAILABLE:

    class _LocalOnlyTransport(Transport):
        """zeep transport that refuses remote (http/https) fetches."""

        def load(self, url: str) -> bytes:
            """Refuse network loads; allow local files only.

            Args:
                url: The URL zeep asks to load.

            Returns:
                Local file bytes.

            Raises:
                ZeepError: When the URL is remote.
            """
            scheme = urlparse(url).scheme.lower()
            if scheme in ("http", "https"):
                raise ZeepError(f"Remote WSDL/XSD fetch refused: {url}")
            if scheme == "file":
                return Path(urlparse(url).path).read_bytes()
            # Bare paths are treated as local files.
            return Path(url).read_bytes()

else:  # pragma: no cover - optional "soap" extra
    _LocalOnlyTransport = None  # type: ignore[assignment]


def _body_schema(xsd_type: Any) -> dict[str, Any]:
    """Derive the JSON Schema of a SOAP body element from its zeep type.

    Args:
        xsd_type: A resolved zeep type — ``operation.input.body.type`` or
            ``operation.output.body.type``.

    Returns:
        The JSON Schema describing the body, tagged with the element's local
        name as its ``title`` so a tool listing names the wrapper a caller has
        to send.  Deriving this from zeep's resolved type tree is what gives a
        SOAP operation's parameters real names, types and cardinality instead
        of an opaque signature string.
    """
    schema = ZeepJsonSchemaMapper().map_type(xsd_type)
    name = getattr(xsd_type, "name", None)
    if name:
        schema["title"] = name
    return schema


class WsdlContractProvider:
    """Compile a WSDL artifact into an :class:`OperationCatalog` (§31)."""

    def __init__(self, transport: Optional[Transport] = None) -> None:
        """Initialise with an optional transport override (tests)."""
        self._transport = transport or (_LocalOnlyTransport() if ZEEP_AVAILABLE else None)

    async def discover(self, artifact: ContractArtifact, context: DiscoveryContext) -> OperationCatalog:
        """Discover operations from a WSDL artifact.

        Args:
            artifact: The WSDL artifact (``artifact_format="wsdl"``).
            context: Discovery context (unused in PR5).

        Returns:
            An ``OperationCatalog`` with one ``OperationDefinition`` per
            WSDL operation.

        Raises:
            ContractProviderError: When the artifact cannot be parsed.
        """
        del context  # PR5 does not need discovery context
        if not ZEEP_AVAILABLE:
            raise ContractProviderError("WSDL support requires the optional 'soap' extra (zeep); install it with `pip install '.[soap]'`")
        if artifact.artifact_format not in ("wsdl",):
            raise ContractProviderError(f"Unsupported WSDL artifact format: {artifact.artifact_format}")

        operations: list[OperationDefinition] = []
        diagnostics: list[ContractDiagnostic] = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                wsdl_path = Path(temp_dir) / "artifact.wsdl"
                wsdl_path.write_bytes(artifact.payload)
                client = Client(str(wsdl_path), transport=self._transport)
                for service_name, service in client.wsdl.services.items():
                    for port_name, port in service.ports.items():
                        binding = port.binding
                        # zeep exposes ``binding.name`` as an lxml QName; normalise
                        # it to its text form (e.g. ``{urn:report}ReportBinding``) so
                        # the value stays JSON-serialisable when it travels through
                        # ``extensions["binding"]`` and keeps the operation key stable
                        # (``str`` is the identity for a plain-string fallback).
                        binding_name = str(getattr(binding, "name", None) or port_name)
                        address = (port.binding_options or {}).get("address")
                        soap_version = "1.2" if "Soap12" in type(binding).__name__ else "1.1"
                        for operation_name, operation in binding._operations.items():
                            operation_definition = self._compile_operation(
                                service_name,
                                port_name,
                                binding_name,
                                address,
                                operation_name,
                                operation,
                                soap_version=soap_version,
                            )
                            if operation_definition is not None:
                                operations.append(operation_definition)
                            else:
                                diagnostics.append(
                                    ContractDiagnostic(
                                        severity="error",
                                        code="operation-schema-unavailable",
                                        message=f"Operation {operation_name} has no input/output type",
                                        location=f"{service_name}:{port_name}:{operation_name}",
                                    )
                                )
        except (ZeepError, OSError, ValueError, AttributeError) as exc:
            raise ContractProviderError(f"WSDL discovery failed: {exc}") from exc

        return OperationCatalog(
            source_type="wsdl",
            source_hash=hashlib.sha256(artifact.payload).hexdigest(),
            operations=tuple(operations),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _compile_operation(
        service_name: str,
        port_name: str,
        binding_name: str,
        address: Optional[str],
        operation_name: str,
        operation: Any,
        *,
        soap_version: str = "1.1",
    ) -> Optional[OperationDefinition]:
        """Compile one zeep operation into an OperationDefinition.

        Args:
            service_name: The WSDL service name.
            port_name: The port name.
            binding_name: The binding name.
            address: The SOAP endpoint address.
            operation_name: The operation name.
            operation: The zeep operation object.
            soap_version: ``"1.1"`` or ``"1.2"`` derived from the binding.

        Returns:
            The compiled operation, or ``None`` when no input type exists.
        """
        input_type = operation.input.body.type
        output_type = operation.output.body.type
        if input_type is None:
            return None

        path = "/"
        if address:
            parsed = urlparse(address)
            path = parsed.path or "/"

        content_type = "application/soap+xml" if soap_version == "1.2" else "text/xml"
        soap = {
            "version": soap_version,
            "operation": getattr(input_type, "name", operation_name),
            "namespace": getattr(input_type, "namespace", None),
            "soapAction": getattr(operation, "soapaction", None),
        }

        # Typed HTTP contracts, so a SOAP operation is compiled by the same
        # OperationToolCompiler as an OpenAPI one (design §31).  The SOAP
        # specifics travel in typed fields: the body codec, the response
        # codec (SOAP 1.1 replies arrive as text/xml) and ``soap_binding``.
        input_schema = _body_schema(input_type)
        request = HttpRequestContract(
            method="POST",
            path_template=path,
            parameters=(),
            bodies=(
                HttpBodyVariant(
                    media_type=content_type,
                    codec="soap",
                    schema=input_schema,
                    required=True,
                ),
            ),
        )
        response = HttpResponseContract(
            variants=(
                HttpResponseVariant(
                    status_code="200",
                    media_type=content_type,
                    schema=_body_schema(output_type),
                    description="SOAP response envelope",
                ),
            )
            if output_type is not None
            else (),
            codec="soap",
        )

        return OperationDefinition(
            key=f"{service_name}:{port_name}:{binding_name}:{operation_name}",
            protocol="http",
            source_operation_id=operation_name,
            title=operation_name,
            description=None,
            request=request,
            response=response,
            soap_binding=soap,
            extensions={
                "service": service_name,
                "port": port_name,
                "binding": binding_name,
            },
        )


__all__ = ["WsdlContractProvider"]
