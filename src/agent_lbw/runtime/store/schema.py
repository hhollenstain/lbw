"""Create the directory tables, and fold an older account database into them."""

from __future__ import annotations

import re
import uuid

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.names import slug_for

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS identities (
        id TEXT PRIMARY KEY,
        email TEXT,
        display_name TEXT NOT NULL,
        github_login TEXT,
        password_hash TEXT,
        crossing INTEGER NOT NULL DEFAULT 0,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked INTEGER NOT NULL DEFAULT 0,
        created INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('personal', 'organization')),
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL CHECK (mode IN ('local', 'hosted'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_memberships (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        rank TEXT NOT NULL CHECK (rank IN ('owner', 'admin', 'member')),
        created INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, identity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        name TEXT NOT NULL,
        github_slug TEXT NOT NULL DEFAULT '',
        UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_memberships (
        team_id TEXT NOT NULL REFERENCES teams (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        rank TEXT NOT NULL CHECK (rank IN ('owner', 'admin', 'member')),
        created INTEGER NOT NULL,
        PRIMARY KEY (team_id, identity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS duty_grants (
        id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'team')),
        scope_id TEXT NOT NULL,
        identity_id TEXT NOT NULL REFERENCES identities (id),
        duty TEXT NOT NULL CHECK (duty IN ('billing', 'security', 'rules')),
        UNIQUE (scope_type, scope_id, identity_id, duty)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('github', 'git', 'folder')),
        locator TEXT NOT NULL,
        UNIQUE (tenant_id, locator)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_grants (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects (id),
        subject_type TEXT NOT NULL CHECK (subject_type IN ('team', 'identity')),
        subject_id TEXT NOT NULL,
        access TEXT NOT NULL CHECK (access IN ('use', 'write', 'manage')),
        UNIQUE (project_id, subject_type, subject_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        scope_type TEXT NOT NULL CHECK (
            scope_type IN ('tenant', 'team', 'project', 'identity')
        ),
        scope_id TEXT NOT NULL,
        name TEXT NOT NULL,
        space_name TEXT NOT NULL,
        path TEXT NOT NULL,
        created_by TEXT NOT NULL REFERENCES identities (id),
        created INTEGER NOT NULL,
        UNIQUE (tenant_id, scope_type, scope_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rule_requirements (
        rule_id TEXT NOT NULL REFERENCES rules (id),
        project_id TEXT NOT NULL REFERENCES projects (id),
        PRIMARY KEY (rule_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_grants (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        skill_name TEXT NOT NULL,
        scope_type TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        PRIMARY KEY (tenant_id, skill_name, scope_type, scope_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_repos (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'team', 'project')),
        scope_id TEXT NOT NULL,
        team_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        sha TEXT NOT NULL,
        space_name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        UNIQUE (tenant_id, scope_type, scope_id, team_id, url)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_repo_files (
        repo_id TEXT NOT NULL REFERENCES skill_repos (id),
        path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (repo_id, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_requirements (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        role_name TEXT NOT NULL,
        PRIMARY KEY (tenant_id, role_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_endpoints (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        name TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider IN (
            'openai', 'anthropic', 'google', 'ollama', 'vllm', 'lmstudio',
            'openai_compatible', 'cursor', 'litellm', 'gateway'
        )),
        base_url TEXT NOT NULL DEFAULT '',
        api_key_ciphertext TEXT,
        models_json TEXT NOT NULL DEFAULT '[]',
        auth TEXT NOT NULL DEFAULT 'key',
        audience TEXT NOT NULL DEFAULT '',
        UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS endpoint_credentials (
        endpoint_id TEXT NOT NULL REFERENCES model_endpoints (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        access_ciphertext TEXT NOT NULL,
        refresh_ciphertext TEXT,
        expires_at INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'sso',
        PRIMARY KEY (endpoint_id, identity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        endpoint_id TEXT REFERENCES model_endpoints (id),
        name TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider IN (
            'openai', 'anthropic', 'google', 'ollama', 'vllm', 'lmstudio',
            'openai_compatible', 'cursor', 'litellm', 'gateway'
        )),
        model_name TEXT NOT NULL,
        base_url TEXT NOT NULL DEFAULT '',
        api_key_ciphertext TEXT,
        enabled INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_teams (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        name TEXT NOT NULL,
        job TEXT NOT NULL CHECK (job IN ('plan', 'build', 'review')),
        instruction TEXT NOT NULL DEFAULT '',
        model_id TEXT REFERENCES models (id),
        UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_team_projects (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        team_id TEXT NOT NULL REFERENCES agent_teams (id),
        project_id TEXT NOT NULL REFERENCES projects (id),
        enabled INTEGER NOT NULL,
        PRIMARY KEY (team_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_team_steps (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        team_id TEXT NOT NULL REFERENCES agent_teams (id),
        step_key TEXT NOT NULL,
        job TEXT NOT NULL CHECK (job IN ('plan', 'build', 'review')),
        after_json TEXT NOT NULL DEFAULT '[]',
        paths_json TEXT NOT NULL DEFAULT '[]',
        position INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, step_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_grants (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        model_id TEXT NOT NULL REFERENCES models (id),
        scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'team', 'identity')),
        scope_id TEXT NOT NULL,
        UNIQUE (model_id, scope_type, scope_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_project_settings (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        model_id TEXT NOT NULL REFERENCES models (id),
        project_id TEXT NOT NULL REFERENCES projects (id),
        enabled INTEGER NOT NULL,
        PRIMARY KEY (model_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('lbw', 'claude_code', 'codex', 'cursor')),
        model_id TEXT REFERENCES models (id),
        human_review INTEGER NOT NULL DEFAULT 1 CHECK (human_review = 1),
        enabled INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_grants (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        agent_id TEXT NOT NULL REFERENCES agents (id),
        scope_type TEXT NOT NULL CHECK (scope_type IN ('project', 'team', 'identity')),
        scope_id TEXT NOT NULL,
        UNIQUE (agent_id, scope_type, scope_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS github_apps (
        tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
        app_id TEXT NOT NULL,
        private_key_ciphertext TEXT NOT NULL,
        installation_id TEXT,
        account_login TEXT,
        account_type TEXT,
        client_id TEXT,
        client_secret_ciphertext TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS github_installations (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        installation_id TEXT NOT NULL,
        account_login TEXT NOT NULL DEFAULT '',
        account_type TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, installation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sign_in (
        tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
        provider TEXT NOT NULL CHECK (provider IN ('password', 'entra', 'auth0', 'saml')),
        issuer TEXT NOT NULL DEFAULT '',
        metadata_url TEXT NOT NULL DEFAULT '',
        client_id TEXT NOT NULL DEFAULT '',
        client_secret_ciphertext TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_daily (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        day TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        identity_id TEXT NOT NULL DEFAULT '',
        service TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        step TEXT NOT NULL DEFAULT '',
        purpose TEXT NOT NULL DEFAULT '',
        calls BIGINT NOT NULL DEFAULT 0,
        errors BIGINT NOT NULL DEFAULT 0,
        input_tokens BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        cache_read_tokens BIGINT NOT NULL DEFAULT 0,
        cache_write_tokens BIGINT NOT NULL DEFAULT 0,
        duration_ms BIGINT NOT NULL DEFAULT 0,
        cost_micros BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, day, project_id, identity_id, service, model, step, purpose)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_files_daily (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        day TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        identity_id TEXT NOT NULL DEFAULT '',
        extension TEXT NOT NULL DEFAULT '',
        files BIGINT NOT NULL DEFAULT 0,
        lines_added BIGINT NOT NULL DEFAULT 0,
        lines_removed BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, day, project_id, identity_id, extension)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_skills_daily (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        day TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        identity_id TEXT NOT NULL DEFAULT '',
        skill TEXT NOT NULL DEFAULT '',
        runs BIGINT NOT NULL DEFAULT 0,
        tokens BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, day, project_id, identity_id, skill)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_runs_daily (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        day TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        identity_id TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        runs BIGINT NOT NULL DEFAULT 0,
        duration_ms BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, day, project_id, identity_id, outcome)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_prices (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        input_micros BIGINT NOT NULL DEFAULT 0,
        output_micros BIGINT NOT NULL DEFAULT 0,
        cache_read_micros BIGINT NOT NULL DEFAULT 0,
        cache_write_micros BIGINT NOT NULL DEFAULT 0,
        updated BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, provider, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runners (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '',
        claude_version TEXT NOT NULL DEFAULT '',
        claude_ready INTEGER NOT NULL DEFAULT 0,
        command TEXT NOT NULL DEFAULT '',
        created BIGINT NOT NULL,
        last_seen BIGINT NOT NULL DEFAULT 0,
        revoked BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runner_links (
        device_hash TEXT PRIMARY KEY,
        user_code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        created BIGINT NOT NULL,
        tenant_id TEXT REFERENCES tenants (id),
        identity_id TEXT REFERENCES identities (id),
        approved BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runner_jobs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        runner_id TEXT NOT NULL REFERENCES runners (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        task_id TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
        created BIGINT NOT NULL,
        leased_until BIGINT NOT NULL DEFAULT 0,
        started BIGINT,
        finished BIGINT,
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_models (
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        identity_id TEXT NOT NULL REFERENCES identities (id),
        allowed INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 0,
        provider TEXT NOT NULL DEFAULT 'ollama',
        base_url TEXT NOT NULL DEFAULT '',
        models_json TEXT NOT NULL DEFAULT '[]',
        synced BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, identity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_keys (
        tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
        wrapped_key TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        project_id TEXT NOT NULL REFERENCES projects (id),
        requester_id TEXT NOT NULL REFERENCES identities (id),
        status TEXT NOT NULL,
        branch TEXT,
        pr_url TEXT,
        run_ref TEXT,
        goal_ciphertext TEXT,
        agent_id TEXT,
        model_id TEXT,
        reviewer_id TEXT,
        fault TEXT,
        result_ciphertext TEXT,
        created INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        actor_id TEXT REFERENCES identities (id),
        at INTEGER NOT NULL,
        action TEXT NOT NULL,
        method TEXT,
        path TEXT,
        target_type TEXT,
        target_id TEXT,
        summary TEXT NOT NULL,
        detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        project_id TEXT NOT NULL REFERENCES projects (id),
        kind TEXT NOT NULL CHECK (kind IN ('change', 'scan')),
        cadence TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        created_by TEXT NOT NULL REFERENCES identities (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS install (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        tenant_id TEXT NOT NULL REFERENCES tenants (id),
        current_identity_id TEXT NOT NULL REFERENCES identities (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        identity_id TEXT NOT NULL REFERENCES identities (id),
        tenant_id TEXT,
        expires INTEGER NOT NULL
    )
    """,
)

# Child rows go before the identity and tenant they point at.
CLEAR_ORDER = (
    "audit_log",
    "runner_jobs",
    "runner_links",
    "local_models",
    "runners",
    "usage_daily",
    "usage_files_daily",
    "usage_skills_daily",
    "usage_runs_daily",
    "model_prices",
    "agent_grants",
    "model_project_settings",
    "model_grants",
    "agents",
    "models",
    "endpoint_credentials",
    "model_endpoints",
    "github_installations",
    "github_apps",
    "skill_repo_files",
    "skill_repos",
    "skill_grants",
    "agent_team_steps",
    "agent_team_projects",
    "agent_teams",
    "role_requirements",
    "rule_requirements",
    "rules",
    "schedules",
    "tasks",
    "project_grants",
    "projects",
    "duty_grants",
    "team_memberships",
    "teams",
    "tenant_memberships",
    "sign_in",
    "tenant_keys",
    "sessions",
    "install",
    "identities",
    "tenants",
)

TASK_COLUMNS = frozenset(
    {
        "id",
        "tenant_id",
        "project_id",
        "requester_id",
        "status",
        "branch",
        "pr_url",
        "run_ref",
        "goal_ciphertext",
        "agent_id",
        "model_id",
        "reviewer_id",
        "fault",
        "result_ciphertext",
        "agent_kind",
        "created",
    }
)


SCHEMA_VERSION = 13


def read_schema_version(db: Database) -> int:
    """The saved schema version, or 0 when this database has not been marked."""
    if not _has_table(db, "schema_meta"):
        return 0
    row = db.fetchone("SELECT version FROM schema_meta WHERE id = 1")
    if row is None or row["version"] is None:
        return 0
    return int(row["version"])


def write_schema_version(db: Database) -> None:
    """Remember that the current schema has been applied."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO schema_meta (id, version) VALUES (1, ?)
        ON CONFLICT (id) DO UPDATE SET version = excluded.version
        """,
        (SCHEMA_VERSION,),
    )


def apply_schema(db: Database) -> None:
    """Create tables. An older organizations/users database is copied in once."""
    legacy = None
    if _has_table(db, "organizations") and not _has_table(db, "tenants"):
        legacy = _legacy_account(db)
    if legacy is not None:
        for name in ("install", "users", "organizations"):
            db.execute(f"DROP TABLE {name}")
    for statement in _STATEMENTS:
        db.execute(statement)
    _add_column(db, "identities", "github_login", "TEXT")
    _add_column(db, "identities", "password_hash", "TEXT")
    _add_column(db, "identities", "crossing", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "identities", "failed_attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "identities", "locked", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "github_apps", "account_type", "TEXT")
    _add_column(db, "github_apps", "client_id", "TEXT")
    _add_column(db, "github_apps", "client_secret_ciphertext", "TEXT")
    _add_column(db, "tasks", "agent_id", "TEXT")
    _add_column(db, "tasks", "model_id", "TEXT")
    _add_column(db, "tasks", "reviewer_id", "TEXT")
    _add_column(db, "tasks", "fault", "TEXT")
    _add_column(db, "tasks", "result_ciphertext", "TEXT")
    _add_column(db, "tasks", "agent_kind", "TEXT")
    _add_column(db, "sessions", "tenant_id", "TEXT")
    _add_column(db, "models", "endpoint_id", "TEXT")
    _add_column(db, "models", "enabled", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "skill_repos", "enabled", "INTEGER NOT NULL DEFAULT 1")
    _add_column(db, "skill_repo_files", "enabled", "INTEGER NOT NULL DEFAULT 1")
    _add_column(db, "agents", "enabled", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "teams", "github_slug", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "projects", "provider_id", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "projects", "enabled", "INTEGER NOT NULL DEFAULT 1")
    _add_column(db, "projects", "archived_at", "INTEGER")
    _add_column(db, "model_endpoints", "auth", "TEXT NOT NULL DEFAULT 'key'")
    _add_column(db, "model_endpoints", "audience", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "endpoint_credentials", "kind", "TEXT NOT NULL DEFAULT 'sso'")
    _add_column(db, "tenants", "local_models", "TEXT NOT NULL DEFAULT 'off'")
    _add_column(db, "runners", "llm_relay", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "tenants", "spec_required", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "projects", "spec_required", "INTEGER NOT NULL DEFAULT 0")
    for table in ("usage_daily", "usage_files_daily", "usage_skills_daily", "usage_runs_daily"):
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_project ON {table} (tenant_id, project_id, day)"
        )
        db.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_person ON {table} (tenant_id, identity_id, day)"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS runner_jobs_queue ON runner_jobs (runner_id, status, created)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS runners_person ON runners (tenant_id, identity_id)")
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS projects_provider
        ON projects (tenant_id, provider_id)
        WHERE provider_id <> ''
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS teams_github_slug
        ON teams (tenant_id, github_slug)
        WHERE github_slug <> ''
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS identities_github_login
        ON identities (github_login)
        WHERE github_login IS NOT NULL
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS identities_email
        ON identities (email)
        WHERE email IS NOT NULL
        """
    )
    _widen_duty_check(db)
    _widen_model_provider(db)
    _seed_team_steps(db)
    _widen_project_access(db)
    if legacy is not None:
        _write_legacy(db, legacy)
    _mark_crossing_owner(db)
    _drop_claude_link(db)


def clear_store(db: Database) -> None:
    """Remove directory rows. Used when a shared Postgres database is reset."""
    for name in CLEAR_ORDER:
        if _has_table(db, name):
            db.execute(f"DELETE FROM {name}")


def table_columns(db: Database, table: str) -> set[str]:
    if db.kind == "sqlite":
        rows = db.fetchall(f"PRAGMA table_info({table})")
        return {str(row["name"]) for row in rows}
    rows = db.fetchall(
        """
        SELECT column_name AS name
        FROM information_schema.columns
        WHERE table_name = ?
        """,
        (table,),
    )
    return {str(row["name"]) for row in rows}


def _mark_crossing_owner(db: Database) -> None:
    """The install owner is the first LBW engineer when none is marked yet."""
    if not _has_table(db, "identities"):
        return
    marked = db.fetchone("SELECT id FROM identities WHERE crossing = 1 LIMIT 1")
    if marked is not None:
        return
    db.execute(
        """
        UPDATE identities SET crossing = 1
        WHERE id IN (
            SELECT identity_id FROM tenant_memberships WHERE rank = 'owner'
        )
        """
    )


def _seed_team_steps(db: Database) -> None:
    """Give each team made before steps existed one step for the job it had."""
    rows = db.fetchall(
        """
        SELECT id, tenant_id, name, job FROM agent_teams
        WHERE NOT EXISTS (
            SELECT 1 FROM agent_team_steps WHERE agent_team_steps.team_id = agent_teams.id
        )
        ORDER BY tenant_id, name, id
        """
    )
    for row in rows:
        taken = {
            str(item["step_key"])
            for item in db.fetchall(
                "SELECT step_key FROM agent_team_steps WHERE tenant_id = ?",
                (row["tenant_id"],),
            )
        }
        base = re.sub(r"[^a-z0-9]+", "-", str(row["name"]).lower()).strip("-")[:60] or "team"
        key = base
        number = 2
        while key in taken:
            key = f"{base}-{number}"
            number += 1
        db.execute(
            """
            INSERT INTO agent_team_steps (id, tenant_id, team_id, step_key, job, position)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (f"ats_{uuid.uuid4().hex[:12]}", row["tenant_id"], row["id"], key, row["job"]),
        )


def _add_column(db: Database, table: str, column: str, ddl: str) -> None:
    if column not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _drop_claude_link(db: Database) -> None:
    """Claude runs on each person's runner now, so the Claude Platform OAuth link is gone."""
    db.execute("DELETE FROM endpoint_credentials WHERE kind = 'claude_link'")
    for name in ("link_pending", "claude_profiles", "claude_link_apps"):
        if _has_table(db, name):
            db.execute(f"DROP TABLE {name}")


def _has_table(db: Database, name: str) -> bool:
    if db.kind == "sqlite":
        row = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        )
    else:
        row = db.fetchone(
            "SELECT tablename AS name FROM pg_tables WHERE schemaname = 'public' AND tablename = ?",
            (name,),
        )
    return row is not None


def _legacy_account(db: Database) -> dict | None:
    if not _has_table(db, "users") or not _has_table(db, "install"):
        return None
    install = db.fetchone("SELECT org_id, current_user_id FROM install WHERE id = 1")
    orgs = db.fetchall("SELECT id, name, mode FROM organizations")
    users = db.fetchall("SELECT id, org_id, name, role, created FROM users")
    if install is None or not orgs or not users:
        return None
    return {"install": install, "orgs": orgs, "users": users}


def _write_legacy(db: Database, legacy: dict) -> None:
    for org in legacy["orgs"]:
        slug = slug_for(str(org["name"]), str(org["id"]))
        mode = org["mode"] if org["mode"] in {"local", "hosted"} else "local"
        db.execute(
            "INSERT INTO tenants (id, kind, name, slug, mode) VALUES (?, 'organization', ?, ?, ?)",
            (org["id"], org["name"], slug, mode),
        )
    for user in legacy["users"]:
        rank = user["role"] if user["role"] in {"owner", "admin", "member"} else "member"
        db.execute(
            "INSERT INTO identities (id, email, display_name, created) VALUES (?, NULL, ?, ?)",
            (user["id"], user["name"], user["created"]),
        )
        db.execute(
            """
            INSERT INTO tenant_memberships (tenant_id, identity_id, rank, created)
            VALUES (?, ?, ?, ?)
            """,
            (user["org_id"], user["id"], rank, user["created"]),
        )
    install = legacy["install"]
    db.execute(
        "INSERT INTO install (id, tenant_id, current_identity_id) VALUES (1, ?, ?)",
        (install["org_id"], install["current_user_id"]),
    )


def _duty_check_has_rules(db: Database) -> bool:
    if db.kind == "sqlite":
        row = db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'duty_grants'"
        )
        return row is not None and "rules" in str(row["sql"] or "")
    row = db.fetchone(
        """
        SELECT pg_get_constraintdef(oid) AS sql
        FROM pg_constraint
        WHERE conrelid = 'duty_grants'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%duty%'
        """
    )
    return row is not None and "rules" in str(row["sql"])


def _provider_check_has_gateway(db: Database, table: str) -> bool:
    if db.kind == "sqlite":
        row = db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return row is not None and "litellm" in str(row["sql"] or "")
    row = db.fetchone(
        """
        SELECT pg_get_constraintdef(oid) AS sql
        FROM pg_constraint
        WHERE conrelid = ?::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%provider%'
        """,
        (table,),
    )
    return row is not None and "litellm" in str(row["sql"])


def _create_sql(table: str) -> str:
    needle = f"CREATE TABLE IF NOT EXISTS {table} "
    for statement in _STATEMENTS:
        if needle in statement:
            return statement
    raise KeyError(table)


def _widen_model_provider(db: Database) -> None:
    """Let an existing model table store a LiteLLM or other gateway endpoint."""
    for table in ("model_endpoints", "models"):
        if not _has_table(db, table) or _provider_check_has_gateway(db, table):
            continue
        if db.kind == "sqlite":
            _rebuild_provider_table(db, table)
            continue
        rows = db.fetchall(
            """
            SELECT conname AS name
            FROM pg_constraint
            WHERE conrelid = ?::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%provider%'
              AND pg_get_constraintdef(oid) NOT LIKE '%litellm%'
            """,
            (table,),
        )
        for row in rows:
            name = str(row["name"]).replace('"', "")
            db.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        db.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {table}_provider_check
            CHECK (provider IN (
                'openai', 'anthropic', 'google', 'ollama', 'vllm', 'lmstudio',
                'openai_compatible', 'cursor', 'litellm', 'gateway'
            ))
            """
        )


def _rebuild_provider_table(db: Database, table: str) -> None:
    previous = f"{table}_provider_prev"
    kept = table_columns(db, table)
    db.raw.execute("PRAGMA foreign_keys = OFF")
    db.execute(f"ALTER TABLE {table} RENAME TO {previous}")
    db.execute(_create_sql(table))
    shared = [name for name in sorted(kept) if name in table_columns(db, table)]
    joined = ", ".join(shared)
    db.execute(f"INSERT INTO {table} ({joined}) SELECT {joined} FROM {previous}")
    db.execute(f"DROP TABLE {previous}")
    db.raw.execute("PRAGMA foreign_keys = ON")


def _access_check_has_write(db: Database) -> bool:
    if db.kind == "sqlite":
        row = db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_grants'"
        )
        return row is not None and "write" in str(row["sql"] or "")
    row = db.fetchone(
        """
        SELECT pg_get_constraintdef(oid) AS sql
        FROM pg_constraint
        WHERE conrelid = 'project_grants'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%access%'
          AND pg_get_constraintdef(oid) LIKE '%write%'
        """
    )
    return row is not None


def _widen_project_access(db: Database) -> None:
    """Let an existing grant store write access. Manage still includes it."""
    if not _has_table(db, "project_grants") or _access_check_has_write(db):
        return
    if db.kind == "sqlite":
        db.raw.execute("PRAGMA foreign_keys = OFF")
        db.execute("ALTER TABLE project_grants RENAME TO project_grants_prev")
        db.execute(_create_sql("project_grants"))
        db.execute(
            """
            INSERT INTO project_grants (id, project_id, subject_type, subject_id, access)
            SELECT id, project_id, subject_type, subject_id, access FROM project_grants_prev
            """
        )
        db.execute("DROP TABLE project_grants_prev")
        db.raw.execute("PRAGMA foreign_keys = ON")
        return
    rows = db.fetchall(
        """
        SELECT conname AS name
        FROM pg_constraint
        WHERE conrelid = 'project_grants'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%access%'
          AND pg_get_constraintdef(oid) NOT LIKE '%write%'
        """
    )
    for row in rows:
        name = str(row["name"]).replace('"', "")
        db.execute(f'ALTER TABLE project_grants DROP CONSTRAINT "{name}"')
    db.execute(
        """
        ALTER TABLE project_grants
        ADD CONSTRAINT project_grants_access_check
        CHECK (access IN ('use', 'write', 'manage'))
        """
    )


def _widen_duty_check(db: Database) -> None:
    """Let an existing duty table accept the rules duty."""
    if not _has_table(db, "duty_grants") or _duty_check_has_rules(db):
        return
    if db.kind == "sqlite":
        db.execute("ALTER TABLE duty_grants RENAME TO duty_grants_prev")
        db.execute(
            """
            CREATE TABLE duty_grants (
                id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'team')),
                scope_id TEXT NOT NULL,
                identity_id TEXT NOT NULL REFERENCES identities (id),
                duty TEXT NOT NULL CHECK (duty IN ('billing', 'security', 'rules')),
                UNIQUE (scope_type, scope_id, identity_id, duty)
            )
            """
        )
        db.execute(
            """
            INSERT INTO duty_grants (id, scope_type, scope_id, identity_id, duty)
            SELECT id, scope_type, scope_id, identity_id, duty FROM duty_grants_prev
            """
        )
        db.execute("DROP TABLE duty_grants_prev")
        return
    rows = db.fetchall(
        """
        SELECT conname AS name
        FROM pg_constraint
        WHERE conrelid = 'duty_grants'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%duty%'
          AND pg_get_constraintdef(oid) NOT LIKE '%rules%'
        """
    )
    for row in rows:
        name = str(row["name"]).replace('"', "")
        db.execute(f'ALTER TABLE duty_grants DROP CONSTRAINT "{name}"')
    db.execute(
        """
        ALTER TABLE duty_grants
        ADD CONSTRAINT duty_grants_duty_check
        CHECK (duty IN ('billing', 'security', 'rules'))
        """
    )
