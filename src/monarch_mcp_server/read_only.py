"""Opt in read only mode.

Set ``MONARCH_MCP_READ_ONLY`` to a truthy value and the mutating tools are
never registered, so they do not appear in the tool list and cannot be called
at all. This is stronger than relying on client side approval prompts: a tool
that is not registered cannot be invoked by a model that was talked into it by
a merchant name or memo it read back, which is the threat the README's
approval section is about.

Read only is off by default. Enabling it is a deliberate choice, so existing
setups keep working exactly as before.

``MONARCH_MCP_ALLOWED_MUTATIONS`` punches a narrow, explicit hole in that
gate: a comma-separated list of exact tool names from ``MUTATING_TOOLS`` that
should register anyway, for a deployment that wants one or two specific
writes (e.g. re-categorizing a transaction) without dropping to full
read/write. Unrecognised names and anything in ``PERMANENTLY_EXCLUDED`` are
ignored rather than erroring, so a typo or an attempt to allowlist a
permanently-excluded tool fails closed (the tool simply stays unregistered)
instead of crashing the server.
"""

import logging
import os
from typing import Any, Callable, FrozenSet, TypeVar

logger = logging.getLogger(__name__)

ENV_VAR = "MONARCH_MCP_READ_ONLY"
ALLOWLIST_ENV_VAR = "MONARCH_MCP_ALLOWED_MUTATIONS"

_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})

# Tools that stay unregistered even if named in MONARCH_MCP_ALLOWED_MUTATIONS.
# delete_transaction has no visible undo in the Monarch UI/API -- see
# docs/architecture/mcp-monarch.md's "exclude ... permanently regardless of
# confirm-gating" note. This is a second, independent check, not just a
# matter of leaving it off the allowlist: it holds even if a future config
# change lists it by mistake.
PERMANENTLY_EXCLUDED: FrozenSet[str] = frozenset({"delete_transaction"})

# Every registered tool that writes. Listed explicitly rather than matched by
# name prefix: this is a security control, and a tool silently failing to be
# recognised as mutating would defeat the whole point.
MUTATING_TOOLS: FrozenSet[str] = frozenset(
    {
        # Transactions
        "create_transaction",
        "update_transaction",
        "delete_transaction",
        "categorize_transaction",
        "update_transaction_notes",
        "mark_transaction_reviewed",
        "bulk_categorize_transactions",
        "split_transaction",
        "upload_account_balance_history",
        # Tags
        "set_transaction_tags",
        "add_transaction_tag",
        "create_transaction_tag",
        # Rules
        "create_transaction_rule",
        "update_transaction_rule",
        "delete_transaction_rule",
        "reorder_transaction_rule",
        # Categories and budgets
        "create_transaction_category",
        "update_category",
        "set_budget_amount",
        # Goals
        "update_savings_goal",
        "set_goal_contribution",
        # Merchants
        "update_merchant",
        "review_recurring_stream",
        # Session mutation. Logging out or replacing the stored session is a
        # change to durable state, and a read only deployment should not be
        # able to do it either.
        "monarch_login",
        "monarch_login_with_token",
        "monarch_logout",
        # Accounts
        "update_account",
        # Side effecting: posts a refresh request to the institutions.
        "refresh_accounts",
    }
)

F = TypeVar("F", bound=Callable[..., Any])


def is_read_only() -> bool:
    """Whether read only mode is enabled for this process."""
    return os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY


def allowed_mutations() -> FrozenSet[str]:
    """Mutating tools explicitly allowlisted through read only mode.

    Reads ``MONARCH_MCP_ALLOWED_MUTATIONS``, a comma-separated list of tool
    names. Anything not in ``MUTATING_TOOLS``, or in ``PERMANENTLY_EXCLUDED``,
    is dropped with a warning rather than allowed through or raised -- a
    misconfiguration here should narrow what gets exposed, never widen it.
    """
    raw = os.environ.get(ALLOWLIST_ENV_VAR, "")
    named = {name.strip() for name in raw.split(",") if name.strip()}

    unknown = named - MUTATING_TOOLS
    if unknown:
        logger.warning(
            "%s names tools that are not recognised mutating tools, "
            "ignoring: %s",
            ALLOWLIST_ENV_VAR,
            sorted(unknown),
        )

    excluded = named & PERMANENTLY_EXCLUDED
    if excluded:
        logger.warning(
            "%s lists permanently excluded tools, ignoring: %s",
            ALLOWLIST_ENV_VAR,
            sorted(excluded),
        )

    return frozenset(named & MUTATING_TOOLS - PERMANENTLY_EXCLUDED)


def install(mcp: Any) -> None:
    """Make ``mcp.tool()`` skip mutating tools while read only is enabled.

    Wrapping registration is what keeps this change small: every tool module
    already registers through ``@mcp.tool()``, so nothing else has to know
    about read only mode, and the decorated function is still returned so the
    re-exports in ``server.py`` keep working.
    """
    if not is_read_only():
        return

    allowed = allowed_mutations()
    original_tool = mcp.tool

    def guarded_tool(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        register = original_tool(*args, **kwargs)

        def decorator(fn: F) -> F:
            # FastMCP.tool() takes `name` as its first positional parameter, so
            # @mcp.tool("some_name") must be honoured too. Comparing only
            # fn.__name__ would let a renamed mutating tool through the gate.
            positional = args[0] if args and isinstance(args[0], str) else None
            name = positional or kwargs.get("name") or getattr(fn, "__name__", "")
            if name in MUTATING_TOOLS:
                if name in allowed:
                    logger.warning(
                        "Read only mode: %s is explicitly allowlisted via %s, "
                        "registering it anyway",
                        name,
                        ALLOWLIST_ENV_VAR,
                    )
                    return register(fn)
                logger.info("Read only mode: not registering %s", name)
                return fn
            return register(fn)

        return decorator

    mcp.tool = guarded_tool  # type: ignore[method-assign]
    logger.warning(
        "%s is set: %d mutating tools will not be registered%s",
        ENV_VAR,
        len(MUTATING_TOOLS) - len(allowed),
        f" ({sorted(allowed)} allowlisted via {ALLOWLIST_ENV_VAR})"
        if allowed
        else "",
    )
