"""Run the service when the repository directory itself is executed.

From the repository root:

    python -m service
    python run.py
    python .            # this file
"""
try:  # package-style launch: python -m <repo_parent>.<repo_name>
    from .service.main import main
except ImportError:  # directory-style launch: python .
    from service.main import main

if __name__ == "__main__":
    raise SystemExit(main())
