import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, trim, when, cast
from pyspark.sql.types import DecimalType
from awsglue.dynamicframe import DynamicFrame

# --- Job setup ---
args = getResolvedOptions(sys.argv, ["JOB_NAME"])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# --- Configuration ---
SOURCE_PATH = "s3://capstone-docsearch-sjach/structured-data/"
REDSHIFT_CONNECTION = "capstone-redshift-connection"
REDSHIFT_TABLE = "public.customer_data"
REDSHIFT_TEMP_DIR = "s3://capstone-docsearch-sjach/glue-temp/"

# --- 1. Read raw CSV data from S3 ---
raw_df = spark.read.option("header", "true").csv(SOURCE_PATH)
print(f"Rows read from source: {raw_df.count()}")

# --- 2. Validate: drop rows missing required fields ---
required_columns = ["customer_id", "customer_name", "signup_date", "plan_tier", "monthly_revenue"]
missing_cols = [c for c in required_columns if c not in raw_df.columns]
if missing_cols:
    raise ValueError(f"Source data is missing required columns: {missing_cols}")

validated_df = raw_df.filter(
    col("customer_id").isNotNull() & (trim(col("customer_id")) != "")
)
dropped_count = raw_df.count() - validated_df.count()
print(f"Dropped {dropped_count} rows with null/empty customer_id")

# --- 3. Normalize: trim text fields, cast revenue to decimal, handle bad values ---
normalized_df = validated_df \
    .withColumn("customer_name", trim(col("customer_name"))) \
    .withColumn("plan_tier", trim(col("plan_tier"))) \
    .withColumn(
        "monthly_revenue",
        when(col("monthly_revenue").rlike(r"^\d+(\.\d+)?$"), col("monthly_revenue").cast(DecimalType(10, 2)))
        .otherwise(None)
    )

invalid_revenue_count = normalized_df.filter(col("monthly_revenue").isNull()).count()
print(f"Rows with invalid/unparseable monthly_revenue set to NULL: {invalid_revenue_count}")

print(f"Final row count to load: {normalized_df.count()}")

# --- 4. Write to Redshift directly via JDBC (bypassing the Glue Connection object,
#         which failed to resolve even after network/security-group fixes) ---
dynamic_frame = DynamicFrame.fromDF(
    normalized_df, glueContext, "normalized_dynamic_frame"
)

glueContext.write_dynamic_frame_from_options(
    frame=dynamic_frame,
    connection_type="redshift",
    connection_options={
        "url": "jdbc:redshift://capstone-docsearch-wg.567781376107.us-east-1.redshift-serverless.amazonaws.com:5439/dev",
        "dbtable": REDSHIFT_TABLE,
        "user": "capstoneadmin",
        "password": "TrainingAWS123",
        "redshiftTmpDir": REDSHIFT_TEMP_DIR,
    },
)

print("ETL job complete: data loaded into Redshift.")
job.commit()
