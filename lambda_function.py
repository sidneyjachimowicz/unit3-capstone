import json
import os

# Must be set BEFORE importing huggingface_hub, since it reads these
# at import time to compute its default cache directory.
os.environ["HOME"] = "/tmp"
os.environ["HF_HOME"] = "/tmp/hf_cache"
os.environ["TRANSFORMERS_CACHE"] = "/tmp/hf_cache"

import boto3
import psycopg2
import requests
from requests.auth import HTTPBasicAuth
from tokenizers import Tokenizer
import onnxruntime as ort
import numpy as np
from huggingface_hub import hf_hub_download

# --- Configuration (env vars set on the Lambda function) ---
RDS_HOST = os.environ.get("RDS_HOST", "capstone-docsearch-db.cm36ie8wq7x9.us-east-1.rds.amazonaws.com")
RDS_PORT = os.environ.get("RDS_PORT", "5432")
RDS_DB = os.environ.get("RDS_DB", "postgres")
RDS_USER = os.environ.get("RDS_USER", "capstoneadmin")
RDS_PASSWORD = os.environ.get("RDS_PASSWORD", "TrainingAWS")

OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "search-capstone-docsearch-2vwjeiie2vr2ojl7bgtckkumua.us-east-1.es.amazonaws.com")
OPENSEARCH_USER = os.environ.get("OPENSEARCH_USER", "capstoneadmin")
OPENSEARCH_PASSWORD = os.environ.get("OPENSEARCH_PASSWORD", "TrainingAWS123#")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "capstone-documents")


CHUNK_TOKEN_TARGET = 750
CHUNK_TOKEN_OVERLAP = 50

_tokenizer = None
_onnx_session = None

textract = boto3.client("textract")
s3 = boto3.client("s3")


def get_embedding_model():
    global _tokenizer, _onnx_session
    if _tokenizer is None or _onnx_session is None:
        model_path = hf_hub_download("sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx")
        tokenizer_path = hf_hub_download("sentence-transformers/all-MiniLM-L6-v2", "tokenizer.json")
        _tokenizer = Tokenizer.from_file(tokenizer_path)
        _onnx_session = ort.InferenceSession(model_path)
    return _tokenizer, _onnx_session


def embed_text(text):
    tokenizer, session = get_embedding_model()
    encoded = tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)

    outputs = session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    })

    token_embeddings = outputs[0][0]
    mask = attention_mask[0]
    summed = (token_embeddings * mask[:, None]).sum(axis=0)
    counts = mask.sum()
    embedding = summed / counts
    return embedding.tolist()


def chunk_text(text, tokenizer):
    encoded = tokenizer.encode(text)
    ids = encoded.ids
    chunks = []
    start = 0
    while start < len(ids):
        end = min(start + CHUNK_TOKEN_TARGET, len(ids))
        chunk_ids = ids[start:end]
        chunk_text_str = tokenizer.decode(chunk_ids)
        chunks.append(chunk_text_str)
        if end == len(ids):
            break
        start = end - CHUNK_TOKEN_OVERLAP
    return chunks


def extract_text_with_textract(bucket, key):
    response = textract.detect_document_text(
        Document={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    lines = [block["Text"] for block in response.get("Blocks", []) if block["BlockType"] == "LINE"]
    return "\n".join(lines)


def store_chunk_in_rds(conn, doc_key, chunk_index, chunk_text_str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO document_chunks (document_key, chunk_index, chunk_text) VALUES (%s, %s, %s)",
            (doc_key, chunk_index, chunk_text_str),
        )
    conn.commit()


def index_chunk_in_opensearch(doc_key, chunk_index, chunk_text_str, embedding):
    url = f"https://{OPENSEARCH_HOST}/{OPENSEARCH_INDEX}/_doc"
    payload = {
        "document_key": doc_key,
        "chunk_index": chunk_index,
        "text": chunk_text_str,
        "embedding": embedding,
    }
    resp = requests.post(
        url, json=payload,
        auth=HTTPBasicAuth(OPENSEARCH_USER, OPENSEARCH_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_rds_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS document_chunks (
                id SERIAL PRIMARY KEY,
                document_key TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
    conn.commit()


def lambda_handler(event, context):
    results = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        print(f"Processing s3://{bucket}/{key}")

        if not key.lower().endswith(".pdf"):
            print(f"Skipping non-PDF file: {key}")
            continue

        extracted_text = extract_text_with_textract(bucket, key)
        if not extracted_text.strip():
            print(f"No text extracted from {key}")
            continue

        tokenizer, _ = get_embedding_model()
        chunks = chunk_text(extracted_text, tokenizer)
        print(f"Split into {len(chunks)} chunks")

        conn = psycopg2.connect(
            host=RDS_HOST, port=RDS_PORT, dbname=RDS_DB,
            user=RDS_USER, password=RDS_PASSWORD, connect_timeout=10,
        )
        ensure_rds_table(conn)

        for i, chunk in enumerate(chunks):
            embedding = embed_text(chunk)
            store_chunk_in_rds(conn, key, i, chunk)
            index_chunk_in_opensearch(key, i, chunk, embedding)

        conn.close()
        results.append({"key": key, "chunks_processed": len(chunks)})

    return {"statusCode": 200, "body": json.dumps({"processed": results})}
