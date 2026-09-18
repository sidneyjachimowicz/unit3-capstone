# Intelligent Document Search Pipeline

## Project Overview

This project implements an AWS-based intelligent document search pipeline that ingests unstructured (PDF) and structured (CSV/JSON) files, extracts and indexes their content for semantic and structured search, and exposes an AI-powered query layer built on Amazon Bedrock (Claude).

**Status: Bronze tier — complete.** Full pipeline built and verified end-to-end: unstructured-document ingestion (Textract → chunk → embed → RDS/OpenSearch), structured-data ETL (Glue → Redshift), matplotlib visualizations, SQL validation, and the AI query layer (routing, NL-to-SQL, retrieval, synthesis) — the last of which is verified via a deterministic fallback due to an external AWS Marketplace block on live Bedrock invocation (see Adaptations #10).

---

## Architecture

~~~
Upload (PDF) ──▶ S3 (capstone-docsearch-sjach)
                     │
                     ▼
              Lambda trigger (capstoneDocProcessor)
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   Amazon Textract          (structured files:
   (text extraction)         routed to Glue, not
        │                    this Lambda)
        ▼
   Chunking (~750 tokens,
   50-token overlap)
        │
        ▼
   ONNX-based embedding
   (all-MiniLM-L6-v2, 384-dim)
        │
        ├──────────────┬───────────────┐
        ▼              ▼
   RDS Postgres    OpenSearch
   (raw chunk       (chunk text +
   text, structured  embedding vector,
   queries)          k-NN search)


Structured files (CSV/JSON) ──▶ S3 ──▶ Glue Catalog (manual table,
                                        crawler unavailable in this
                                        sandbox — see Adaptations)
                                            │
                                            ▼
                                    Glue ETL job (planned)
                                            │
                                            ▼
                                    Redshift Serverless
                                    (structured + vector data,
                                    SQL-queryable warehouse)
~~~

### AWS Resources

| Resource | Identifier | Purpose |
|---|---|---|
| S3 bucket | `capstone-docsearch-sjach` | Central raw storage for PDFs, CSVs, JSON, and deployment artifacts |
| Lambda function | `capstoneDocProcessor` | Triggered on S3 upload; runs Textract, chunking, embedding, and writes to RDS/OpenSearch |
| Lambda execution role | `capstone-lambda-role` | Scoped inline policies for S3 and Textract |
| RDS (PostgreSQL) | `capstone-docsearch-db` | Stores raw extracted text (`document_chunks` table) |
| OpenSearch domain | `capstone-docsearch` | Stores chunk text + 384-dim embedding vectors, k-NN enabled index (`capstone-documents`) |
| Glue Database | `capstone_docsearch_db` | Data Catalog namespace |
| Glue Table | `customer_data` | Manually cataloged schema (crawler unavailable — see below) |
| Glue role | `capstone-glue-role` | For future ETL job execution |
| Redshift Serverless | Namespace `capstone-docsearch-ns`, workgroup `capstone-docsearch-wg` | Consolidated SQL warehouse for structured + vector data |

### Code Files

| File | Purpose |
|---|---|
| `lambda_function.py` | Document ingestion: Textract extraction, chunking, ONNX embedding, RDS + OpenSearch writes |
| `glue_etl_job.py` | Reads CSV from S3, validates/normalizes, writes to Redshift via direct JDBC |
| `generate_charts.py` | Queries Redshift, produces 2 matplotlib charts |
| `test_sql_validator.py` | SQL validation logic + 9-case test suite (includes stacked-query test) |
| `sql_validator_test_output.txt` | Saved output from running the validator's test suite (9/9 passing) |
| `ai_query_layer.py` | Full AI query layer: routing, NL-to-SQL, validated execution, OpenSearch retrieval, synthesis, tokenomics tracking, Bedrock-blocked fallback |
| `query_layer_test_output.txt` | Saved output from the 10-query test batch (fallback path) |

### Credentials

Master credentials for RDS and OpenSearch are stored as environment variables on the Lambda function and locally in the sandbox (not committed to any repository). **These are training-sandbox credentials only and should be rotated or discarded when the sandbox is retired.** In a production setting, these would be managed via AWS Secrets Manager rather than plaintext environment variables.

---

## Adaptations From the Original Plan

This sandbox account (`Whiz_User_...`, tier 1) has a restrictive, narrowly-scoped permission set with no ability to self-grant access. Several planned steps hit explicit or implicit permission walls. Each was diagnosed from the actual AWS error text and resolved with a documented workaround rather than skipped:

1. **Glue Crawler creation blocked** (`glue:CreateCrawler` — no identity-based policy allows this action, and self-granting via `iam:PutUserPolicy` was explicitly denied). **Workaround:** created the Glue Database and Table manually via `glue:CreateTable` with an explicit schema, achieving the same cataloging outcome without auto-discovery.

2. **Redshift provisioned-cluster subnet group creation blocked** (`redshift:CreateClusterSubnetGroup` denied). **Workaround:** used **Redshift Serverless** instead, which doesn't require a manually created subnet group in the same way. This also better fits a learning project's cost profile (scales to zero when idle).

3. **ECR repository access fully blocked** (`ecr:CreateRepository` and even `ecr:DescribeRepositories` denied), ruling out a container-image deployment for the Lambda function's ML dependencies. **Workaround:** used a direct Lambda deployment package (dependencies + code bundled and zipped together) instead of a Lambda Layer or container image.

4. **PyTorch too large for any Lambda packaging strategy** (~1.4GB even with CPU-only builds, versus Lambda's 250MB layer / effective deployment-package ceiling). **Workaround:** bypassed the `sentence-transformers` Python package (which hard-imports `torch` even when configured with `backend="onnx"`, confirmed via upstream GitHub issues) and instead load the same `all-MiniLM-L6-v2` model's published ONNX weights directly via `onnxruntime` + `tokenizers`, manually implementing tokenization, inference, and mean-pooling. This produces numerically identical embeddings to the full library, at a fraction of the dependency size (~150MB vs. 1.4GB+).

5. **RDS kept private initially, but Lambda runs outside any VPC.** Attaching Lambda to the VPC to reach private RDS would have required a NAT Gateway (added cost/complexity) since the function also needs internet access for Textract, Hugging Face model downloads, and the OpenSearch endpoint. **Workaround:** made RDS publicly accessible (matching the same public+authenticated pattern already used for OpenSearch), protected by its master password and a security group.

6. **Binary-package platform mismatches.** `psycopg2-binary` and `onnxruntime`, when installed directly in CloudShell, pulled wheels built for CloudShell's environment rather than Lambda's actual runtime, causing `ImportModuleError` at cold start. **Fix:** installed both with `pip install --platform manylinux_2_28_x86_64 --implementation cp --python-version 3.12 --only-binary=:all:` to force Lambda-compatible binaries.

7. **Lambda deployment size exceeded the direct CLI upload limit** (~70MB request-size ceiling), once the corrected `onnxruntime` wheel was included. **Fix:** switched to S3-based deployment (`aws lambda update-function-code --s3-bucket ... --s3-key ...`), which supports the full 250MB unzipped limit.

8. **`huggingface_hub` cache-path bug.** The library reads `HF_HOME` at import time to compute its cache directory; setting the environment variable after import had no effect, and the library fell back to a non-writable path in Lambda's filesystem. **Fix:** set `HOME`, `HF_HOME`, and `TRANSFORMERS_CACHE` environment variables at the very top of the file, before any dependent imports.

9. **CloudShell's `/tmp` directory is ephemeral.** Build work done directly in `/tmp` (dependency installs, the finalized `lambda_function.py`) was lost when the CloudShell session went inactive. **Fix:** recovered the deployed code from the S3 deployment artifact (`s3://capstone-docsearch-sjach/deployments/function-deploy.zip`), and going forward, finalized source files are copied into the persistent home directory / committed to git immediately rather than left in `/tmp`.

10. **Bedrock model invocation blocked at the AWS Marketplace subscription layer.** After one initial successful test call, all subsequent `bedrock:InvokeModel` calls failed with `AccessDeniedException`, specifically citing missing `aws-marketplace:Subscribe` / `aws-marketplace:ViewSubscriptions` permissions needed to complete the model's Marketplace subscription. This is distinct from every other permission wall in this project (which were all missing IAM grants) — this is an AWS Marketplace subscription state that, per AWS's own documentation, requires a user with Marketplace permissions to complete, and is not resolvable through any Bedrock- or IAM-side configuration available to a tier-1 sandbox user. Waited 5+ minutes and retried with identical credentials/code; failure was consistent, not transient.

    **Adaptation (per instructor guidance to complete everything achievable without live Bedrock access):** `ai_query_layer.py` implements the full intended architecture (routing, NL-to-SQL, validated execution, OpenSearch retrieval, combined synthesis) calling real Bedrock. `invoke_claude()` wraps the Bedrock call in a try/except that, specifically on `AccessDeniedException`, falls back to a small deterministic (keyword/template-based) stand-in — **not a language model** — so the rest of the pipeline can still be exercised with real infrastructure calls: real query routing decisions, real validated SQL execution against live Redshift data, real OpenSearch k-NN retrieval against indexed document embeddings, and a real tokenomics log (which correctly reports 0 tokens/cost for fallback calls, since no model was actually invoked). The full 10-query test batch (including both required harder synthesis queries) was run end-to-end on this fallback path; output is saved in `query_layer_test_output.txt`. The SQL fallback in particular is a crude template match and produces incorrect SQL for several less common phrasings (documented honestly rather than hidden) — it demonstrates the validation/execution wiring is correct, not that NL-to-SQL is solved without an LLM. The real Bedrock code path is unchanged and ready to run correctly the moment Marketplace access is resolved.

---

## How to Test

**Upload a PDF and confirm processing:**
```bash
aws s3 cp your-document.pdf s3://capstone-docsearch-sjach/your-document.pdf
sleep 20
aws logs tail /aws/lambda/capstoneDocProcessor --since 1m
```

**Verify RDS storage:**
```bash
PGPASSWORD='<password>' psql -h capstone-docsearch-db.cm36ie8wq7x9.us-east-1.rds.amazonaws.com \
  -U capstoneadmin -d postgres \
  -c "SELECT document_key, chunk_index, LEFT(chunk_text, 60) FROM document_chunks;"
```

**Verify OpenSearch indexing:**
```bash
curl -s -u capstoneadmin:'<password>' \
  "https://<opensearch-endpoint>/capstone-documents/_search?pretty"
```

