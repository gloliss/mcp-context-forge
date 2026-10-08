# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/database_template_service.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Query template CRUD and per-template tool materialization (Phase 2).

A ``DatabaseQueryTemplate`` packages one fixed statement plus the parameters it
binds, so an agent can run a curated query without holding the SQL.  Creating,
updating, enabling, or deleting a template keeps the matching MCP tool in step:
the tool exists exactly while its template is enabled, and its input schema is
the template's declared ``parameter_schema``.

Validation happens here rather than in the request schema because the rules are
cross-field — a renamed statement or a swapped parameter schema has to be
checked against the other half of the definition, which only the merged state
shows.
"""

# Standard
import re
from typing import Any, Optional

# Third-Party
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.adapters.database.sql_policy import SqlStatementClassifier
from mcpgateway.db import DatabaseQueryTemplate, DatabaseSource
from mcpgateway.schemas import (
    DatabaseQueryTemplateCreate,
    DatabaseQueryTemplateRead,
    DatabaseQueryTemplateUpdate,
)
from mcpgateway.services.database_source_service import DatabaseSourceNotFoundError
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.create_slug import slugify

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

#: Placeholder syntax a template statement may bind (``:name``).  The lookbehind
#: keeps PostgreSQL casts (``col::text``) from being read as a placeholder.
_PLACEHOLDER_PATTERN = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


class DatabaseTemplateError(ValueError):
    """Base error for query template operations."""


class DatabaseTemplateNotFoundError(DatabaseTemplateError):
    """Raised when a requested query template is not found."""


class DatabaseTemplateNameConflictError(DatabaseTemplateError):
    """Raised when a query template name or slug conflicts with an existing one."""

    def __init__(self, name: str, slug: str):
        """Initialize the conflict error.

        Args:
            name: The conflicting display name.
            slug: The conflicting slug.
        """
        self.name = name
        self.slug = slug
        super().__init__(f"Query template with name '{name}' or slug '{slug}' already exists")


class DatabaseTemplateService:
    """Manage query templates and the tools materialized from them."""

    @staticmethod
    def _require_source(db: Session, source_id: str) -> DatabaseSource:
        """Return the owning source, or raise as not found.

        Args:
            db: Database session.
            source_id: Database source ID.

        Returns:
            DatabaseSource: The source row.

        Raises:
            DatabaseSourceNotFoundError: If no such source exists.
        """
        source = db.get(DatabaseSource, source_id)
        if source is None:
            raise DatabaseSourceNotFoundError(f"Database source with ID '{source_id}' not found")
        return source

    @staticmethod
    def _require_template(db: Session, source_id: str, template_id: str) -> DatabaseQueryTemplate:
        """Return one template scoped to its source, or raise as not found.

        Args:
            db: Database session.
            source_id: Owning database source ID.
            template_id: Query template ID.

        Returns:
            DatabaseQueryTemplate: The template row.

        Raises:
            DatabaseTemplateNotFoundError: If no such template exists.
        """
        template = db.get(DatabaseQueryTemplate, template_id)
        if template is None or template.source_id != source_id:
            raise DatabaseTemplateNotFoundError(
                f"Query template with ID '{template_id}' not found for source '{source_id}'"
            )
        return template

    @staticmethod
    def _validate_definition(statement: str, parameter_schema: dict[str, Any]) -> None:
        """Validate a template definition as a whole.

        Two failures are caught at registration time instead of on every call:
        a statement carrying more than one SQL statement (which the SQL policy
        would refuse anyway), and a placeholder the parameter schema does not
        declare — which would otherwise leave the materialized tool unable to
        supply it.

        Args:
            statement: The template's SQL statement.
            parameter_schema: The template's declared parameters.

        Raises:
            DatabaseTemplateError: If the definition is not usable.
        """
        try:
            classification = SqlStatementClassifier().classify(statement)
        except Exception as exc:  # pragma: no cover - sqlparse is lenient
            raise DatabaseTemplateError(f"statement could not be parsed: {exc}") from exc

        if classification.statement_count == 0:
            raise DatabaseTemplateError("statement must contain a SQL statement")
        if classification.is_multi_statement:
            raise DatabaseTemplateError("statement must contain exactly one SQL statement")

        declared = set((parameter_schema or {}).get("properties") or {})
        undeclared = sorted(set(_PLACEHOLDER_PATTERN.findall(statement)) - declared)
        if undeclared:
            raise DatabaseTemplateError("statement binds undeclared parameter(s): " + ", ".join(undeclared))

    @staticmethod
    def _conflicting_template(
        db: Session, source_id: str, name: str, slug: str, exclude_id: Optional[str] = None
    ) -> Optional[DatabaseQueryTemplate]:
        """Return a template of ``source_id`` whose name or slug collides.

        Template slugs are unique per source, so a collision is scoped to the
        owning source rather than global.

        Args:
            db: Database session.
            source_id: Owning database source ID.
            name: Candidate display name.
            slug: Candidate slug.
            exclude_id: Optional ID to exclude (for updates).

        Returns:
            Optional[DatabaseQueryTemplate]: The conflicting template, or ``None``.
        """
        clause = and_(
            DatabaseQueryTemplate.source_id == source_id,
            or_(DatabaseQueryTemplate.name == name, DatabaseQueryTemplate.slug == slug),
        )
        if exclude_id is not None:
            clause = and_(clause, DatabaseQueryTemplate.id != exclude_id)
        return db.execute(select(DatabaseQueryTemplate).where(clause)).scalar_one_or_none()

    @classmethod
    def _to_read(cls, template: DatabaseQueryTemplate, source: DatabaseSource) -> DatabaseQueryTemplateRead:
        """Render a template, naming the tool it is exposed as when enabled.

        Args:
            template: The template row.
            source: The owning database source.

        Returns:
            DatabaseQueryTemplateRead: The read model.
        """
        # First-Party
        from mcpgateway.services.database_tool_service import DatabaseToolService  # pylint: disable=import-outside-toplevel

        read = DatabaseQueryTemplateRead.model_validate(template)
        if not template.enabled:
            return read
        return read.model_copy(update={"tool_name": DatabaseToolService.template_tool_name(source.slug, template.slug)})

    @classmethod
    def create_template(
        cls,
        db: Session,
        source_id: str,
        data: DatabaseQueryTemplateCreate,
        user_email: Optional[str] = None,
    ) -> DatabaseQueryTemplateRead:
        """Create a template and materialize its tool when enabled.

        Args:
            db: Database session (transaction owned by the caller).
            source_id: Owning database source ID.
            data: Validated creation payload.
            user_email: Email of the creating user.

        Returns:
            DatabaseQueryTemplateRead: The created template.

        Raises:
            DatabaseSourceNotFoundError: If the source does not exist.
            DatabaseTemplateNameConflictError: If the name/slug is taken.
            DatabaseTemplateError: If the definition is not usable.
        """
        # First-Party
        from mcpgateway.services.database_tool_service import DatabaseToolService  # pylint: disable=import-outside-toplevel

        source = cls._require_source(db, source_id)
        slug = slugify(data.name)
        cls._validate_definition(data.statement, data.parameter_schema)
        if cls._conflicting_template(db, source_id, data.name, slug) is not None:
            raise DatabaseTemplateNameConflictError(data.name, slug)

        template = DatabaseQueryTemplate(
            source_id=source_id,
            name=data.name,
            slug=slug,
            statement=data.statement,
            parameter_schema=data.parameter_schema,
            result_schema=data.result_schema,
            max_rows=data.max_rows,
            timeout_seconds=data.timeout_seconds,
            enabled=data.enabled,
            created_by=user_email,
        )
        db.add(template)
        db.flush()
        DatabaseToolService.sync_template(db, template, source)
        db.commit()
        db.refresh(template)
        logger.info("Created query template %s on source %s", template.name, source.name)
        return cls._to_read(template, source)

    @classmethod
    def list_templates(
        cls, db: Session, source_id: str, include_inactive: bool = False
    ) -> list[DatabaseQueryTemplateRead]:
        """List a source's templates, hiding disabled ones by default.

        Args:
            db: Database session.
            source_id: Owning database source ID.
            include_inactive: When true, also return disabled templates.

        Returns:
            list[DatabaseQueryTemplateRead]: The read models.

        Raises:
            DatabaseSourceNotFoundError: If the source does not exist.
        """
        source = cls._require_source(db, source_id)
        query = (
            select(DatabaseQueryTemplate)
            .where(DatabaseQueryTemplate.source_id == source_id)
            .order_by(desc(DatabaseQueryTemplate.created_at), desc(DatabaseQueryTemplate.id))
        )
        if not include_inactive:
            query = query.where(DatabaseQueryTemplate.enabled.is_(True))
        rows = db.execute(query).scalars().all()
        return [cls._to_read(row, source) for row in rows]

    @classmethod
    def get_template(cls, db: Session, source_id: str, template_id: str) -> DatabaseQueryTemplateRead:
        """Fetch one template.

        Args:
            db: Database session.
            source_id: Owning database source ID.
            template_id: Query template ID.

        Returns:
            DatabaseQueryTemplateRead: The read model.

        Raises:
            DatabaseSourceNotFoundError: If the source does not exist.
            DatabaseTemplateNotFoundError: If the template does not exist.
        """
        source = cls._require_source(db, source_id)
        return cls._to_read(cls._require_template(db, source_id, template_id), source)

    @classmethod
    def update_template(
        cls,
        db: Session,
        source_id: str,
        template_id: str,
        data: DatabaseQueryTemplateUpdate,
        user_email: Optional[str] = None,
    ) -> DatabaseQueryTemplateRead:
        """Update a template and re-sync its materialized tool.

        The definition is validated against the merged state, so changing only
        the statement is still checked against the existing parameter schema
        and vice versa.

        Args:
            db: Database session (transaction owned by the caller).
            source_id: Owning database source ID.
            template_id: Query template ID.
            data: Update payload.
            user_email: Email of the updating user.

        Returns:
            DatabaseQueryTemplateRead: The updated template.

        Raises:
            DatabaseSourceNotFoundError: If the source does not exist.
            DatabaseTemplateNotFoundError: If the template does not exist.
            DatabaseTemplateNameConflictError: If the new name/slug collides.
            DatabaseTemplateError: If the merged definition is not usable.
        """
        # First-Party
        from mcpgateway.services.database_tool_service import DatabaseToolService  # pylint: disable=import-outside-toplevel

        source = cls._require_source(db, source_id)
        template = cls._require_template(db, source_id, template_id)
        values = data.model_dump(exclude_unset=True)
        previous_name = DatabaseToolService.template_tool_name(source.slug, template.slug)

        if "name" in values and values["name"] != template.name:
            new_slug = slugify(values["name"])
            if cls._conflicting_template(db, source_id, values["name"], new_slug, exclude_id=template_id) is not None:
                raise DatabaseTemplateNameConflictError(values["name"], new_slug)

        statement = values.get("statement", template.statement)
        parameter_schema = values.get("parameter_schema", template.parameter_schema)
        cls._validate_definition(statement, parameter_schema)

        for field, value in values.items():
            setattr(template, field, value)
        if "name" in values:
            template.slug = slugify(template.name)

        template.version += 1
        if user_email is not None:
            template.modified_by = user_email

        db.flush()
        DatabaseToolService.sync_template(db, template, source, previous_name=previous_name)
        db.commit()
        db.refresh(template)
        logger.info("Updated query template %s on source %s", template.name, source.name)
        return cls._to_read(template, source)

    @classmethod
    def delete_template(cls, db: Session, source_id: str, template_id: str) -> None:
        """Delete a template and retire its materialized tool.

        Args:
            db: Database session (transaction owned by the caller).
            source_id: Owning database source ID.
            template_id: Query template ID.

        Raises:
            DatabaseSourceNotFoundError: If the source does not exist.
            DatabaseTemplateNotFoundError: If the template does not exist.
        """
        # First-Party
        from mcpgateway.services.database_tool_service import DatabaseToolService  # pylint: disable=import-outside-toplevel

        source = cls._require_source(db, source_id)
        template = cls._require_template(db, source_id, template_id)
        name = template.name
        DatabaseToolService.dematerialize_template(
            db, DatabaseToolService.template_tool_name(source.slug, template.slug)
        )
        db.delete(template)
        db.commit()
        logger.info("Deleted query template %s from source %s", name, source.name)
