"""
AI Query Layer for the Intelligent Document Search Pipeline.

Combines:
  - Query routing (OpenSearch vs Redshift vs both)
  - Natural-language-to-SQL generation (validated before execution)
  - Contextual document response generation (OpenSearch retrieval + Claude)
  - Combined synthesis for queries needing both sources
  - Tokenomics tracking across all Bedrock calls

NOTE: Live invocation of Bedrock is currently blocked in this sandbox account
with AccessDeniedException on aws-marketplace:Subscribe / ViewSubscriptions
(see README Adaptations section). This code is written and structured to be
correct and ready to run the moment that access is restored; it has not yet
been exercised against a live model due to that blocker.
"""

import json
import re
import boto3
import psycopg2
import requests
from requests.auth import HTTPBasicAuth

from test_sql_validator import validate_sql

# --- Configuration ---
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
_account_id = boto3.client("sts").get_caller_identity()["Account"]
MODEL_ID = f"arn:aws:bedrock:us-east-1:{_account_id}:inference-profile/us.anthropic.claude-sonnet-4-6"

REDSHIFT_HOST = "capstone-docsearch-wg.567781376107.us-east-1.redshift-serverless.amazonaws.com"
REDSHIFT_PORT = 5439
REDSHIFT_DB = "dev"
REDSHIFT_USER = "capstoneadmin"
REDSHIFT_PASSWORD = "TrainingAWS123"

OPENSEARCH_HOST = "search-capstone-docsearch-2vwjeiie2vr2ojl7bgtckkumua.us-east-1.es.amazonaws.com"
OPENSEARCH_USER = "capstoneadmin"
OPENSEARCH_PASSWORD = "TrainingAWS123#"
OPENSEARCH_INDEX = "capstone-documents"

REDSHIFT_SCHEMA_DESCRIPTION = """
Table: public.customer_data
Columns:
  customer_id (varchar) - unique customer identifier
  customer_name (varchar) - customer/company name
  signup_date (varchar) - date customer signed up, format YYYY-MM-DD
  plan_tier (varchar) - subscription tier, e.g. 'Standard' or 'Enterprise'
  monthly_revenue (decimal) - monthly revenue in USD
"""


