"""ytbrain - YouTube corpus -> grounded structured knowledge for a founder-coach agent."""
__version__ = "0.1.0"


def _find_runtime() -> None:
    """ytbrain imports the shared search from the sibling `founder_coach` package. An
    editable install made before that package existed doesn't map it, which would break
    every command until `uv pip install -e .` is re-run; fall back to the checkout."""
    try:
        import founder_coach  # noqa: F401
    except ImportError:
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        if (root / "founder_coach" / "__init__.py").exists():
            sys.path.append(str(root))


_find_runtime()
