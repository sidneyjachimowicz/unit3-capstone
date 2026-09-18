"""
SQL Validation Layer

Per capstone spec: no Bedrock-generated SQL query may execute against Redshift
without passing through this validator first. Blocks any non-SELECT statement
and any statement containing a blocked keyword (including stacked queries like
"SELECT ...; DROP TABLE ...;").

NOTE: The capstone spec's originally-provided validator used naive space-padded
substring matching (f" {keyword} " in f" {query_upper} "), which fails to catch
a blocked keyword immediately adjacent to punctuation rather than whitespace
(e.g. "(ALTER TABLE ...)" -- no space before ALTER). Discovered via this test
suite (see the "Keyword hidden mid-query" case) and fixed below using regex
word-boundary matching, which correctly treats punctuation as a boundary.
"""

import re

BLOCKED_KEYWORDS = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]


def validate_sql(query: str) -> dict:
    query_upper = query.upper().strip()
    for keyword in BLOCKED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", query_upper):
            return {"valid": False, "reason": f"Blocked: {keyword} not permitted"}
    if not query_upper.startswith("SELECT"):
        return {"valid": False, "reason": "Only SELECT queries are permitted"}
    return {"valid": True, "reason": "OK"}


# --- Test suite: 5+ cases including a stacked-query attempt, run before wiring
#     the validator into any live Redshift connection ---

TEST_CASES = [
    {
        "name": "Valid simple SELECT",
        "query": "SELECT customer_name, monthly_revenue FROM customer_data;",
        "expect_valid": True,
    },
    {
        "name": "Valid SELECT with WHERE and JOIN",
        "query": "SELECT c.customer_name FROM customer_data c JOIN plans p ON c.plan_tier = p.tier WHERE p.active = true;",
        "expect_valid": True,
    },
    {
        "name": "Stacked query attempt (SELECT + DROP TABLE)",
        "query": "SELECT * FROM customer_data; DROP TABLE customer_data;",
        "expect_valid": False,
    },
    {
        "name": "Direct DELETE statement",
        "query": "DELETE FROM customer_data WHERE customer_id = '1001';",
        "expect_valid": False,
    },
    {
        "name": "Direct UPDATE statement",
        "query": "UPDATE customer_data SET monthly_revenue = 0;",
        "expect_valid": False,
    },
    {
        "name": "INSERT statement",
        "query": "INSERT INTO customer_data VALUES ('9999', 'Fake Corp', '2026-01-01', 'Enterprise', 999.99);",
        "expect_valid": False,
    },
    {
        "name": "Non-SELECT read attempt (SHOW TABLES-style)",
        "query": "SHOW TABLES;",
        "expect_valid": False,
    },
    {
        "name": "Keyword hidden mid-query (ALTER inside subquery)",
        "query": "SELECT * FROM (ALTER TABLE customer_data ADD COLUMN x INT) AS sub;",
        "expect_valid": False,
    },
    {
        "name": "Valid SELECT with aggregate and GROUP BY",
        "query": "SELECT plan_tier, COUNT(*), AVG(monthly_revenue) FROM customer_data GROUP BY plan_tier;",
        "expect_valid": True,
    },
]


def run_tests():
    passed = 0
    failed = 0
    for case in TEST_CASES:
        result = validate_sql(case["query"])
        success = result["valid"] == case["expect_valid"]
        status = "PASS" if success else "FAIL"
        if success:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {case['name']}")
        print(f"       Query: {case['query']}")
        print(f"       Expected valid={case['expect_valid']}, got valid={result['valid']} ({result['reason']})")
        print()

    print(f"--- {passed}/{len(TEST_CASES)} tests passed, {failed} failed ---")
    return failed == 0


if __name__ == "__main__":
    all_passed = run_tests()
    exit(0 if all_passed else 1)
