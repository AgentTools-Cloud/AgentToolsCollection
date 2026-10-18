"""Run audited owner-lifecycle regressions without a production database."""

import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest


MODULES = (
    "test_owner_lifecycle", "test_owner_declarations", "test_ingestion_pipeline",
    "test_ingestion_catalog", "test_ingestion_privacy_alias", "test_source_endpoint_lifecycle",
    "test_internal_notices", "test_outbound_mail_notice", "test_service_cards",
    "test_agent_card_ingestion", "test_agent_card_selection", "test_wellknown_reconciliation",
    "test_category_browse",
)


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="owner-lifecycle-checks-") as temporary:
        os.environ["AGENT_TOOLS_DB_PATH"] = str(Path(temporary) / "unused-default.db")
        os.environ["AGENT_TOOLS_SOURCE_DB"] = str(Path(temporary) / "never-production.db")
        from directory import db
        if db.DEFAULT_DB_PATH != os.environ["AGENT_TOOLS_DB_PATH"]:
            raise RuntimeError("test database path was imported before isolation")
        suite = unittest.TestLoader().loadTestsFromNames(MODULES)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())