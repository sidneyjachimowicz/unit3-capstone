# Intelligent Document Search Pipeline — Capstone README

## Project Overview

This project implements an AWS-based intelligent document search pipeline that ingests unstructured (PDF) and structured (CSV/JSON) files, extracts and indexes their content for semantic and structured search, and exposes an AI-powered query layer built on Amazon Bedrock (Claude).

**Status: Bronze tier — core unstructured-document pipeline fully built and verified end-to-end. Structured-data ETL, AI query layer, and validation/tokenomics layers in progress.**

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
| Redshift Serverless | Namespace `capstone-docsearch-ns`, workgroup `capstone-docsearch-wg` | Consolidated SQL warehouse for structured + vector data (ETL job pending) |

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

---

## Bronze Checklist Status

- [x] S3 stores raw PDFs, CSVs, and JSONs
- [x] Lambda triggers on upload; calls Textract for PDFs
- [x] Textract extracts and chunks PDF text
- [x] Embeddings generated (ONNX-based, in place of full Sentence Transformers/PyTorch — see Adaptations #4)
- [x] Raw text stored in RDS; embeddings in OpenSearch
- [x] Glue Database/Table catalogs schema (manual, in place of Crawler — see Adaptations #1)
- [ ] Glue ETL normalizes, validates, and loads CSV/JSON into Redshift
- [x] Redshift (Serverless) consolidates structured and vector data
- [ ] Matplotlib charts (2+) visualize Redshift data
- [x] Lambda and BOTO3 automate flows
- [x] IAM secures resources (scoped inline policies throughout)
- [ ] AI query layer (Bedrock routing, NL-to-SQL, contextual response)
- [ ] SQL validation layer tested (5+ cases incl. stacked-query attempt)
- [ ] Tokenomics logging + 10-query cost summary
- [ ] Both harder synthesis queries answered, citing both sources

## Next Steps

1. Build Glue ETL job to load structured (CSV/JSON) data into Redshift
2. Generate 2+ matplotlib charts from Redshift data
3. Build the Bedrock AI query layer (routing, NL-to-SQL, contextual generation) using the inference-profile ARN pattern
4. Implement and test the SQL validation layer
5. Add tokenomics logging across all Bedrock calls
6. Test and document the two required harder synthesis queries