# --- Tokenomics tracking ---
class TokenTracker:
    """Logs input/output tokens per Bedrock call type across a session."""

    # Illustrative placeholder rate -- check current Bedrock pricing page for
    # exact figures before using this for real cost reporting.
    COST_PER_1K_INPUT_TOKENS = 0.003
    COST_PER_1K_OUTPUT_TOKENS = 0.015

    def __init__(self):
        self.calls = []

    def log(self, call_type: str, input_tokens: int, output_tokens: int):
        self.calls.append({
            "call_type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    def summary(self) -> dict:
        total_input = sum(c["input_tokens"] for c in self.calls)
        total_output = sum(c["output_tokens"] for c in self.calls)
        by_type = {}
        for c in self.calls:
            t = c["call_type"]
            by_type.setdefault(t, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            by_type[t]["calls"] += 1
            by_type[t]["input_tokens"] += c["input_tokens"]
            by_type[t]["output_tokens"] += c["output_tokens"]

        estimated_cost = (
            (total_input / 1000) * self.COST_PER_1K_INPUT_TOKENS
            + (total_output / 1000) * self.COST_PER_1K_OUTPUT_TOKENS
        )

        return {
            "total_calls": len(self.calls),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "by_call_type": by_type,
            "estimated_cost_usd": round(estimated_cost, 6),
            "cost_note": "Illustrative estimate using placeholder per-1K-token rates; verify against actual Bedrock pricing.",
        }

    def print_summary(self):
        s = self.summary()
        print("\n=== Tokenomics Summary ===")
        print(f"Total calls: {s['total_calls']}")
        print(f"Total input tokens: {s['total_input_tokens']}")
        print(f"Total output tokens: {s['total_output_tokens']}")
        print("By call type:")
        for t, stats in s["by_call_type"].items():
            print(f"  {t}: {stats['calls']} calls, {stats['input_tokens']} in / {stats['output_tokens']} out")
        print(f"Estimated cost: ${s['estimated_cost_usd']} ({s['cost_note']})")


tracker = TokenTracker()


def invoke_claude(prompt: str, max_tokens: int = 500, call_type: str = "unspecified") -> dict:
    """Calls Bedrock. If Bedrock access is blocked (AccessDeniedException, seen
    in this sandbox as an AWS Marketplace subscription issue outside our IAM
    control), falls back to a deterministic stub so the rest of the pipeline
    (routing -> validated SQL execution -> OpenSearch retrieval -> combined
    answer) can still be exercised end-to-end with real infrastructure calls.
    This fallback does NOT pretend to be a real language model; it exists so
    the non-Bedrock parts of Bronze can be demonstrated with real output while
    Bedrock access is unavailable. See README Adaptations for details."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
        result = json.loads(response["body"].read())
        input_tokens = result["usage"]["input_tokens"]
        output_tokens = result["usage"]["output_tokens"]
        tracker.log(call_type, input_tokens, output_tokens)
        return {
            "text": result["content"][0]["text"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "used_fallback": False,
        }
    except Exception as e:
        if "AccessDeniedException" not in str(type(e)) and "AccessDenied" not in str(e):
            raise
        tracker.log(f"{call_type}_FALLBACK", 0, 0)
        return {
            "text": _deterministic_fallback(prompt, call_type),
            "input_tokens": 0,
            "output_tokens": 0,
            "used_fallback": True,
        }


def _deterministic_fallback(prompt: str, call_type: str) -> str:
    """Rule-based stand-in used only when Bedrock is unavailable. Not a
    language model -- deliberately simple, keyword-driven logic so the
    surrounding pipeline (validation, execution, retrieval) can still be
    tested with real output."""
    prompt_lower = prompt.lower()

    if call_type == "routing":
        structured_terms = ["revenue", "churn rate", "how many", "count", "average", "total", "customers are"]
        doc_terms = ["document", "policy", "strategy says", "according to", "textract", "pipeline"]
        has_structured = any(t in prompt_lower for t in structured_terms)
        has_doc = any(t in prompt_lower for t in doc_terms)
        if has_structured and has_doc:
            route = "both"
        elif has_structured:
            route = "redshift"
        elif has_doc:
            route = "opensearch"
        else:
            route = "both"
        return json.dumps({"route": route, "reasoning": "Fallback: keyword-based routing (Bedrock unavailable)"})

    if call_type == "sql_generation":
        # Minimal template matching against the one known table/schema.
        if "total" in prompt_lower and "revenue" in prompt_lower:
            return "SELECT SUM(monthly_revenue) AS total_revenue FROM customer_data;"
        if "average" in prompt_lower and "plan" in prompt_lower:
            return "SELECT plan_tier, AVG(monthly_revenue) AS avg_revenue FROM customer_data GROUP BY plan_tier;"
        if "enterprise" in prompt_lower:
            return "SELECT customer_name FROM customer_data WHERE plan_tier = 'Enterprise';"
        if "above" in prompt_lower or "greater" in prompt_lower:
            return "SELECT customer_name, monthly_revenue FROM customer_data WHERE monthly_revenue > 2000;"
        if "2024" in prompt_lower:
            return "SELECT customer_name FROM customer_data WHERE signup_date LIKE '2024%';"
        return "SELECT * FROM customer_data;"

    if call_type in ("contextual_response", "synthesis"):
        return ("[FALLBACK: Bedrock unavailable in this sandbox. Real retrieval/SQL "
                "results above are genuine; this text would normally be a Claude-"
                "generated synthesis of those results. See README Adaptations.]")

    return "[FALLBACK: Bedrock unavailable]"


# --- 1. Routing ---
ROUTER_PROMPT = """You are a query router for a hybrid document + data warehouse system.

Given a user's question, decide which data source(s) are needed:
- "opensearch" — needs semantic search over unstructured documents
- "redshift" — needs structured/numeric data from the SQL warehouse
- "both" — needs structured data AND document content combined

Respond with ONLY a JSON object: {{"route": "opensearch" | "redshift" | "both", "reasoning": "<one sentence>"}}

Question: {question}
"""


def route_query(question: str) -> dict:
    result = invoke_claude(ROUTER_PROMPT.format(question=question), max_tokens=150, call_type="routing")
    try:
        parsed = json.loads(result["text"].strip())
    except json.JSONDecodeError:
        parsed = {"route": "both", "reasoning": "Fallback: could not parse router response"}
    return parsed


# --- 2. NL-to-SQL generation + validated execution ---
SQL_GEN_PROMPT = """You are a SQL generation assistant for a Redshift data warehouse.

Schema:
{schema}

Generate a single SELECT statement (no other statement types) that answers this question.
Respond with ONLY the SQL query, no explanation, no markdown formatting.

Question: {question}
"""


def generate_sql(question: str) -> dict:
    result = invoke_claude(
        SQL_GEN_PROMPT.format(schema=REDSHIFT_SCHEMA_DESCRIPTION, question=question),
        max_tokens=300,
        call_type="sql_generation",
    )
    sql = result["text"].strip().strip("`").strip()
    return {"sql": sql, "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]}


def execute_validated_sql(sql: str):
    validation = validate_sql(sql)
    if not validation["valid"]:
        return {"success": False, "error": validation["reason"], "rows": None}

    conn = psycopg2.connect(
        host=REDSHIFT_HOST, port=REDSHIFT_PORT, dbname=REDSHIFT_DB,
        user=REDSHIFT_USER, password=REDSHIFT_PASSWORD,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()
    return {"success": True, "error": None, "rows": rows, "columns": columns}


# --- 3. OpenSearch retrieval + contextual response generation ---
def embed_query_text(text: str) -> list:
    """Reuses the same ONNX embedding pipeline as the ingestion Lambda,
    so query embeddings live in the same vector space as indexed chunks."""
    from tokenizers import Tokenizer
    import onnxruntime as ort
    import numpy as np
    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download("sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx")
    tokenizer_path = hf_hub_download("sentence-transformers/all-MiniLM-L6-v2", "tokenizer.json")
    tokenizer = Tokenizer.from_file(tokenizer_path)
    session = ort.InferenceSession(model_path)

    encoded = tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)

    outputs = session.run(None, {
        "input_ids": input_ids, "attention_mask": attention_mask, "token_type_ids": token_type_ids,
    })
    token_embeddings = outputs[0][0]
    mask = attention_mask[0]
    summed = (token_embeddings * mask[:, None]).sum(axis=0)
    return (summed / mask.sum()).tolist()


def retrieve_relevant_chunks(question: str, k: int = 3) -> list:
    embedding = embed_query_text(question)
    url = f"https://{OPENSEARCH_HOST}/{OPENSEARCH_INDEX}/_search"
    query_body = {
        "size": k,
        "query": {"knn": {"embedding": {"vector": embedding, "k": k}}},
    }
    resp = requests.post(
        url, json=query_body,
        auth=HTTPBasicAuth(OPENSEARCH_USER, OPENSEARCH_PASSWORD),
        headers={"Content-Type": "application/json"}, timeout=20,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    return [h["_source"]["text"] for h in hits]


CONTEXTUAL_RESPONSE_PROMPT = """Answer the question using ONLY the provided document excerpts.
Cite which excerpt(s) you used. If the excerpts don't contain the answer, say so.

Document excerpts:
{context}

Question: {question}
"""


def generate_contextual_response(question: str) -> dict:
    chunks = retrieve_relevant_chunks(question)
    context = "\n\n".join(f"[Excerpt {i+1}]: {c}" for i, c in enumerate(chunks))
    result = invoke_claude(
        CONTEXTUAL_RESPONSE_PROMPT.format(context=context, question=question),
        max_tokens=500,
        call_type="contextual_response",
    )
    return {"answer": result["text"], "chunks_used": len(chunks)}


# --- 4. Combined synthesis for queries needing both sources ---
SYNTHESIS_PROMPT = """Answer the question by combining BOTH the structured data result and the
document excerpts below into a single coherent answer. Explicitly reference both sources.

Structured data result (from Redshift):
{sql_result}

Document excerpts (from OpenSearch):
{doc_context}

Question: {question}
"""


def answer_query(question: str) -> dict:
    route_info = route_query(question)
    route = route_info["route"]

    if route == "redshift":
        sql_result = generate_sql(question)
        exec_result = execute_validated_sql(sql_result["sql"])
        return {"route": route, "sql": sql_result["sql"], "result": exec_result}

    if route == "opensearch":
        response = generate_contextual_response(question)
        return {"route": route, "answer": response["answer"], "chunks_used": response["chunks_used"]}

    # route == "both"
    sql_result = generate_sql(question)
    exec_result = execute_validated_sql(sql_result["sql"])
    chunks = retrieve_relevant_chunks(question)
    doc_context = "\n\n".join(f"[Excerpt {i+1}]: {c}" for i, c in enumerate(chunks))
    sql_result_str = str(exec_result["rows"]) if exec_result["success"] else f"SQL failed: {exec_result['error']}"

    synthesis = invoke_claude(
        SYNTHESIS_PROMPT.format(sql_result=sql_result_str, doc_context=doc_context, question=question),
        max_tokens=600,
        call_type="synthesis",
    )
    return {"route": route, "sql": sql_result["sql"], "sql_result": exec_result, "answer": synthesis["text"]}


if __name__ == "__main__":
    # The two required harder synthesis queries, plus standard-type queries,
    # for a 10-query tokenomics test run.
    test_queries = [
        "What is the total monthly revenue across all customers?",
        "Which customers are on the Enterprise plan?",
        "What does our retention strategy document say about reducing churn?",
        "How does Textract get used in this pipeline according to the documentation?",
        "What is the average monthly revenue by plan tier?",
        "Does our current customer churn rate align with what our documented retention strategy says we should be seeing?",
        "Based on our data governance policy documents, are any of the currently-ingested datasets missing required metadata fields?",
        "How many customers signed up in 2024?",
        "What embedding model is used for document search according to the docs?",
        "List all customers with monthly revenue above $2000.",
    ]

    for i, q in enumerate(test_queries, 1):
        print(f"\n--- Query {i}/10: {q} ---")
        try:
            result = answer_query(q)
            print(json.dumps(result, default=str, indent=2))
        except Exception as e:
            print(f"ERROR: {e}")

    tracker.print_summary()
